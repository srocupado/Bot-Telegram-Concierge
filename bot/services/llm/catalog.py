"""Catálogo de modelos por provider via Models API (REST) — DINÂMICO: modelos
novos aparecem sozinhos, sem mexer no código. Usado por /provider e /dou_provider
pra listar e validar modelos. É metadata — NÃO gasta token de inferência.

REST (não os SDKs) de propósito: mesmo contrato em sandbox e prod, sem depender
de campo específico de cada SDK.
"""
from __future__ import annotations

import logging

import httpx

from bot.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0

# Filtros pra mostrar só modelos de CHAT (texto) — fora imagem/áudio/embedding.
_GEMINI_EXCLUDE = ("image", "tts", "robotics", "embedding", "aqa", "omni",
                   "customtools", "learnlm")
_OPENAI_PREFIX = ("gpt-", "o1", "o3", "o4", "chatgpt")
_OPENAI_EXCLUDE = ("embedding", "tts", "whisper", "dall-e", "moderation",
                   "realtime", "image", "audio", "transcribe", "search",
                   "-instruct", "codex")
# Modelos OpenAI que ACEITAM áudio de entrada (STT / speech).
_OPENAI_AUDIO = ("whisper", "transcribe", "audio", "realtime")

# Modalidades de ENTRADA suportadas:
#   "text"   → modelos de chat (texto)            — usado por /provider
#   "vision" → aceitam imagem na entrada          — usado por /provider_visao
#   "audio"  → aceitam áudio na entrada (STT)     — usado por /voice


async def list_models(provider: str, modality: str = "text") -> list[tuple[str, str]]:
    """[(id, display_name)] dos modelos do provider que aceitam `modality` na
    entrada. [] se faltar a chave, a API falhar, ou o provider não ter nenhum
    modelo dessa modalidade (o caller mostra mensagem honesta)."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            if provider == "anthropic":
                return await _anthropic(c, modality)
            if provider == "gemini":
                return await _gemini(c, modality)
            if provider == "openai":
                return await _openai(c, modality)
    except Exception:
        logger.exception("catalog: falha listando modelos de %s", provider)
    return []


async def _anthropic(c: httpx.AsyncClient, modality: str) -> list[tuple[str, str]]:
    # Claude aceita texto e imagem, mas NÃO áudio na entrada.
    if modality == "audio":
        return []
    if not settings.anthropic_api_key:
        return []
    out: list[tuple[str, str]] = []
    after = None
    for _ in range(5):  # paginação (poucos modelos, mas por garantia)
        params = {"limit": 100}
        if after:
            params["after_id"] = after
        r = await c.get(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": settings.anthropic_api_key,
                     "anthropic-version": "2023-06-01"},
            params=params,
        )
        r.raise_for_status()
        d = r.json()
        for m in d.get("data", []):
            mid = m.get("id", "")
            if mid.startswith("claude-"):
                out.append((mid, m.get("display_name") or mid))
        if not d.get("has_more"):
            break
        after = d.get("last_id")
    return out


async def _gemini(c: httpx.AsyncClient, modality: str) -> list[tuple[str, str]]:
    # Os modelos de chat do Gemini são multimodais: aceitam texto, imagem E
    # áudio na entrada. Então o mesmo conjunto serve às três modalidades.
    if not settings.gemini_api_key:
        return []
    r = await c.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": settings.gemini_api_key, "pageSize": 200},
    )
    r.raise_for_status()
    out: list[tuple[str, str]] = []
    for m in r.json().get("models", []):
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        mid = (m.get("name") or "").replace("models/", "")
        if not mid.startswith("gemini") or any(x in mid for x in _GEMINI_EXCLUDE):
            continue
        out.append((mid, m.get("displayName") or mid))
    return out


async def _openai(c: httpx.AsyncClient, modality: str) -> list[tuple[str, str]]:
    if not settings.openai_api_key:
        return []
    r = await c.get(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {settings.openai_api_key}"},
    )
    r.raise_for_status()
    ids = [m.get("id", "") for m in r.json().get("data", [])]
    out: list[tuple[str, str]] = []
    if modality == "audio":
        # Entrada de áudio (STT/speech): whisper, *-transcribe, *-audio, realtime.
        for mid in ids:
            if any(x in mid for x in _OPENAI_AUDIO):
                out.append((mid, mid))
    else:
        for mid in ids:
            if not mid.startswith(_OPENAI_PREFIX) or any(x in mid for x in _OPENAI_EXCLUDE):
                continue
            # Visão: descarta os text-only (gpt-3.5 não enxerga imagem).
            if modality == "vision" and mid.startswith("gpt-3.5"):
                continue
            out.append((mid, mid))
    out.sort()
    return out
