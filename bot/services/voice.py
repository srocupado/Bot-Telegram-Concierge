from __future__ import annotations

import asyncio
import logging

import google.generativeai as genai

from bot.config import settings

logger = logging.getLogger(__name__)


class VoiceTranscribeError(Exception):
    pass


_TRANSCRIBE_PROMPT = (
    "Transcreva o áudio em português brasileiro. "
    "Retorne apenas a transcrição literal, sem comentários, "
    "sem aspas, sem prefixos. "
    "Se o áudio começa com a palavra 'barra' seguida de um comando "
    "(ex: 'barra trânsito now casa'), transcreva como o slash + comando "
    "com underline (ex: '/transito_now casa')."
)

_STT_MODEL = "gemini-2.5-flash"


async def transcribe(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Transcreve áudio usando Gemini multimodal.

    Lança VoiceTranscribeError em falhas de API.
    Retorna string vazia se o áudio não tem fala detectável.
    """
    if not settings.gemini_api_key:
        raise VoiceTranscribeError("GEMINI_API_KEY ausente — STT requer Gemini")

    genai.configure(api_key=settings.gemini_api_key)

    def _call() -> str:
        try:
            model = genai.GenerativeModel(_STT_MODEL)
            resp = model.generate_content(
                [
                    {"mime_type": mime_type, "data": audio_bytes},
                    _TRANSCRIBE_PROMPT,
                ],
                generation_config={"max_output_tokens": 1024, "temperature": 0.0},
            )
            return (resp.text or "").strip()
        except Exception as e:
            raise VoiceTranscribeError(f"gemini transcribe failed: {e}") from e

    return await asyncio.to_thread(_call)
