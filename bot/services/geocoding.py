from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

GEOCODING_ENDPOINT = "https://geocode.googleapis.com/v4beta/geocode/address"


class GeocodingError(Exception):
    pass


@dataclass(frozen=True)
class GeocodeHit:
    coords: str            # "lat,lng"
    formatted_address: str


def _bias_params(coords: str, half_deg: float = 0.45) -> dict[str, str]:
    lat_s, lng_s = coords.split(",")
    lat, lng = float(lat_s), float(lng_s)
    return {
        "locationBias.rectangle.low.latitude": f"{lat - half_deg}",
        "locationBias.rectangle.low.longitude": f"{lng - half_deg}",
        "locationBias.rectangle.high.latitude": f"{lat + half_deg}",
        "locationBias.rectangle.high.longitude": f"{lng + half_deg}",
    }


async def geocode(
    client: httpx.AsyncClient,
    api_key: str,
    query: str,
    bias_coords: str | None = None,
) -> GeocodeHit | None:
    """Resolve `query` para coords + endereço formatado via Geocoding API (New) v4beta.

    bias_coords ("lat,lng") é usado como viewport bias (~50km) pra priorizar
    resultados próximos do usuário. regionCode=br dá bias regional adicional.
    """
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
