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

# Modelo de fallback quando o principal (gemini-2.5-flash) está sobrecarregado
# (503 UNAVAILABLE). O lite costuma ter mais capacidade disponível.
_STT_FALLBACK_MODEL = "gemini-2.5-flash-lite"


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


async def transcribe(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    """Transcreve áudio usando Gemini multimodal via SDK google-genai (HTTP).

    Usa o mesmo SDK do chat (`google-genai`) em vez do antigo
    `google-generativeai`, que fala gRPC e pendura em alguns ambientes
    (ARM/docker). Lança VoiceTranscribeError em falhas de API.
    Retorna string vazia se o áudio não tem fala detectável.

    Em 503/sobrecarga do modelo, tenta de novo com backoff e cai num modelo
    de fallback (flash-lite) se o principal seguir congestionado.
    """
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
    if _STT_FALLBACK_MODEL not in models:
        models.append(_STT_FALLBACK_MODEL)

    last_exc: Exception | None = None
    for model in models:
        for attempt in range(3):
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
