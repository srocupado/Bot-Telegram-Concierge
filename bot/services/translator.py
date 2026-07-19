"""Tradução de áudio para o modo tradutor.

Recebe o áudio (na língua falada), devolve {original, translation} no idioma-alvo.
Coerente com o /tradutor_provider — o MESMO motor entende e fala:
  - openai → Whisper (auto-detecta a língua) + GPT traduz  (não treina)
  - gemini → uma chamada multimodal (áudio → JSON)         (tier grátis treina)
"""
from __future__ import annotations

import asyncio
import io
import logging

from bot.config import settings

logger = logging.getLogger(__name__)


class TranslateError(Exception):
    pass


# Aliases comuns (código ou nome PT/EN) → nome canônico em PT que o LLM entende.
# Idioma fora da lista passa como veio (o modelo entende "coreano", "holandês"…).
_LANG_ALIASES = {
    "en": "inglês", "ingles": "inglês", "inglês": "inglês", "english": "inglês",
    "pt": "português", "portugues": "português", "português": "português",
    "portuguese": "português", "pt-br": "português",
    "es": "espanhol", "espanhol": "espanhol", "spanish": "espanhol", "español": "espanhol",
    "fr": "francês", "frances": "francês", "francês": "francês", "french": "francês",
    "de": "alemão", "alemao": "alemão", "alemão": "alemão", "german": "alemão",
    "it": "italiano", "italiano": "italiano", "italian": "italiano",
    "ja": "japonês", "japones": "japonês", "japonês": "japonês", "japanese": "japonês",
    "zh": "chinês", "chines": "chinês", "chinês": "chinês", "chinese": "chinês",
    "mandarim": "chinês", "mandarin": "chinês",
    "ru": "russo", "russo": "russo", "russian": "russo",
    "ko": "coreano", "coreano": "coreano", "korean": "coreano",
}


def normalize_lang(raw: str) -> str | None:
    """Normaliza o idioma-alvo. Vazio → None. Desconhecido → devolve como veio."""
    key = (raw or "").strip().lower()
    if not key:
        return None
    return _LANG_ALIASES.get(key, (raw or "").strip())


def _parse_delimited(text: str) -> dict:
    """Extrai {original, translation} do formato <<<ORIGINAL>>>/<<<TRADUCAO>>>.
    Tolerante: sem o delimitador, usa o texto todo como tradução (melhor
    entregar a tradução sem o original do que falhar). Imune a aspas/quebras."""
    t = (text or "").strip()
    if t.startswith("```"):  # modelo insistiu em cerca markdown
        t = t.strip("`").strip()
        if t.lower().startswith("json"):
            t = t[4:].strip()
    if "<<<TRADUCAO>>>" in t:
        head, _, tail = t.partition("<<<TRADUCAO>>>")
        original = head.replace("<<<ORIGINAL>>>", "").strip()
        translation = tail.strip()
    else:
        original = ""
        translation = t.replace("<<<ORIGINAL>>>", "").strip()
    return {"original": original, "translation": translation}


def _ext_from_mime(mime_type: str) -> str:
    if mime_type and "/" in mime_type:
        return mime_type.split("/")[-1].split(";")[0] or "ogg"
    return "ogg"


async def _translate_openai(
    audio_bytes: bytes, mime_type: str, target_lang: str, *, stt_model: str | None = None,
) -> dict:
    if not settings.openai_api_key:
        raise TranslateError("OPENAI_API_KEY ausente")

    def _call() -> dict:
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key)
        # SEM language= → Whisper auto-detecta (o falante pode estar em inglês).
        f = io.BytesIO(audio_bytes)
        f.name = f"audio.{_ext_from_mime(mime_type)}"
        tr = client.audio.transcriptions.create(
            model=stt_model or settings.voice_stt_openai_model, file=f,
        )
        original = (getattr(tr, "text", "") or "").strip()
        if not original:
            return {"original": "", "translation": ""}
        chat = client.chat.completions.create(
            model=settings.translator_openai_chat_model,
            temperature=0.2,
            messages=[
                {"role": "system", "content": (
                    f"Traduza o texto do usuário para {target_lang}. "
                    f"BIDIRECIONAL: se o texto JÁ estiver em {target_lang} "
                    "(ex.: a resposta de um atendente local), traduza para "
                    "português brasileiro. Responda SOMENTE com a tradução — "
                    "sem aspas, sem comentários, sem o texto original."
                )},
                {"role": "user", "content": original},
            ],
        )
        translation = (chat.choices[0].message.content or "").strip()
        return {"original": original, "translation": translation}

    return await asyncio.to_thread(_call)


async def _translate_gemini(
    audio_bytes: bytes, mime_type: str, target_lang: str, *, model: str | None = None,
) -> dict:
    if not settings.gemini_api_key:
        raise TranslateError("GEMINI_API_KEY ausente")

    def _call() -> dict:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=settings.gemini_api_key)
        model_id = model or settings.translator_stt_gemini_model or settings.voice_stt_model
        # Formato DELIMITADO em vez de JSON: transcrição/tradução têm aspas,
        # apóstrofos e quebras de linha que arrebentam JSON quando o modelo não
        # escapa (o bug real observado: JSONDecodeError). Sentinela é imune.
        prompt = (
            "O áudio contém fala em algum idioma. Responda EXATAMENTE neste "
            "formato, sem comentários, sem markdown, sem mais nada:\n"
            "<<<ORIGINAL>>>\n"
            "a transcrição literal do que foi dito, no idioma original\n"
            "<<<TRADUCAO>>>\n"
            f"a tradução para {target_lang}. BIDIRECIONAL: se a fala JÁ "
            f"estiver em {target_lang} (ex.: a resposta de um atendente "
            "local), a tradução deve ser para português brasileiro"
        )
        # Desliga o thinking: gemini-3.x é 'thinking' e o texto de pensamento
        # contaminava resp.text (a outra metade do bug). flash aceita 0; pro
        # exige mínimo ~128. Espelha bot/services/voice._thinking_config.
        thinking = None
        try:
            budget = 128 if "pro" in (model_id or "") else 0
            thinking = types.ThinkingConfig(thinking_budget=budget)
        except Exception:
            thinking = None
        resp = client.models.generate_content(
            model=model_id,
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                types.Part.from_text(text=prompt),
            ],
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=2048,
                thinking_config=thinking,
            ),
        )
        return _parse_delimited(resp.text or "")

    return await asyncio.to_thread(_call)


async def translate_audio(
    audio_bytes: bytes, mime_type: str, target_lang: str, *,
    provider: str, model: str | None = None,
) -> dict:
    """Áudio → {original, translation}. `provider` = 'openai' | 'gemini'.
    `model` sobrepõe o modelo de entendimento (id do provider; None = default)."""
    prov = (provider or settings.translator_tts_provider or "openai").lower()
    if prov == "gemini":
        return await _translate_gemini(audio_bytes, mime_type, target_lang, model=model)
    return await _translate_openai(audio_bytes, mime_type, target_lang, stt_model=model)
