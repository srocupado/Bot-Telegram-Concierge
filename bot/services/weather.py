"""Cliente Open-Meteo (não requer API key)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import httpx

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather codes → descrição PT-BR (simplificado)
WEATHER_CODE_PT = {
    0: "céu limpo",
    1: "predominantemente limpo",
    2: "parcialmente nublado",
    3: "nublado",
    45: "neblina",
    48: "neblina com geada",
    51: "garoa fraca",
    53: "garoa moderada",
    55: "garoa intensa",
    61: "chuva fraca",
    63: "chuva moderada",
    65: "chuva forte",
    71: "neve fraca",
    73: "neve moderada",
    75: "neve forte",
    80: "pancadas de chuva fracas",
    81: "pancadas de chuva moderadas",
    82: "pancadas de chuva fortes",
    95: "trovoada",
    96: "trovoada com granizo leve",
    99: "trovoada com granizo forte",
}


@dataclass
class WeatherForecast:
    day: date
    t_max: float
    t_min: float
    precip_prob: int
    precip_mm: float
    code: int

    @property
    def description(self) -> str:
        return WEATHER_CODE_PT.get(self.code, f"código {self.code}")


async def fetch_today(lat: float, lon: float, timezone: str = "America/Sao_Paulo") -> WeatherForecast | None:
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum",
        "timezone": timezone,
        "forecast_days": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(OPEN_METEO_URL, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception:
        logger.exception("open-meteo request failed")
        return None

    daily = data.get("daily") or {}
    times = daily.get("time") or []
    if not times:
        return None
    try:
        return WeatherForecast(
            day=date.fromisoformat(times[0]),
            t_max=float(daily["temperature_2m_max"][0]),
            t_min=float(daily["temperature_2m_min"][0]),
            precip_prob=int(daily.get("precipitation_probability_max", [0])[0] or 0),
            precip_mm=float(daily.get("precipitation_sum", [0])[0] or 0.0),
            code=int(daily["weather_code"][0]),
        )
    except (KeyError, IndexError, ValueError, TypeError):
        logger.exception("open-meteo parse failed: %s", data)
        return None
