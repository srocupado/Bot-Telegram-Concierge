from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

GEOCODING_ENDPOINT = "https://geocode.googleapis.com/v4beta/geocode/address"
PLACES_TEXT_SEARCH_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"


class GeocodingError(Exception):
    pass


@dataclass(frozen=True)
class GeocodeHit:
    coords: str            # "lat,lng"
    formatted_address: str


def _bias_rect(coords: str, half_deg: float = 0.45) -> dict:
    """Retângulo de viewport bias (~50 km) centrado em coords."""
    lat_s, lng_s = coords.split(",")
    lat, lng = float(lat_s), float(lng_s)
    return {
        "low": {"latitude": lat - half_deg, "longitude": lng - half_deg},
        "high": {"latitude": lat + half_deg, "longitude": lng + half_deg},
    }


def _bias_params(coords: str, half_deg: float = 0.45) -> dict[str, str]:
    """Mesmo retângulo no formato de query params da Geocoding API v4beta."""
    rect = _bias_rect(coords, half_deg)
    return {
        "locationBias.rectangle.low.latitude": str(rect["low"]["latitude"]),
        "locationBias.rectangle.low.longitude": str(rect["low"]["longitude"]),
        "locationBias.rectangle.high.latitude": str(rect["high"]["latitude"]),
        "locationBias.rectangle.high.longitude": str(rect["high"]["longitude"]),
    }


async def _geocode_address(
    client: httpx.AsyncClient,
    api_key: str,
    query: str,
    bias_coords: str | None,
) -> GeocodeHit | None:
    """Geocoding API (New) — endereços postais (rua + número + cidade)."""
    url = f"{GEOCODING_ENDPOINT}/{quote(query, safe='')}"
    params: dict[str, str] = {
        "regionCode": "br",
        "languageCode": "pt-BR",
        "key": api_key,
    }
    if bias_coords:
        params.update(_bias_params(bias_coords))

    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        safe_url = str(e.request.url).replace(api_key, "***")
        body = (e.response.text or "")[:400]
        raise GeocodingError(
            f"geocoding HTTP {e.response.status_code} url={safe_url} body={body!r}"
        ) from e
    except httpx.HTTPError as e:
        raise GeocodingError(f"geocoding request failed: {e}") from e

    data = resp.json()
    results = data.get("results") or []
    if not results:
        return None
    top = results[0]
    loc = top.get("location") or {}
    lat, lng = loc.get("latitude"), loc.get("longitude")
    if lat is None or lng is None:
        return None
    return GeocodeHit(
        coords=f"{lat},{lng}",
        formatted_address=top.get("formattedAddress") or query,
    )


async def _places_text_search(
    client: httpx.AsyncClient,
    api_key: str,
    query: str,
    bias_coords: str | None,
) -> GeocodeHit | None:
    """Places API (New) Text Search — POIs/prédios/órgãos por nome
    (ex.: 'Anexo IV da Câmara dos Deputados', 'Aeroporto JK').
    Complementa a Geocoding, que é só pra endereço postal."""
    body: dict = {
        "textQuery": query,
        "regionCode": "BR",
        "languageCode": "pt-BR",
        "maxResultCount": 1,
    }
    if bias_coords:
        body["locationBias"] = {"rectangle": _bias_rect(bias_coords)}

    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.location,places.formattedAddress,places.displayName",
        "Content-Type": "application/json",
    }
    try:
        resp = await client.post(PLACES_TEXT_SEARCH_ENDPOINT, json=body, headers=headers)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        text = (e.response.text or "")[:400]
        raise GeocodingError(
            f"places HTTP {e.response.status_code} body={text!r}"
        ) from e
    except httpx.HTTPError as e:
        raise GeocodingError(f"places request failed: {e}") from e

    data = resp.json()
    places = data.get("places") or []
    if not places:
        return None
    top = places[0]
    loc = top.get("location") or {}
    lat, lng = loc.get("latitude"), loc.get("longitude")
    if lat is None or lng is None:
        return None
    display = (top.get("displayName") or {}).get("text") or ""
    formatted = top.get("formattedAddress") or ""
    # Junta nome do POI + endereço pra deixar a confirmação informativa
    # ('Anexo IV — Praça dos Três Poderes...').
    if display and formatted and display.lower() not in formatted.lower():
        label = f"{display} — {formatted}"
    else:
        label = formatted or display or query
    return GeocodeHit(coords=f"{lat},{lng}", formatted_address=label)


# Tokens de logradouro/endereço (BR + padrões de Brasília). Se a query tiver
# um destes, é ENDEREÇO POSTAL → Geocoding primeiro. Senão é NOME DE LUGAR
# (loja, órgão, POI) → Places Text Search primeiro (a Geocoding devolveria só
# o centroide do bairro, foi o bug do 'AC Coelho materiais ... asa norte').
_LOGRADOURO_RE = re.compile(
    r"\b("
    r"rua|r|av|avenida|alameda|travessa|tv|rodovia|rod|estrada|"
    r"quadra|qd|qn|qi|ql|qs|qe|qnl|qng|qnm|sqn|sqs|sqsw|sqnw|sgan|sgas|"
    r"cln|cls|clnw|clsw|shin|shis|shcs|shcgn|shcgs|scs|scn|scrn|scln|sds|"
    r"setor|lote|bloco|cep"
    r")\b"
)


def _looks_like_postal_address(query: str) -> bool:
    n = unicodedata.normalize("NFKD", (query or "").lower())
    n = "".join(c for c in n if not unicodedata.combining(c))
    return _LOGRADOURO_RE.search(n) is not None


async def geocode(
    client: httpx.AsyncClient,
    api_key: str,
    query: str,
    bias_coords: str | None = None,
) -> GeocodeHit | None:
    """Resolve `query` em coords + endereço formatado.

    Escolhe a ferramenta certa pela CARA da query e usa a outra como fallback:
      • ENDEREÇO POSTAL (tem 'rua/av/quadra/SQN…') → Geocoding API primeiro
        (é a boa pra 'Av. Paulista 1000', 'SQN 410 Bl A');
      • NOME DE LUGAR (loja/órgão/POI, ex.: 'AC Coelho materiais de construção',
        'Aeroporto JK') → Places Text Search primeiro. A Geocoding, nesses
        casos, devolvia só o centroide do bairro e a rota ia pro lugar errado.
    bias_coords define o viewport (~50 km) pra priorizar resultados perto.
    """
    async def _via_geocoding() -> GeocodeHit | None:
        return await _geocode_address(client, api_key, query, bias_coords)

    async def _via_places() -> GeocodeHit | None:
        return await _places_text_search(client, api_key, query, bias_coords)

    if _looks_like_postal_address(query):
        ordem = [("Geocoding", _via_geocoding), ("Places", _via_places)]
    else:
        ordem = [("Places", _via_places), ("Geocoding", _via_geocoding)]

    for nome, metodo in ordem:
        try:
            hit = await metodo()
        except GeocodingError as e:
            logger.warning("geocode: %s falhou p/ %r (%s)", nome, query, e)
            continue
        if hit is not None:
            logger.info("geocode: resolvido via %s (%r)", nome, query)
            return hit

    logger.info("geocode: nada encontrado (Geocoding + Places) p/ %r", query)
    return None
