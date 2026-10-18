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


# ───────────── INDICAÇÃO de lugar ("me indica uma pizzaria") ─────────────
#
# Caso real (18/10/2026): pedido de "pizzaria delivery" respondido DE
# MEMÓRIA pelo modelo — Don Nero (é de Caruaru) e Maiale (não existe em
# Brasília), descobertos só quando o dono pediu o site. Indicação agora vem
# do Google e vai VERBATIM ao usuário: nome que não veio daqui não aparece.

SUGESTAO_MAX = 5
# Pede mais que o necessário pra ordenar por nota com volume: a cobrança da
# Text Search é por REQUISIÇÃO, não por resultado.
_SUGESTAO_PAGINA = 15
# Raio do viés de localização (não é restrição: "pizzaria em Goiânia" segue
# indo pra Goiânia, porque a cidade no texto pesa mais).
_SUGESTAO_RAIO_M = 20000.0
# Média bayesiana: 4,9 com 8 avaliações não passa à frente de 4,7 com 3 mil.
_PRIOR_NOTA = 4.3
_PRIOR_PESO = 50


def _score(p: dict) -> float:
    nota = float(p.get("rating") or 0)
    n = int(p.get("userRatingCount") or 0)
    return (nota * n + _PRIOR_NOTA * _PRIOR_PESO) / (n + _PRIOR_PESO)


def _vies(coords: str | None) -> dict | None:
    try:
        lat, lng = (float(x) for x in (coords or "").split(","))
    except ValueError:
        return None
    return {"circle": {"center": {"latitude": lat, "longitude": lng},
                       "radius": _SUGESTAO_RAIO_M}}


async def sugerir_lugares(query: str, *, coords: str | None = None,
                          entrega: bool = False) -> list[dict]:
    """Até SUGESTAO_MAX lugares ABERTOS (em operação) pra uma categoria
    ("pizzaria Brasília"), ordenados por nota ponderada pelo volume de
    avaliações. `entrega=True` pede o campo delivery (SKU mais caro —
    Enterprise + Atmosphere). Levanta PlacesError em falha de config/rede."""
    if settings.google_maps_api_key is None:
        raise PlacesError("GOOGLE_MAPS_API_KEY não configurada")
    mascara = _FIELD_MASK + (",places.delivery" if entrega else "")
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": settings.google_maps_api_key.get_secret_value(),
        "X-Goog-FieldMask": mascara,
    }
    body: dict = {"textQuery": query, "languageCode": "pt-BR", "regionCode": "BR",
                  "pageSize": _SUGESTAO_PAGINA}
    vies = _vies(coords)
    if vies:
        body["locationBias"] = vies
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(PLACES_SEARCH_ENDPOINT, json=body, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise PlacesError(f"Places request failed: {type(e).__name__}: {e}") from e
    lugares = [p for p in (resp.json().get("places") or [])
               if p.get("businessStatus", "OPERATIONAL") == "OPERATIONAL"]
    lugares.sort(key=_score, reverse=True)
    logger.info("sugerir_lugares: %d resultado(s) para %r", len(lugares), query)
    return lugares[:SUGESTAO_MAX]


def formatar_sugestoes_html(query: str, lugares: list[dict]) -> str:
    """Lista pronta pro Telegram (HTML). Telefone em <code>: tocar copia."""
    import html as _h

    def e(s) -> str:
        return _h.escape(str(s), quote=False)

    if not lugares:
        return (f"Não achei no Google nenhum lugar em funcionamento para "
                f"<i>{e(query)}</i>. Quer que eu tente com outras palavras "
                "ou outro bairro?")
    linhas = [f"📍 <b>Indicações do Google</b> para <i>{e(query)}</i> "
              "(ordem: nota × número de avaliações):"]
    for i, p in enumerate(lugares, 1):
        nome = (p.get("displayName") or {}).get("text") or "(sem nome)"
        bloco = [f"\n<b>{i}. {e(nome)}</b>"]
        if p.get("rating"):
            bloco.append(f"⭐ {p['rating']} ({p.get('userRatingCount') or 0} avaliações)")
        extras = []
        aberto = (p.get("currentOpeningHours") or {}).get("openNow")
        if aberto is not None:
            extras.append("🟢 aberto agora" if aberto else "🔴 fechado agora")
        if p.get("delivery") is True:
            extras.append("🛵 faz entrega")
        if extras:
            bloco.append(" · ".join(extras))
        if p.get("formattedAddress"):
            bloco.append(f"📍 {e(p['formattedAddress'])}")
        tel = p.get("nationalPhoneNumber") or p.get("internationalPhoneNumber")
        if tel:
            bloco.append(f"📞 <code>{e(tel)}</code>")
        if p.get("websiteUri"):
            bloco.append(f"🔗 {e(p['websiteUri'])}")
        if p.get("googleMapsUri"):
            bloco.append(f'🗺️ <a href="{_h.escape(p["googleMapsUri"])}">abrir no Maps</a>')
        linhas.append("\n".join(bloco))
    return "\n".join(linhas)
