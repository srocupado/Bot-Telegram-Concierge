from __future__ import annotations

import asyncio
import logging

from google import genai
from google.genai import types

from bot.config import settings

logger = logging.getLogger(__name__)


class VoiceTranscribeError(Exception):
    pass


_TRANSCRIBE_PROMPT = """\
Transcreva o áudio em português brasileiro. Retorne APENAS a transcrição,
sem aspas, sem prefixos, sem comentários, sem markdown.

REGRAS DE CONVERSÃO PARA COMANDO (aplique somente quando a intenção do
falante é claramente pedir a informação agora; em conversa casual mantenha
transcrição literal):

1. Se o áudio começa com a palavra "barra" seguida do nome de um comando,
   transcreva como "/comando" com underline ligando as palavras.
   Exemplos:
     "barra trânsito agora casa"  → /transito_agora casa
     "barra tarefas"              → /tarefas
     "barra ping"                 → /ping

2. Para trânsito, qualquer das formas abaixo deve virar "/transito_agora <destino>":
     "trânsito para casa", "trânsito pra casa", "trânsito casa",
     "como está o trânsito para casa"        → /transito_agora casa
     "trânsito para o trabalho", "trânsito pro trabalho",
     "trânsito trabalho"                      → /transito_agora trabalho

3. Para congresso, a frase "pauta de MP do congresso agora"
   (e variações próximas de pontuação ou preposição, ex: "pauta de MPs no
   congresso agora", "pauta das MPs do congresso") → /congresso_agora

4. Para pedido de rota com localização atual, qualquer das formas abaixo
   vira "/rota <destino>" — preserve o destino exatamente como falado,
   inclusive nomes próprios, números e acentos:
     "rota para casa", "rota pra casa"                → /rota casa
     "rota para o trabalho", "rota pro trabalho"      → /rota trabalho
     "rota para X", "rota pra X", "como chegar em X",
     "como chegar até X", "trajeto até X",
     "me leva para X"                                  → /rota X

5. Para pedido de busca/pesquisa na web, qualquer das formas abaixo vira
   "/buscar <termo>" — preserve o termo exatamente como falado:
     "busca X", "busca por X", "busca X na internet"  → /buscar X
     "pesquisa X", "pesquisa sobre X"                 → /buscar X
     "procura X", "procura por X"                     → /buscar X
     "google X", "googleia X"                          → /buscar X

Em qualquer outro caso, transcreva literalmente o que foi dito —
inclusive quando o usuário só MENCIONA trânsito ou congresso em uma
conversa, sem pedir explicitamente a informação naquele momento.

IMPORTANTE: NÃO converta pedidos de tarefa, lembrete, clima, ou
qualquer outra ação em comando com "/". Apenas transcreva literalmente.
Essas intenções são tratadas pelo chat livre (que tem ferramentas para
agir). Exemplos do que NÃO virar comando:
  "nova tarefa comprar leite"          → transcrição literal
  "me lembre de pagar boleto amanhã"   → transcrição literal
  "apaga o lembrete 5"                 → transcrição literal
  "qual a previsão do tempo hoje"      → transcrição literal
"""

# Cadeia de fallback quando o modelo principal está sobrecarregado (503).
# flash-lite tem mais capacidade; gemini-2.0-flash é de outra geração (GA,
# pool de capacidade separado) e não sofre dos picos de demanda do 2.5.
_STT_FALLBACK_MODELS = ["gemini-2.5-flash-lite", "gemini-2.0-flash"]


def _is_transient(exc: Exception) -> bool:
    """True para erros temporários do lado do Google (sobrecarga/instabilidade)
    que valem retry: 503 UNAVAILABLE, 429 (rate limit), 'overloaded'."""
    s = str(exc).lower()
    return any(t in s for t in ("503", "unavailable", "overloaded", "429", "high demand"))


def _thinking_config(model: str):
    """Desliga o thinking pra STT (transcrição não precisa raciocinar; ganha
    velocidade e custo). O pro não permite 0 (mín ~128), então clampa."""
    budget = 0
    if "pro" in (model or ""):
        budget = 128
    try:
        return types.ThinkingConfig(thinking_budget=budget)
    except Exception:
        return None


async def _transcribe_openai(audio_bytes: bytes, mime_type: str) -> str:
    """Transcrição via OpenAI (Whisper / gpt-4o-transcribe). Literal — não faz
    a conversão pra comando que o Gemini multimodal faz."""
    import io

    from openai import OpenAI

    if not settings.openai_api_key:
        raise VoiceTranscribeError("OPENAI_API_KEY ausente — STT openai requer chave")

    ext = "ogg"
    if mime_type and "/" in mime_type:
        ext = mime_type.split("/")[-1].split(";")[0] or "ogg"

    def _call() -> str:
        try:
            client = OpenAI(api_key=settings.openai_api_key)
            f = io.BytesIO(audio_bytes)
            f.name = f"audio.{ext}"
            resp = client.audio.transcriptions.create(
                model=settings.voice_stt_openai_model,
                file=f,
                language="pt",
            )
            return (getattr(resp, "text", "") or "").strip()
        except Exception as e:
            raise VoiceTranscribeError(f"openai transcribe failed: {e}") from e

    return await asyncio.to_thread(_call)


async def transcribe(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Transcreve áudio. Provider escolhido por VOICE_STT_PROVIDER:
    "gemini" (multimodal, faz conversão p/ comando) ou "openai" (literal).

    No Gemini, em 503/sobrecarga tenta de novo com backoff e cai num modelo
    de fallback (flash-lite → 2.0-flash) se o principal seguir congestionado.
    """
    if settings.voice_stt_provider == "openai":
        return await _transcribe_openai(audio_bytes, mime_type)

    if not settings.gemini_api_key:
        raise VoiceTranscribeError("GEMINI_API_KEY ausente — STT requer Gemini")

    def _call(model: str) -> str:
        client = genai.Client(api_key=settings.gemini_api_key)
        resp = client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                types.Part.from_text(text=_TRANSCRIBE_PROMPT),
            ],
            config=types.GenerateContentConfig(
                max_output_tokens=8192,
                temperature=0.0,
                thinking_config=_thinking_config(model),
            ),
        )
        return (resp.text or "").strip()

    models = [settings.voice_stt_model]
    for m in _STT_FALLBACK_MODELS:
        if m not in models:
            models.append(m)

    last_exc: Exception | None = None
    for model in models:
        for attempt in range(2):
            try:
                return await asyncio.to_thread(_call, model)
            except Exception as e:
                last_exc = e
                if _is_transient(e):
                    logger.warning(
                        "STT %s transitório (tentativa %d): %s", model, attempt + 1, e
                    )
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise VoiceTranscribeError(f"gemini transcribe failed: {e}") from e
    raise VoiceTranscribeError(f"gemini transcribe failed: {last_exc}") from last_exc
