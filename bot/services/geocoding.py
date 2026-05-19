from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

GEOCODING_ENDPOINT = "https://maps.googleapis.com/maps/api/geocoding/json"


class GeocodingError(Exception):
    pass


@dataclass(frozen=True)
class GeocodeHit:
    coords: str            # "lat,lng"
    formatted_address: str


def _bounds_around(coords: str, half_deg: float = 0.45) -> str:
    lat_s, lng_s = coords.split(",")
    lat, lng = float(lat_s), float(lng_s)
    sw = f"{lat - half_deg},{lng - half_deg}"
    ne = f"{lat + half_deg},{lng + half_deg}"
    return f"{sw}|{ne}"


async def geocode(
    client: httpx.AsyncClient,
    api_key: str,
    query: str,
    bias_coords: str | None = None,
) -> GeocodeHit | None:
    """Resolve `query` para coords + endereço formatado.

    bias_coords ("lat,lng") é usado como viewport bias (bounds ~50km)
    pra priorizar resultados próximos do usuário. region=br dá bias
    regional adicional.
    """
    params: dict[str, str] = {
        "address": query,
        "region": "br",
        "language": "pt-BR",
        "key": api_key,
    }
    if bias_coords:
        params["bounds"] = _bounds_around(bias_coords)

    try:
        resp = await client.get(GEOCODING_ENDPOINT, params=params)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        safe_url = str(e.request.url).replace(api_key, "***")
        body = (e.response.text or "")[:400]
        # 404 nesse endpoint normalmente = Geocoding API não habilitada
        # no projeto Cloud, ou API key com restrição que bloqueia Geocoding.
        raise GeocodingError(
            f"geocoding HTTP {e.response.status_code} url={safe_url} body={body!r}"
        ) from e
    except httpx.HTTPError as e:
        raise GeocodingError(f"geocoding request failed: {e}") from e

    data = resp.json()
    status = data.get("status")
    if status == "ZERO_RESULTS":
        return None
    if status != "OK":
        msg = data.get("error_message") or status or "unknown"
        raise GeocodingError(f"geocoding API status={status}: {msg}")

    results = data.get("results") or []
    if not results:
        return None
    top = results[0]
    loc = (top.get("geometry") or {}).get("location") or {}
    lat, lng = loc.get("lat"), loc.get("lng")
    if lat is None or lng is None:
        return None
    return GeocodeHit(
        coords=f"{lat},{lng}",
        formatted_address=top.get("formatted_address") or query,
    )
