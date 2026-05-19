"""Google Maps Routes API (computeRoutes) — tempo de viagem com trânsito real."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
FIELD_MASK = "routes.duration,routes.staticDuration,routes.distanceMeters"


@dataclass
class RouteInfo:
    duration_seconds: int          # com trânsito (TRAFFIC_AWARE)
    static_duration_seconds: int   # sem trânsito (free flow)
    distance_meters: int

    @property
    def duration_minutes(self) -> int:
        return round(self.duration_seconds / 60)

    @property
    def static_minutes(self) -> int:
        return round(self.static_duration_seconds / 60)

    @property
    def delay_minutes(self) -> int:
        return self.duration_minutes - self.static_minutes

    @property
    def distance_km(self) -> float:
        return round(self.distance_meters / 1000.0, 1)


async def compute_route(
    api_key: str,
    origin: tuple[float, float],
    destination: tuple[float, float],
    *,
    travel_mode: str = "DRIVE",
) -> RouteInfo | None:
    body = {
        "origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
        "destination": {"location": {"latLng": {"latitude": destination[0], "longitude": destination[1]}}},
        "travelMode": travel_mode,
        "routingPreference": "TRAFFIC_AWARE",
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(ROUTES_URL, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
    except Exception:
        logger.exception("routes request failed")
        return None

    routes = data.get("routes") or []
    if not routes:
        logger.warning("routes response sem 'routes': %s", data)
        return None
    route = routes[0]
    try:
        dur = _parse_duration(route.get("duration", "0s"))
        static_dur = _parse_duration(route.get("staticDuration", route.get("duration", "0s")))
        dist = int(route.get("distanceMeters", 0))
    except Exception:
        logger.exception("routes parse failed: %s", route)
        return None
    return RouteInfo(duration_seconds=dur, static_duration_seconds=static_dur, distance_meters=dist)


def _parse_duration(s: str) -> int:
    """A API retorna '1234s'."""
    if s.endswith("s"):
        return int(float(s[:-1]))
    return int(float(s))
