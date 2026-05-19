"""Compõe a mensagem de trânsito (casa↔trabalho) + clima."""
from __future__ import annotations

import logging

from bot.config import settings
from bot.services.traffic import RouteInfo, compute_route
from bot.services.weather import WeatherForecast, fetch_today

logger = logging.getLogger(__name__)


async def build_traffic_briefing() -> str:
    if not settings.home_coords or not settings.work_coords:
        return "⚠️ HOME_COORDS e WORK_COORDS não configurados."

    home = settings.parsed_coords(settings.home_coords)
    work = settings.parsed_coords(settings.work_coords)

    home_to_work: RouteInfo | None = None
    work_to_home: RouteInfo | None = None
    if settings.google_maps_api_key:
        home_to_work = await compute_route(settings.google_maps_api_key, home, work)
        work_to_home = await compute_route(settings.google_maps_api_key, work, home)
    else:
        logger.warning("GOOGLE_MAPS_API_KEY ausente; pulando trânsito")

    weather: WeatherForecast | None = None
    if settings.weather_lat is not None and settings.weather_lon is not None:
        weather = await fetch_today(settings.weather_lat, settings.weather_lon, settings.timezone)

    return _render(home_to_work, work_to_home, weather)


def _render(htw: RouteInfo | None, wth: RouteInfo | None, weather) -> str:
    lines = ["☀️ *Briefing do dia*\n"]

    if weather:
        rain = ""
        if weather.precip_prob >= 30 or weather.precip_mm > 0.5:
            rain = f" — 🌧 {weather.precip_prob}% chuva ({weather.precip_mm:.1f} mm)"
        lines.append(
            f"🌡 *Clima*: {weather.description}, "
            f"máx {weather.t_max:.0f}°C / mín {weather.t_min:.0f}°C{rain}"
        )
    else:
        lines.append("🌡 *Clima*: indisponível")

    lines.append("\n🚗 *Trânsito*")
    if htw:
        lines.append(_fmt_route("Casa → Trabalho", htw))
    else:
        lines.append("• Casa → Trabalho: indisponível")
    if wth:
        lines.append(_fmt_route("Trabalho → Casa", wth))
    else:
        lines.append("• Trabalho → Casa: indisponível")

    return "\n".join(lines)


def _fmt_route(label: str, r: RouteInfo) -> str:
    delay = r.delay_minutes
    if delay <= 2:
        emoji = "🟢"
    elif delay <= 8:
        emoji = "🟡"
    else:
        emoji = "🔴"
    base = f"• {label}: {emoji} {r.duration_minutes} min ({r.distance_km} km)"
    if delay > 0:
        base += f" — +{delay} min vs. fluxo livre"
    return base
