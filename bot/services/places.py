"""Consulta de estabelecimentos via Google Places API (New).

Devolve o dado OFICIAL do Google (telefone, endereço, horário de
funcionamento, status do negócio) — a mesma fonte do painel do Google
Maps/Search. Necessário porque busca web genérica (SearXNG/Firecrawl) cai em
agregadores com telefone secundário/desatualizado pra esse tipo de pergunta.

Reutiliza a GOOGLE_MAPS_API_KEY (a mesma do trânsito) — exige a **"Places API
(New)"** habilitada no projeto do Google Cloud.
"""
from __future__ import annotations

import logging

import httpx

from bot.config import settings

logger = logging.getLogger(__name__)

PLACES_SEARCH_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"

# FieldMask: só o que precisamos (Places New cobra por campo solicitado).
_FIELD_MASK = ",".join(
    [
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.regularOpeningHours.weekdayDescriptions",
        "places.currentOpeningHours.openNow",
        "places.websiteUri",
        "places.googleMapsUri",
        "places.rating",
        "places.userRatingCount",
        "places.businessStatus",
    ]
)

_MAX_PLACES = 3
_TIMEOUT_S = 20.0

_STATUS_PT = {
    "OPERATIONAL": "",
    "CLOSED_TEMPORARILY": "⚠️ fechado temporariamente",
    "CLOSED_PERMANENTLY": "⛔ fechado permanentemente",
}


class PlacesError(Exception):
    pass


async def buscar_local(query: str) -> str:
    """Busca um estabelecimento e devolve dados oficiais do Google (texto pronto
    pro LLM). Levanta PlacesError em falha de config/rede."""
    if settings.google_maps_api_key is None:
        raise PlacesError("GOOGLE_MAPS_API_KEY não configurada")

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": settings.google_maps_api_key.get_secret_value(),
        "X-Goog-FieldMask": _FIELD_MASK,
    }
    body = {"textQuery": query, "languageCode": "pt-BR", "regionCode": "BR"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(PLACES_SEARCH_ENDPOINT, json=body, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        # 403 normalmente = Places API (New) não habilitada no projeto.
        raise PlacesError(f"Places request failed: {e}") from e

    places = (resp.json().get("places") or [])[:_MAX_PLACES]
    if not places:
        return f"(nenhum estabelecimento encontrado para: {query})"

    logger.info("buscar_local: %d resultado(s) para %r", len(places), query)
    body = "\n\n".join(_format_place(p, i) for i, p in enumerate(places, 1))
    # Places API não expõe WhatsApp (vem do Perfil da Empresa, fora da API).
    # Nota pro agente: se o usuário pedir WhatsApp, ofereça procurar no site.
    body += (
        "\n\nNota: o WhatsApp do estabelecimento NÃO consta na Places API. Se o "
        "usuário pediu o WhatsApp e há site acima, você pode ler o site com "
        "buscar_web pra achar um link wa.me — ou avise que não está disponível."
    )
    return body


def _format_place(p: dict, idx: int) -> str:
    name = (p.get("displayName") or {}).get("text") or "(sem nome)"
    status = _STATUS_PT.get(p.get("businessStatus", "OPERATIONAL"), "")
    lines = [f"{idx}. {name}" + (f" — {status}" if status else "")]

    addr = p.get("formattedAddress")
    if addr:
        lines.append(f"📍 {addr}")

    # Monospace (crases): no Telegram, tocar em texto monospace COPIA ele
    # ("Copiado") → cola no discador. É o "toque captura o número" confiável
    # (a auto-detecção de telefone do Telegram é furada). Formato nacional
    # (sem +55) — basta pra ligar localmente.
    phone = p.get("nationalPhoneNumber") or p.get("internationalPhoneNumber")
    if phone:
        lines.append(f"📞 `{phone}`")

    open_now = (p.get("currentOpeningHours") or {}).get("openNow")
    if open_now is not None:
        lines.append("🟢 aberto agora" if open_now else "🔴 fechado agora")

    hours = (p.get("regularOpeningHours") or {}).get("weekdayDescriptions") or []
    if hours:
        lines.append("🕒 " + "; ".join(hours))

    site = p.get("websiteUri")
    if site:
        lines.append(f"🔗 {site}")

    maps = p.get("googleMapsUri")
    if maps:
        lines.append(f"🗺️ {maps}")

    rating = p.get("rating")
    if rating:
        count = p.get("userRatingCount") or 0
        lines.append(f"⭐ {rating} ({count} avaliações)")

    return "\n".join(lines)
