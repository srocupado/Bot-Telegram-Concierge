"""STT via Gemini 2.5 Flash multimodal — aceita OGG/Opus nativo."""
from __future__ import annotations

import asyncio
import logging

import google.generativeai as genai

from bot.config import settings

logger = logging.getLogger(__name__)


class VoiceTranscribeError(Exception):
    pass


_PROMPT = (
    "Transcreva o áudio em português brasileiro. "
    "Retorne APENAS a transcrição literal do que foi dito, "
    "sem comentários, sem aspas, sem prefixos como 'O áudio diz:'. "
    "Se o áudio estiver vazio ou inaudível, retorne uma string vazia."
)

_MODEL_NAME = "gemini-2.5-flash"


async def transcribe(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Transcreve áudio para texto usando Gemini multimodal.

    Args:
        audio_bytes: bytes do arquivo de áudio (Telegram envia OGG/Opus).
        mime_type: MIME type do áudio.

    Returns:
        Texto transcrito (string vazia se Gemini não detectar fala).

    Raises:
        VoiceTranscribeError: se a chamada à API falhar.
    """
    if not settings.gemini_api_key:
        raise VoiceTranscribeError(
            "GEMINI_API_KEY ausente — necessário para transcrição de voz"
        )

    genai.configure(api_key=settings.gemini_api_key)

    def _call() -> str:
        try:
            model = genai.GenerativeModel(_MODEL_NAME)
            resp = model.generate_content(
                [
                    {"mime_type": mime_type, "data": audio_bytes},
                    _PROMPT,
                ]
            )
            return (resp.text or "").strip()
        except Exception as e:
            raise VoiceTranscribeError(f"Gemini transcribe failed: {e}") from e

    return await asyncio.to_thread(_call)
