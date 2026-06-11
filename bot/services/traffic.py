from __future__ import annotations

import html
import logging
import math
from dataclasses import dataclass, field
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

# Seleção de rota alternativa: uma alternativa só vale a pena se divergir de
# verdade da preferida. Pontos a menos de _OVERLAP_RADIUS_M do traçado da
# preferida contam como sobrepostos; rotas com mais de _MAX_OVERLAP_RATIO de
# sobreposição são descartadas (antes o critério era só o `summary` textual,
# que deixava passar rotas 95% idênticas).
_OVERLAP_RADIUS_M = 200.0
_MAX_OVERLAP_RATIO = 0.70
_OVERLAP_MAX_SAMPLES = 120


class TrafficError(Exception):
    pass


@dataclass(frozen=True)
class TrafficInfo:
    duration_minutes: int
    typical_minutes: int
    distance_km: float
    summary: str
    maps_url: str
    polyline: tuple[tuple[float, float], ...] = field(default=(), compare=False)


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


def _decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """Decodifica o overview_polyline do Directions (algoritmo de polyline
    codificada do Google). Retorna lista de (lat, lng)."""
    points: list[tuple[float, float]] = []
    index = lat = lng = 0
    n = len(encoded)
    while index < n:
        for is_lng in (False, True):
            shift = result = 0
            while True:
                if index >= n:
                    return points
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lng:
                lng += delta
            else:
                lat += delta
        points.append((lat / 1e5, lng / 1e5))
    return points


def _route_maps_url(
    pts: list[tuple[float, float]], origin: str, destination: str
) -> str | None:
    """Link do Maps que abre ~nesta rota específica, usando um ponto no meio
    do traçado (overview_polyline) como waypoint. Sem o waypoint o link seria
    genérico origem→destino e o Maps escolheria a rota dele. Custo zero de API:
    o polyline já vem na mesma resposta do Directions. None se não decodificar."""
    if len(pts) < 3:
        return None
    mid_lat, mid_lng = pts[len(pts) // 2]
    waypoint = f"{mid_lat:.5f},{mid_lng:.5f}"
    return (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote(origin, safe=',')}"
        f"&destination={quote(destination, safe=',')}"
        f"&waypoints={quote(waypoint, safe=',')}"
        "&travelmode=driving"
    )


def _sample_points(
    points: tuple[tuple[float, float], ...], max_n: int
) -> list[tuple[float, float]]:
    if len(points) <= max_n:
        return list(points)
    step = len(points) / max_n
    return [points[int(i * step)] for i in range(max_n)]


def route_overlap_ratio(
    candidate: tuple[tuple[float, float], ...],
    reference: tuple[tuple[float, float], ...],
) -> float:
    """Fração dos pontos de `candidate` a menos de _OVERLAP_RADIUS_M de algum
    ponto de `reference` (aprox. equiretangular — erro desprezível na escala
    urbana). Ambos os traçados são amostrados pra limitar o custo a
    O(_OVERLAP_MAX_SAMPLES²). Retorna 1.0 (totalmente sobreposta) se faltar
    geometria de algum lado."""
    if not candidate or not reference:
        return 1.0
    cand = _sample_points(candidate, _OVERLAP_MAX_SAMPLES)
    ref = _sample_points(reference, _OVERLAP_MAX_SAMPLES * 2)

    # Pré-projeta a referência em metros (plano local na latitude média).
    lat0 = math.radians(sum(p[0] for p in ref) / len(ref))
    m_per_deg_lat = 111_320.0
    m_per_deg_lng = 111_320.0 * math.cos(lat0)
    ref_m = [(p[0] * m_per_deg_lat, p[1] * m_per_deg_lng) for p in ref]

    near = 0
    r2 = _OVERLAP_RADIUS_M ** 2
    for lat, lng in cand:
        cx, cy = lat * m_per_deg_lat, lng * m_per_deg_lng
        if any((cx - rx) ** 2 + (cy - ry) ** 2 <= r2 for rx, ry in ref_m):
            near += 1
    return near / len(cand)


def _pick_alternative(
    preferred: TrafficInfo, candidates: list[TrafficInfo]
) -> TrafficInfo | None:
    """Escolhe a alternativa geometricamente mais distinta da preferida.
    Candidatas com sobreposição acima de _MAX_OVERLAP_RATIO são descartadas —
    melhor mostrar uma rota só do que duas praticamente iguais. Sem polyline
    (não deveria acontecer com o Directions), cai no critério antigo de
    `summary` distinto."""
    if not candidates:
        return None
    if not preferred.polyline:
        return next(
            (c for c in candidates if c.summary and c.summary != preferred.summary),
            None,
        )
    best: TrafficInfo | None = None
    best_overlap = _MAX_OVERLAP_RATIO
    for cand in candidates:
        overlap = route_overlap_ratio(cand.polyline, preferred.polyline)
        if overlap < best_overlap:
            best_overlap = overlap
            best = cand
    if best is not None:
        logger.debug(
            "alternative picked: overlap=%.2f summary=%s", best_overlap, best.summary
        )
    return best


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

    enc = (route.get("overview_polyline") or {}).get("points") or ""
    pts = _decode_polyline(enc) if enc else []

    # Prioridade: link específico desta rota (waypoint no meio do traçado) →
    # link pré-configurado (.env) → genérico origem→destino. O 1º faz cada
    # opção abrir a SUA rota; sem ele dois links seriam idênticos.
    route_url = _route_maps_url(pts, origin, destination)
    return TrafficInfo(
        duration_minutes=max(1, round(duration_traffic_s / 60)),
        typical_minutes=max(1, round(duration_typical_s / 60)),
        distance_km=round(distance_m / 1000, 1),
        summary=summary,
        maps_url=route_url or maps_url or fallback_url,
        polyline=tuple(pts),
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
        return pref, _pick_alternative(pref, infos[1:])

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
    return pref, _pick_alternative(pref, free_list)


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
        lines.append(f'🗺️ <a href="{html.escape(info.maps_url, quote=True)}">abrir no Maps</a>')
    return "\n".join(lines)


def _route_block(label: str, info: TrafficInfo, star: bool = False) -> list[str]:
    emoji = _severity_emoji(info.duration_minutes, info.typical_minutes)
    suffix = " ⭐" if star else ""
    via = f" via {html.escape(info.summary)}" if info.summary else ""
    lines = [
        f"{label} <b>~{info.duration_minutes} min</b> (típico: ~{info.typical_minutes}){suffix}",
        f"{emoji} {info.distance_km} km{via}",
    ]
    if info.maps_url:
        lines.append(
            f'🗺️ <a href="{html.escape(info.maps_url, quote=True)}">abrir no Maps</a>'
        )
    return lines


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
    lines += _route_block("➡️ <i>rota A:</i>", preferred, star=not alt_faster)
    lines.append("")
    lines += _route_block("➡️ <i>rota B:</i>", alternative, star=alt_faster)

    # Resumo em duas dimensões: tempo (rápida) e distância (curta) — podem
    # favorecer rotas diferentes, que é o trade-off que interessa.
    lines.append("")
    time_delta = abs(preferred.duration_minutes - alternative.duration_minutes)
    if time_delta > 0:
        nome = "Rota B" if alt_faster else "Rota A"
        lines.append(f"⚡ <b>Rota rápida:</b> {nome} (poupa ~{time_delta} min)")
    else:
        lines.append(f"⚡ <b>Rota rápida:</b> empate (~{preferred.duration_minutes} min)")

    dist_delta = abs(preferred.distance_km - alternative.distance_km)
    if dist_delta > 0:
        alt_shorter = alternative.distance_km < preferred.distance_km
        nome = "Rota B" if alt_shorter else "Rota A"
        dkm = f"{dist_delta:.1f}".rstrip("0").rstrip(".")
        lines.append(f"📏 <b>Rota curta:</b> {nome} (poupa ~{dkm} km)")
    else:
        lines.append("📏 <b>Rota curta:</b> mesma distância")
    return "\n".join(lines)
