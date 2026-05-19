from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from urllib.parse import quote, unquote_plus

import httpx

logger = logging.getLogger(__name__)

DIRECTIONS_ENDPOINT = "https://maps.googleapis.com/maps/api/directions/json"
USER_AGENT = (
    "Mozilla/5.0 (compatible; TelegramTravelsBot/0.2; "
    "+https://github.com/srocupado/telegram-travels)"
)
MAX_WAYPOINTS = 23
SHORT_LINK_HOSTS = ("maps.app.goo.gl", "goo.gl")


class TrafficError(Exception):
    pass


@dataclass(frozen=True)
class TrafficInfo:
    duration_minutes: int
    typical_minutes: int
    distance_km: float
    summary: str
    maps_url: str


async def parse_route_waypoints(client: httpx.AsyncClient, url: str) -> list[str]:
    if not url:
        return []
    expanded = url
    parsed = httpx.URL(url)
    if parsed.host in SHORT_LINK_HOSTS:
        try:
            resp = await client.get(url, follow_redirects=True)
            expanded = str(resp.url)
        except httpx.HTTPError as e:
            raise TrafficError(f"failed to expand short URL: {e}") from e

    marker = "/dir/"
    idx = expanded.find(marker)
    if idx < 0:
        return []
    tail = expanded[idx + len(marker):]
    # cut at viewport (@) or data= or query string
    cut_positions = [len(tail)]
    for sep in ("/@", "/data=", "?"):
        p = tail.find(sep)
        if p >= 0:
            cut_positions.append(p)
    tail = tail[: min(cut_positions)]

    raw_segments = [s for s in tail.split("/") if s]
    segments = [unquote_plus(s) for s in raw_segments]
    if len(segments) <= 2:
        return []
    middle = segments[1:-1]
    if len(middle) > MAX_WAYPOINTS:
        logger.warning(
            "route URL has %d waypoints, capping at %d", len(middle), MAX_WAYPOINTS
        )
        middle = middle[:MAX_WAYPOINTS]
    return middle


def _format_waypoints(waypoints: list[str]) -> str:
    parts = []
    for w in waypoints:
        parts.append(f"via:{w}")
    return "|".join(parts)


def _route_to_info(route: dict, origin: str, destination: str, maps_url: str) -> TrafficInfo:
    legs = route.get("legs") or []
    duration_traffic_s = 0
    duration_typical_s = 0
    distance_m = 0
    for leg in legs:
        dt = (leg.get("duration_in_traffic") or {}).get("value")
        d = (leg.get("duration") or {}).get("value")
        dist = (leg.get("distance") or {}).get("value")
        duration_traffic_s += int(dt if dt is not None else d or 0)
        duration_typical_s += int(d or 0)
        distance_m += int(dist or 0)

    summary = route.get("summary") or ""

    fallback_origin = quote(origin, safe=",")
    fallback_dest = quote(destination, safe=",")
    fallback_url = (
        f"https://www.google.com/maps/dir/?api=1"
        f"&origin={fallback_origin}&destination={fallback_dest}&travelmode=driving"
    )

    return TrafficInfo(
        duration_minutes=max(1, round(duration_traffic_s / 60)),
        typical_minutes=max(1, round(duration_typical_s / 60)),
        distance_km=round(distance_m / 1000, 1),
        summary=summary,
        maps_url=maps_url or fallback_url,
    )


async def fetch_traffic(
    client: httpx.AsyncClient,
    api_key: str,
    origin: str,
    destination: str,
    waypoints: list[str],
    maps_url: str = "",
    alternatives: bool = False,
) -> list[TrafficInfo]:
    """Retorna lista de rotas. Com alternatives=True, pode trazer 2-3."""
    params: dict[str, str] = {
        "origin": origin,
        "destination": destination,
        "departure_time": "now",
        "traffic_model": "best_guess",
        "mode": "driving",
        "key": api_key,
    }
    if waypoints:
        params["waypoints"] = _format_waypoints(waypoints)
    if alternatives:
        params["alternatives"] = "true"

    try:
        resp = await client.get(DIRECTIONS_ENDPOINT, params=params)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise TrafficError(f"directions request failed: {e}") from e

    data = resp.json()
    status = data.get("status")
    if status != "OK":
        msg = data.get("error_message") or status or "unknown error"
        raise TrafficError(f"directions API status={status}: {msg}")

    routes = data.get("routes") or []
    if not routes:
        raise TrafficError("directions API returned no routes")

    infos: list[TrafficInfo] = []
    for r in routes:
        if not (r.get("legs") or []):
            continue
        infos.append(_route_to_info(r, origin, destination, maps_url))
    if not infos:
        raise TrafficError("directions API returned only empty routes")
    return infos


async def fetch_traffic_with_alternative(
    client: httpx.AsyncClient,
    api_key: str,
    origin: str,
    destination: str,
    preferred_waypoints: list[str],
    maps_url: str = "",
) -> tuple[TrafficInfo, TrafficInfo | None]:
    """Retorna (preferida, alternativa distinta). Quando não há waypoints,
    pede alternatives no único request e usa a primeira como 'preferida' —
    economiza chamada."""
    import asyncio as _asyncio

    if not preferred_waypoints:
        infos = await fetch_traffic(
            client, api_key, origin, destination, [],
            maps_url=maps_url, alternatives=True,
        )
        pref = infos[0]
        alt = next(
            (i for i in infos[1:] if i.summary and i.summary != pref.summary),
            None,
        )
        return pref, alt

    pref_task = fetch_traffic(
        client, api_key, origin, destination, preferred_waypoints,
        maps_url=maps_url, alternatives=False,
    )
    free_task = fetch_traffic(
        client, api_key, origin, destination, [],
        maps_url=maps_url, alternatives=True,
    )
    pref_list, free_list = await _asyncio.gather(
        pref_task, free_task, return_exceptions=False
    )
    pref = pref_list[0]
    alt = next(
        (i for i in free_list if i.summary and i.summary != pref.summary),
        None,
    )
    return pref, alt


def _severity_emoji(duration: int, typical: int) -> str:
    if typical <= 0:
        return "🟢"
    delta_ratio = (duration - typical) / typical
    if delta_ratio < 0.10:
        return "🟢"
    if delta_ratio < 0.25:
        return "🟡"
    return "🔴"


def format_traffic_message(info: TrafficInfo, when_label: str) -> str:
    delta = info.duration_minutes - info.typical_minutes
    emoji = _severity_emoji(info.duration_minutes, info.typical_minutes)
    if delta > 0:
        delta_line = f"{emoji} +{delta} min de trânsito"
    elif delta < 0:
        delta_line = f"{emoji} {delta} min vs típico"
    else:
        delta_line = f"{emoji} sem trânsito acima do normal"

    via = f" via {html.escape(info.summary)}" if info.summary else ""
    label = html.escape(when_label)
    lines = [
        f"🚗 <b>Trânsito {label}</b>",
        "",
        f"⏱️ <b>~{info.duration_minutes} min agora</b> (típico: ~{info.typical_minutes} min)",
        delta_line,
        f"📏 {info.distance_km} km{via}",
    ]
    if info.maps_url:
        lines.append("")
        lines.append(f'<a href="{html.escape(info.maps_url, quote=True)}">abrir no Google Maps</a>')
    return "\n".join(lines)


def _route_block(label: str, info: TrafficInfo, star: bool = False) -> list[str]:
    emoji = _severity_emoji(info.duration_minutes, info.typical_minutes)
    suffix = " ⭐" if star else ""
    via = f" via {html.escape(info.summary)}" if info.summary else ""
    return [
        f"{label} <b>~{info.duration_minutes} min</b> (típico: ~{info.typical_minutes}){suffix}",
        f"{emoji} {info.distance_km} km{via}",
    ]


def format_traffic_message_dual(
    preferred: TrafficInfo,
    alternative: TrafficInfo | None,
    when_label: str,
) -> str:
    if alternative is None:
        return format_traffic_message(preferred, when_label)

    label = html.escape(when_label)
    lines = [f"🚗 <b>Trânsito {label}</b>", ""]
    alt_faster = alternative.duration_minutes < preferred.duration_minutes
    lines += _route_block("➡️ <i>sua rota:</i>", preferred, star=not alt_faster)
    lines.append("")
    lines += _route_block("➡️ <i>alternativa:</i>", alternative, star=alt_faster)
    if alt_faster:
        delta = preferred.duration_minutes - alternative.duration_minutes
        lines.append(f"\n💡 Alternativa pode poupar ~{delta} min")
    if preferred.maps_url:
        lines.append("")
        lines.append(
            f'<a href="{html.escape(preferred.maps_url, quote=True)}">abrir sua rota no Google Maps</a>'
        )
    return "\n".join(lines)
