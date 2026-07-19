"""Síntese de voz (TTS) para o modo tradutor.

Converte texto → OGG/Opus (o formato da "nota de voz" do Telegram, a bolha com
waveform). Dois motores, escolhidos pelo /tradutor_provider:
  - openai → gpt-4o-mini-tts (não treina com seu dado por padrão)
  - gemini → *-tts (tier grátis PODE treinar; qualidade boa, chave que já existe)

Ambos os áudios crus passam pelo ffmpeg pra normalizar em OGG/Opus.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess

from bot.config import settings

logger = logging.getLogger(__name__)


class TTSError(Exception):
    pass


def _to_ogg_opus(raw: bytes, *, input_args: list[str]) -> bytes:
    """Normaliza QUALQUER áudio em OGG/Opus via ffmpeg (stdin→stdout).
    `input_args` descreve a entrada quando ela é crua (PCM sem header);
    vazio quando o container já se identifica (WAV/MP3)."""
    if not raw:
        raise TTSError("áudio de entrada vazio")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        *input_args, "-i", "pipe:0",
        "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1",
        "-f", "ogg", "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as e:
        raise TTSError("ffmpeg não encontrado no container") from e
    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr.decode("utf-8", "ignore")[:300]
        raise TTSError(f"ffmpeg falhou: {err}")
    return proc.stdout


async def _synth_openai(text: str, *, voice: str, model: str) -> bytes:
    if not settings.openai_api_key:
        raise TTSError("OPENAI_API_KEY ausente")

    def _call() -> bytes:
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key)
        # response_format=wav: header presente, ffmpeg detecta o formato sozinho.
        resp = client.audio.speech.create(
            model=model, voice=voice, input=text, response_format="wav",
        )
        raw = resp.content
        return _to_ogg_opus(raw, input_args=[])

    return await asyncio.to_thread(_call)


async def _synth_gemini(text: str, *, voice: str, model: str) -> bytes:
    if not settings.gemini_api_key:
        raise TTSError("GEMINI_API_KEY ausente")

    def _call() -> bytes:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=settings.gemini_api_key)
        resp = client.models.generate_content(
            model=model,
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=voice,
                        ),
                    ),
                ),
            ),
        )
        try:
            part = resp.candidates[0].content.parts[0]
            raw = part.inline_data.data  # PCM 24kHz, 16-bit, mono
        except (AttributeError, IndexError, TypeError) as e:
            raise TTSError(f"resposta de áudio do Gemini inesperada: {e}") from e
        # PCM cru: informa formato explícito ao ffmpeg (não há header pra detectar).
        return _to_ogg_opus(raw, input_args=["-f", "s16le", "-ar", "24000", "-ac", "1"])

    return await asyncio.to_thread(_call)


async def synthesize(
    text: str, *, provider: str, voice: str | None = None, model: str | None = None,
) -> bytes:
    """Texto → OGG/Opus. `provider` = 'openai' | 'gemini'. Levanta TTSError."""
    if not (text or "").strip():
        raise TTSError("texto vazio")
    prov = (provider or settings.translator_tts_provider or "openai").lower()
    if prov == "gemini":
        return await _synth_gemini(
            text,
            voice=voice or settings.translator_tts_gemini_voice,
            model=model or settings.translator_tts_gemini_model,
        )
    return await _synth_openai(
        text,
        voice=voice or settings.translator_tts_openai_voice,
        model=model or settings.translator_tts_openai_model,
    )
