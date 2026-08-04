from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import httpx

from bot.config import settings

logger = logging.getLogger(__name__)

FORECAST_ENDPOINT = "https://api.open-meteo.com/v1/forecast"

# Google Weather (Maps Platform): fonte PRIMÁRIA desde 04/08/2026 — o dono
# comparou as duas previsões contra o céu real e o Google vinha acertando
# mais em Brasília. Open-Meteo segue como fallback: falha do Google não pode
# virar "sem previsão" (nem silêncio — a origem alternativa é dita na
# resposta). Reusa a GOOGLE_MAPS_API_KEY do trânsito; sem chave, tudo segue
# 100% Open-Meteo como antes. Free tier: 10k chamadas/mês (o bot usa <500).
GOOGLE_FORECAST_ENDPOINT = "https://weather.googleapis.com/v1/forecast/days:lookup"
# O forecast/days aceita no máximo 10 dias; pedido maior vai direto ao
# Open-Meteo (que cobre 16) — melhor a série inteira de uma fonte só do que
# emendar duas previsões diferentes no meio da lista.
_GOOGLE_MAX_DAYS = 10

FONTE_GOOGLE = "google"
FONTE_OM = "open-meteo"
FONTE_OM_FALLBACK = "open-meteo (fallback)"

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
    fonte: str = FONTE_OM


def _interpret_wmo(code: int) -> tuple[str, str]:
    return _WMO_MAP.get(code, ("🌡️", "condição indefinida"))


def _emoji_google(tipo: str) -> str:
    """Emoji pro WeatherCondition.type do Google. Checagens de precipitação
    vêm ANTES das genéricas (LIGHT_RAIN_SHOWERS tem que cair em chuva, não em
    'claro'). Tipo desconhecido → termômetro neutro, nunca exceção."""
    t = (tipo or "").upper()
    if "THUNDER" in t or "HAIL" in t:
        return "⛈️"
    if "SNOW" in t:
        return "🌨️"
    if "DRIZZLE" in t:
        return "🌦️"
    if "SHOWER" in t or "SCATTERED" in t or "CHANCE" in t:
        return "🌦️"
    if "RAIN" in t:
        return "🌧️"
    if "FOG" in t or "MIST" in t or "HAZE" in t:
        return "🌫️"
    if t == "CLEAR":
        return "☀️"
    if t == "MOSTLY_CLEAR":
        return "🌤️"
    if t == "PARTLY_CLOUDY":
        return "⛅"
    if t in ("MOSTLY_CLOUDY", "CLOUDY"):
        return "☁️"
    if "WIND" in t:
        return "🌬️"
    return "🌡️"


def _google_api_key() -> str | None:
    key = settings.google_maps_api_key
    if key is None:
        return None
    return key.get_secret_value() if hasattr(key, "get_secret_value") else str(key)


async def _fetch_forecast_google(
    client: httpx.AsyncClient, lat: float, lng: float, days: int, key: str,
) -> list[DayWeather]:
    """Previsão diária via Google Weather. Levanta WeatherError em qualquer
    problema — o caller decide o fallback. Parse defensivo dia a dia: entrada
    torta é pulada; zero dias aproveitáveis é erro (aciona o fallback), nunca
    lista vazia silenciosa."""
    params = {
        "key": key,
        "location.latitude": lat,
        "location.longitude": lng,
        "days": days,
        # A resposta é paginada (default menor que 10) — sem pageSize=days uma
        # semana viria pela metade e o resto exigiria nextPageToken.
        "pageSize": days,
        "languageCode": "pt-BR",   # description.text já vem em português
        "unitsSystem": "METRIC",
    }
    try:
        resp = await client.get(GOOGLE_FORECAST_ENDPOINT, params=params)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise WeatherError(f"google weather request failed: {e}") from e

    out: list[DayWeather] = []
    for fd in resp.json().get("forecastDays") or []:
        try:
            dd = fd["displayDate"]
            date_iso = f"{int(dd['year']):04d}-{int(dd['month']):02d}-{int(dd['day']):02d}"
            dia = fd.get("daytimeForecast") or {}
            noite = fd.get("nighttimeForecast") or {}
            cond = (dia.get("weatherCondition") or noite.get("weatherCondition") or {})
            desc = ((cond.get("description") or {}).get("text") or "").strip().lower()

            def _precip(periodo: dict) -> tuple[int, float]:
                p = periodo.get("precipitation") or {}
                prob = int((p.get("probability") or {}).get("percent") or 0)
                mm = float((p.get("qpf") or {}).get("quantity") or 0.0)
                return prob, mm

            prob_d, mm_d = _precip(dia)
            prob_n, mm_n = _precip(noite)
            out.append(DayWeather(
                date_iso=date_iso,
                temp_min_c=float(fd["minTemperature"]["degrees"]),
                temp_max_c=float(fd["maxTemperature"]["degrees"]),
                precip_prob_pct=max(prob_d, prob_n),
                precip_mm=mm_d + mm_n,
                condition_emoji=_emoji_google(cond.get("type") or ""),
                condition_label=desc or "condição indefinida",
                fonte=FONTE_GOOGLE,
            ))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("google weather: dia com formato inesperado (%s)", e)
            continue
    if not out:
        raise WeatherError("google weather: sem dados de previsão")
    logger.info("clima via Google Weather (%d dia(s))", len(out))
    return out


async def fetch_today_weather(
    client: httpx.AsyncClient,
    coords: str,
    tz: str = "America/Sao_Paulo",
) -> WeatherInfo:
    """Previsão de HOJE — mesma cascata Google→Open-Meteo do fetch_forecast
    (um dia é só o caso days=1)."""
    d = (await fetch_forecast(client, coords, tz, days=1))[0]
    return WeatherInfo(
        temp_min_c=d.temp_min_c,
        temp_max_c=d.temp_max_c,
        precip_prob_pct=d.precip_prob_pct,
        precip_mm=d.precip_mm,
        condition_emoji=d.condition_emoji,
        condition_label=d.condition_label,
        fonte=d.fonte,
    )


def format_weather_line(w: WeatherInfo) -> str:
    tmin = round(w.temp_min_c)
    tmax = round(w.temp_max_c)
    rain = ""
    if w.precip_prob_pct >= 30:
        rain = f", {w.precip_prob_pct}% chuva"
    linha = f"{w.condition_emoji} {tmin}°–{tmax}°{rain} ({w.condition_label})"
    if w.fonte == FONTE_OM_FALLBACK:
        # Origem alternativa é DITA (regra do projeto): o dono escolheu o
        # Google por precisão — dado do plano B sem aviso pareceria do A.
        linha += " · fonte: Open-Meteo (Google indisponível)"
    return linha


@dataclass(frozen=True)
class DayWeather:
    date_iso: str
    temp_min_c: float
    temp_max_c: float
    precip_prob_pct: int
    precip_mm: float
    condition_emoji: str
    condition_label: str
    fonte: str = FONTE_OM


async def fetch_forecast(
    client: httpx.AsyncClient,
    coords: str,
    tz: str = "America/Sao_Paulo",
    days: int = 7,
) -> list[DayWeather]:
    """Previsão diária pra `days` dias. Cascata: Google Weather (primária,
    quando há chave e days≤10) → Open-Meteo (fallback explícito, ou fonte
    única sem chave/pedido longo). Falha do Google nunca vira 'sem previsão':
    cai no Open-Meteo com a origem marcada (`fonte`), e só a falha DUPLA
    levanta WeatherError — que os callers já reportam em voz alta."""
    try:
        lat_s, lng_s = coords.split(",", 1)
        lat = float(lat_s.strip())
        lng = float(lng_s.strip())
    except (ValueError, AttributeError) as e:
        raise WeatherError(f"invalid coords '{coords}': {e}") from e

    days = max(1, min(int(days or 7), 16))
    fonte_om = FONTE_OM
    key = _google_api_key()
    if key and days <= _GOOGLE_MAX_DAYS:
        try:
            return await _fetch_forecast_google(client, lat, lng, days, key)
        except WeatherError as e:
            # Fallback é DITO (log + fonte na resposta), nunca silencioso.
            logger.warning("clima: Google Weather falhou (%s) — usando Open-Meteo", e)
            fonte_om = FONTE_OM_FALLBACK

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
                fonte=fonte_om,
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
    if any(d.fonte == FONTE_OM_FALLBACK for d in days):
        linhas.append("(fonte: Open-Meteo — Google Weather indisponível agora)")
    return "\n".join(linhas)
