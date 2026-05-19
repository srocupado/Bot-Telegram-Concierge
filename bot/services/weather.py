from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

FORECAST_ENDPOINT = "https://api.open-meteo.com/v1/forecast"

# WMO Weather interpretation codes → (emoji, label pt-BR)
# https://open-meteo.com/en/docs#weathervariables
_WMO_MAP: dict[int, tuple[str, str]] = {
    0: ("☀️", "céu limpo"),
    1: ("🌤️", "predominantemente limpo"),
    2: ("⛅", "parcialmente nublado"),
    3: ("☁️", "nublado"),
    45: ("🌫️", "neblina"),
    48: ("🌫️", "neblina com geada"),
    51: ("🌦️", "garoa leve"),
    53: ("🌦️", "garoa moderada"),
    55: ("🌦️", "garoa intensa"),
    56: ("🌦️", "garoa congelante leve"),
    57: ("🌦️", "garoa congelante intensa"),
    61: ("🌧️", "chuva leve"),
    63: ("🌧️", "chuva moderada"),
    65: ("🌧️", "chuva forte"),
    66: ("🌧️", "chuva congelante leve"),
    67: ("🌧️", "chuva congelante forte"),
    71: ("🌨️", "neve leve"),
    73: ("🌨️", "neve moderada"),
    75: ("🌨️", "neve forte"),
    77: ("🌨️", "grãos de neve"),
    80: ("🌦️", "pancadas leves"),
    81: ("🌦️", "pancadas moderadas"),
    82: ("🌧️", "pancadas fortes"),
    85: ("🌨️", "pancadas de neve leves"),
    86: ("🌨️", "pancadas de neve fortes"),
    95: ("⛈️", "tempestade"),
    96: ("⛈️", "tempestade com granizo leve"),
    99: ("⛈️", "tempestade com granizo forte"),
}


class WeatherError(Exception):
    pass


@dataclass(frozen=True)
class WeatherInfo:
    temp_min_c: float
    temp_max_c: float
    precip_prob_pct: int
    precip_mm: float
    condition_emoji: str
    condition_label: str


def _interpret_wmo(code: int) -> tuple[str, str]:
    return _WMO_MAP.get(code, ("🌡️", "condição indefinida"))


async def fetch_today_weather(
    client: httpx.AsyncClient,
    coords: str,
    tz: str = "America/Sao_Paulo",
) -> WeatherInfo:
    try:
        lat_s, lng_s = coords.split(",", 1)
        lat = float(lat_s.strip())
        lng = float(lng_s.strip())
    except (ValueError, AttributeError) as e:
        raise WeatherError(f"invalid coords '{coords}': {e}") from e

    params = {
        "latitude": lat,
        "longitude": lng,
        "daily": (
            "temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max,precipitation_sum,weather_code"
        ),
        "timezone": tz,
        "forecast_days": 1,
    }
    try:
        resp = await client.get(FORECAST_ENDPOINT, params=params)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise WeatherError(f"open-meteo request failed: {e}") from e

    data = resp.json()
    daily = data.get("daily") or {}
    try:
        tmin = float(daily["temperature_2m_min"][0])
        tmax = float(daily["temperature_2m_max"][0])
        pprob = int(daily["precipitation_probability_max"][0] or 0)
        pmm = float(daily["precipitation_sum"][0] or 0.0)
        code = int(daily["weather_code"][0])
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise WeatherError(f"open-meteo parse error: {e}") from e

    emoji, label = _interpret_wmo(code)
    return WeatherInfo(
        temp_min_c=tmin,
        temp_max_c=tmax,
        precip_prob_pct=pprob,
        precip_mm=pmm,
        condition_emoji=emoji,
        condition_label=label,
    )


def format_weather_line(w: WeatherInfo) -> str:
    tmin = round(w.temp_min_c)
    tmax = round(w.temp_max_c)
    rain = ""
    if w.precip_prob_pct >= 30:
        rain = f", {w.precip_prob_pct}% chuva"
    return f"{w.condition_emoji} {tmin}°–{tmax}°{rain} ({w.condition_label})"
