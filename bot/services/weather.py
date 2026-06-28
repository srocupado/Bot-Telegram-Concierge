from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

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


@dataclass(frozen=True)
class DayWeather:
    date_iso: str
    temp_min_c: float
    temp_max_c: float
    precip_prob_pct: int
    precip_mm: float
    condition_emoji: str
    condition_label: str


async def fetch_forecast(
    client: httpx.AsyncClient,
    coords: str,
    tz: str = "America/Sao_Paulo",
    days: int = 7,
) -> list[DayWeather]:
    """Previsão diária pra `days` dias (Open-Meteo, máx 16). Mesmo endpoint do
    fetch_today_weather, só que com vários dias e a data de cada um."""
    try:
        lat_s, lng_s = coords.split(",", 1)
        lat = float(lat_s.strip())
        lng = float(lng_s.strip())
    except (ValueError, AttributeError) as e:
        raise WeatherError(f"invalid coords '{coords}': {e}") from e

    days = max(1, min(int(days or 7), 16))
    params = {
        "latitude": lat,
        "longitude": lng,
        "daily": (
            "temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max,precipitation_sum,weather_code"
        ),
        "timezone": tz,
        "forecast_days": days,
    }
    try:
        resp = await client.get(FORECAST_ENDPOINT, params=params)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise WeatherError(f"open-meteo request failed: {e}") from e

    daily = resp.json().get("daily") or {}
    times = daily.get("time") or []
    out: list[DayWeather] = []
    for i, day_iso in enumerate(times):
        try:
            emoji, label = _interpret_wmo(int(daily["weather_code"][i]))
            out.append(DayWeather(
                date_iso=day_iso,
                temp_min_c=float(daily["temperature_2m_min"][i]),
                temp_max_c=float(daily["temperature_2m_max"][i]),
                precip_prob_pct=int(daily["precipitation_probability_max"][i] or 0),
                precip_mm=float(daily["precipitation_sum"][i] or 0.0),
                condition_emoji=emoji,
                condition_label=label,
            ))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    if not out:
        raise WeatherError("open-meteo: sem dados de previsão")
    return out


_DIAS_SEMANA = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def format_week_forecast(days: list[DayWeather], hoje_iso: str | None = None) -> str:
    """Previsão dia a dia: uma linha por dia (emoji, dia da semana, data, faixa
    de temperatura, condição e % de chuva quando relevante)."""
    linhas = []
    for d in days:
        dt = date.fromisoformat(d.date_iso)
        dia = _DIAS_SEMANA[dt.weekday()]
        tmin, tmax = round(d.temp_min_c), round(d.temp_max_c)
        rain = f" · {d.precip_prob_pct}% chuva" if d.precip_prob_pct >= 30 else ""
        marca = " (hoje)" if d.date_iso == hoje_iso else ""
        linhas.append(
            f"{d.condition_emoji} {dia} {dt.strftime('%d/%m')}{marca}: "
            f"{tmin}°–{tmax}° {d.condition_label}{rain}"
        )
    return "\n".join(linhas)
