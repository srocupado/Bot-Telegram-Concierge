"""Tradução de áudio para o modo tradutor.

Recebe o áudio (na língua falada), devolve {original, translation} no idioma-alvo.
Coerente com o /tradutor_provider — o MESMO motor entende e fala:
  - openai → Whisper (auto-detecta a língua) + GPT traduz  (não treina)
  - gemini → uma chamada multimodal (áudio → JSON)         (tier grátis treina)
"""
from __future__ import annotations

import asyncio
import io
import json
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


def _ext_from_mime(mime_type: str) -> str:
    if mime_type and "/" in mime_type:
        return mime_type.split("/")[-1].split(";")[0] or "ogg"
    return "ogg"


async def _translate_openai(audio_bytes: bytes, mime_type: str, target_lang: str) -> dict:
    if not settings.openai_api_key:
        raise TranslateError("OPENAI_API_KEY ausente")

    def _call() -> dict:
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key)
        # SEM language= → Whisper auto-detecta (o falante pode estar em inglês).
        f = io.BytesIO(audio_bytes)
        f.name = f"audio.{_ext_from_mime(mime_type)}"
        tr = client.audio.transcriptions.create(
            model=settings.voice_stt_openai_model, file=f,
        )
        original = (getattr(tr, "text", "") or "").strip()
        if not original:
            return {"original": "", "translation": ""}
        chat = client.chat.completions.create(
            model=settings.translator_openai_chat_model,
            temperature=0.2,
            messages=[
                {"role": "system", "content": (
                    f"Traduza o texto do usuário para {target_lang}. Responda "
                    "SOMENTE com a tradução — sem aspas, sem comentários, sem "
                    "o texto original."
                )},
                {"role": "user", "content": original},
            ],
        )
        translation = (chat.choices[0].message.content or "").strip()
        return {"original": original, "translation": translation}

    return await asyncio.to_thread(_call)


async def _translate_gemini(audio_bytes: bytes, mime_type: str, target_lang: str) -> dict:
    if not settings.gemini_api_key:
        raise TranslateError("GEMINI_API_KEY ausente")

    def _call() -> dict:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=settings.gemini_api_key)
        prompt = (
            "O áudio contém fala em algum idioma. Faça DUAS coisas:\n"
            "1) transcreva LITERALMENTE o que foi dito, no idioma original;\n"
            f"2) traduza para {target_lang}.\n"
            'Responda SOMENTE JSON: {"original": "...", "traducao": "..."}'
        )
        resp = client.models.generate_content(
            model=settings.translator_stt_gemini_model or settings.voice_stt_model,
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                types.Part.from_text(text=prompt),
            ],
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=2048,
                response_mime_type="application/json",
            ),
        )
        try:
            data = json.loads(resp.text or "{}")
        except json.JSONDecodeError as e:
            raise TranslateError(f"JSON inválido do Gemini: {e}") from e
        return {
            "original": (data.get("original") or "").strip(),
            "translation": (data.get("traducao") or data.get("translation") or "").strip(),
        }

    return await asyncio.to_thread(_call)


async def translate_audio(
    audio_bytes: bytes, mime_type: str, target_lang: str, *, provider: str,
) -> dict:
    """Áudio → {original, translation}. `provider` = 'openai' | 'gemini'."""
    prov = (provider or settings.translator_tts_provider or "openai").lower()
    if prov == "gemini":
        return await _translate_gemini(audio_bytes, mime_type, target_lang)
    return await _translate_openai(audio_bytes, mime_type, target_lang)
