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
GOOGLE_HOURLY_ENDPOINT = "https://weather.googleapis.com/v1/forecast/hours:lookup"
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


def tendencia_temperatura(days: list[DayWeather]) -> str | None:
    """Linha da tendência de temperatura da semana — SEMPRE presente
    (esquentando, esfriando, sobe-e-desce ou estável). Pedido do dono em
    09/08/2026, substituindo a régua de +2°C de 04/08: 'relata sempre se vai
    esquentar ou esfriar'. Compara em graus ARREDONDADOS (o que o dono lê nas
    linhas do bloco) — 0,4°C de diferença não é tendência, é ruído de modelo.
    None só com menos de 2 dias de previsão."""
    if len(days) < 2:
        return None
    hoje = round(days[0].temp_max_c)
    futuros = days[1:]
    pico = max(futuros, key=lambda d: d.temp_max_c)
    vale = min(futuros, key=lambda d: d.temp_max_c)
    sobe = round(pico.temp_max_c) - hoje >= 1
    desce = hoje - round(vale.temp_max_c) >= 1

    def _quando(d: DayWeather) -> str:
        dt = date.fromisoformat(d.date_iso)
        return f"{_DIAS_SEMANA[dt.weekday()]} {dt.strftime('%d/%m')}"

    if sobe and desce:
        # Sobe E desce na janela: descreve na ordem em que acontece.
        primeiro, depois = ((pico, vale) if pico.date_iso < vale.date_iso
                            else (vale, pico))
        verbo1 = "sobe até" if primeiro is pico else "cai até"
        verbo2 = "cai até" if depois is vale else "sobe até"
        return (f"🌡️ Semana: hoje máxima de {hoje}°, {verbo1} "
                f"{round(primeiro.temp_max_c)}° na {_quando(primeiro)}, depois "
                f"{verbo2} {round(depois.temp_max_c)}° na {_quando(depois)}.")
    if sobe:
        return (f"🌡️ Esquentando: hoje máxima de {hoje}°, subindo até "
                f"{round(pico.temp_max_c)}° na {_quando(pico)}.")
    if desce:
        return (f"🌡️ Esfriando: hoje máxima de {hoje}°, caindo até "
                f"{round(vale.temp_max_c)}° na {_quando(vale)}.")
    return f"🌡️ Máximas estáveis a semana toda, na casa dos {hoje}°."


@dataclass(frozen=True)
class RainNowcast:
    prob_pct: int
    condition_label: str
    fonte: str = FONTE_OM


def chuva_transicao(prob_pct: int, alerta_ativo: bool, limiar_pct: int) -> str | None:
    """Máquina de estados do alerta de chuva: 'disparar' no cruzamento do
    limiar, 'rearmar' quando a chuva passou, None no resto — inclusive chuva
    contínua já avisada (1 alerta por evento, nunca metralhadora). O rearme
    tem histerese de 20 pontos: probabilidade oscilando em volta do limiar
    (58→62→57→63…) não pode redisparar a cada oscilação."""
    if not alerta_ativo and prob_pct >= limiar_pct:
        return "disparar"
    if alerta_ativo and prob_pct <= max(0, limiar_pct - 20):
        return "rearmar"
    return None


async def fetch_rain_next_hour(client: httpx.AsyncClient, coords: str) -> RainNowcast:
    """Probabilidade de chuva na PRÓXIMA hora — mesma cascata do forecast
    (Google horário → Open-Meteo horário). Olha as 2 primeiras horas e pega o
    MÁXIMO: a "hora corrente" devolvida pode estar quase no fim, e chuva às
    XX:55 é tão 'próxima hora' quanto às XX+1:10."""
    try:
        lat_s, lng_s = coords.split(",", 1)
        lat = float(lat_s.strip())
        lng = float(lng_s.strip())
    except (ValueError, AttributeError) as e:
        raise WeatherError(f"invalid coords '{coords}': {e}") from e

    fonte_om = FONTE_OM
    key = _google_api_key()
    if key:
        try:
            params = {
                "key": key,
                "location.latitude": lat,
                "location.longitude": lng,
                "hours": 2,
                "pageSize": 2,
                "languageCode": "pt-BR",
                "unitsSystem": "METRIC",
            }
            try:
                resp = await client.get(GOOGLE_HOURLY_ENDPOINT, params=params)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                raise WeatherError(f"google hourly request failed: {e}") from e
            melhor: tuple[int, str] | None = None
            for h in (resp.json().get("forecastHours") or [])[:2]:
                p = int(((h.get("precipitation") or {}).get("probability") or {})
                        .get("percent") or 0)
                cond = h.get("weatherCondition") or {}
                desc = ((cond.get("description") or {}).get("text") or "").strip().lower()
                if melhor is None or p > melhor[0]:
                    melhor = (p, desc or "condição indefinida")
            if melhor is None:
                raise WeatherError("google hourly: resposta vazia")
            return RainNowcast(melhor[0], melhor[1], FONTE_GOOGLE)
        except WeatherError as e:
            logger.warning("chuva: Google horário falhou (%s) — usando Open-Meteo", e)
            fonte_om = FONTE_OM_FALLBACK

    params = {
        "latitude": lat,
        "longitude": lng,
        "hourly": "precipitation_probability,weather_code",
        "forecast_hours": 2,
        "timezone": "auto",
    }
    try:
        resp = await client.get(FORECAST_ENDPOINT, params=params)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise WeatherError(f"open-meteo hourly request failed: {e}") from e
    hourly = resp.json().get("hourly") or {}
    probs = hourly.get("precipitation_probability") or []
    codes = hourly.get("weather_code") or []
    melhor = None
    for i, p in enumerate(probs[:2]):
        p = int(p or 0)
        if melhor is None or p > melhor[0]:
            code = int(codes[i]) if i < len(codes) and codes[i] is not None else -1
            melhor = (p, _interpret_wmo(code)[1])
    if melhor is None:
        raise WeatherError("open-meteo hourly: sem dados")
    return RainNowcast(melhor[0], melhor[1], fonte_om)


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
