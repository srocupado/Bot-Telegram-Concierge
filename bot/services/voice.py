from __future__ import annotations

import asyncio
import logging

import google.generativeai as genai

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

Em qualquer outro caso, transcreva literalmente o que foi dito —
inclusive quando o usuário só MENCIONA trânsito ou congresso em uma
conversa, sem pedir explicitamente a informação naquele momento.
"""

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
