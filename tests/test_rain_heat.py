"""Alerta de chuva na próxima hora + tendência de calor (dono, 04/08/2026).

Chuva: vigia a cada 20min (6h–23h), dispara quando a probabilidade cruza o
limiar (60%), UM alerta por evento (histerese no rearme — probabilidade
oscilando em volta do limiar não pode virar metralhadora). Fonte: Google
horário → Open-Meteo, mesma cascata do forecast.

Calor: no briefing, "Esquentando" quando a máxima de algum dia da semana
supera a de hoje em ≥2°C — semana estável fica em silêncio.
"""
from __future__ import annotations

import asyncio

import httpx
import respx
from pydantic import SecretStr

from bot.services import weather

_COORDS = "-15.79,-47.88"


# ───────────────────────── máquina de estados do alerta ─────────────────────

def test_transicao_dispara_no_cruzamento() -> None:
    assert weather.chuva_transicao(60, False, 60) == "disparar"
    assert weather.chuva_transicao(95, False, 60) == "disparar"


def test_transicao_um_alerta_por_evento() -> None:
    # Chuva contínua já avisada: silêncio (nem dispara, nem rearma).
    assert weather.chuva_transicao(80, True, 60) is None
    assert weather.chuva_transicao(61, True, 60) is None


def test_transicao_histerese_no_rearme() -> None:
    # Oscilação em volta do limiar NÃO rearma (58 não é "chuva passou").
    assert weather.chuva_transicao(58, True, 60) is None
    assert weather.chuva_transicao(41, True, 60) is None
    # Só com folga (limiar - 20) o evento acaba e o alerta rearma.
    assert weather.chuva_transicao(40, True, 60) == "rearmar"
    assert weather.chuva_transicao(0, True, 60) == "rearmar"


def test_transicao_abaixo_do_limiar_sem_alerta_ativo() -> None:
    assert weather.chuva_transicao(59, False, 60) is None
    assert weather.chuva_transicao(0, False, 60) is None


# ───────────────────────────── tendência de calor ───────────────────────────

def _dia(iso: str, tmax: float) -> weather.DayWeather:
    return weather.DayWeather(iso, 15, tmax, 0, 0, "🌤️", "limpo")


# Sem régua (dono, 09/08/2026): a tendência é relatada SEMPRE — esquentando,
# esfriando, sobe-e-desce ou estável.

def test_tendencia_esquentando() -> None:
    dias = [_dia("2026-08-05", 26), _dia("2026-08-06", 27), _dia("2026-08-08", 28)]
    linha = weather.tendencia_temperatura(dias)
    assert "Esquentando" in linha
    assert "26°" in linha and "28°" in linha
    assert "sáb 08/08" in linha, "aponta O DIA do pico, não só o valor"


def test_tendencia_esfriando_tambem_e_relatada() -> None:
    """O caso de 09/08/2026: hoje 33° no topo da semana e o briefing mudo."""
    dias = [_dia("2026-08-09", 33), _dia("2026-08-10", 32), _dia("2026-08-12", 29)]
    linha = weather.tendencia_temperatura(dias)
    assert "Esfriando" in linha
    assert "33°" in linha and "29°" in linha and "qua 12/08" in linha


def test_tendencia_sobe_e_desce_na_ordem_em_que_acontece() -> None:
    dias = [_dia("2026-08-05", 28), _dia("2026-08-06", 32), _dia("2026-08-08", 24)]
    linha = weather.tendencia_temperatura(dias)
    assert "sobe até 32°" in linha and "cai até 24°" in linha
    assert linha.index("sobe") < linha.index("depois") < linha.index("cai")


def test_tendencia_estavel_agora_fala() -> None:
    dias = [_dia("2026-08-05", 26), _dia("2026-08-06", 26.4), _dia("2026-08-07", 26)]
    linha = weather.tendencia_temperatura(dias)
    assert "estáveis" in linha and "26°" in linha
    # 0,4° não vira "esquentando" — arredondado é o que o dono lê no bloco.
    assert "Esquentando" not in linha


def test_tendencia_lista_curta_nao_estoura() -> None:
    assert weather.tendencia_temperatura([]) is None
    assert weather.tendencia_temperatura([_dia("2026-08-05", 26)]) is None


# ─────────────────────────── nowcast (fonte da chuva) ───────────────────────

_G_HOURS = {
    "forecastHours": [
        {
            "weatherCondition": {"type": "CLOUDY",
                                 "description": {"text": "Nublado"}},
            "precipitation": {"probability": {"percent": 30}},
        },
        {
            "weatherCondition": {"type": "RAIN",
                                 "description": {"text": "Chuva"}},
            "precipitation": {"probability": {"percent": 75}},
        },
    ],
}

_OM_HOURS = {
    "hourly": {
        "time": ["2026-08-05T14:00", "2026-08-05T15:00"],
        "precipitation_probability": [20, 65],
        "weather_code": [3, 61],
    },
}


def test_nowcast_google_pega_o_maximo_das_duas_horas(monkeypatch) -> None:
    monkeypatch.setattr(weather.settings, "google_maps_api_key", SecretStr("k"))

    async def _main():
        with respx.mock:
            respx.route(host="weather.googleapis.com").respond(200, json=_G_HOURS)
            async with httpx.AsyncClient() as client:
                return await weather.fetch_rain_next_hour(client, _COORDS)

    n = asyncio.run(_main())
    assert n.prob_pct == 75, "máximo das 2 horas — chuva às XX:55 também é 'próxima hora'"
    assert n.condition_label == "chuva"
    assert n.fonte == weather.FONTE_GOOGLE


def test_nowcast_cai_no_open_meteo_quando_google_falha(monkeypatch) -> None:
    monkeypatch.setattr(weather.settings, "google_maps_api_key", SecretStr("k"))

    async def _main():
        with respx.mock:
            respx.route(host="weather.googleapis.com").respond(500)
            respx.route(host="api.open-meteo.com").respond(200, json=_OM_HOURS)
            async with httpx.AsyncClient() as client:
                return await weather.fetch_rain_next_hour(client, _COORDS)

    n = asyncio.run(_main())
    assert n.prob_pct == 65
    assert n.condition_label == "chuva leve"   # WMO 61
    assert n.fonte == weather.FONTE_OM_FALLBACK


def test_nowcast_falha_dupla_estoura(monkeypatch) -> None:
    """O vigia trata WeatherError com o aviso de falha prolongada — o que não
    pode existir é probabilidade inventada/zero silencioso."""
    monkeypatch.setattr(weather.settings, "google_maps_api_key", SecretStr("k"))

    async def _main():
        with respx.mock:
            respx.route(host="weather.googleapis.com").respond(500)
            respx.route(host="api.open-meteo.com").respond(503)
            async with httpx.AsyncClient() as client:
                try:
                    await weather.fetch_rain_next_hour(client, _COORDS)
                except weather.WeatherError:
                    return True
                return False

    assert asyncio.run(_main()) is True


def test_help_documenta_e_roteia(monkeypatch) -> None:
    from bot.handlers.start import HELP_TEXT, find_help_sections
    assert "Alerta de chuva" in HELP_TEXT
    assert "Esquentando" in HELP_TEXT
    secoes = find_help_sections("o bot avisa quando vai esquentar?")
    assert any("Esquentando" in s for s in secoes)
    secoes = find_help_sections("como funciona o alerta de chuva?")
    assert any("Alerta de chuva" in s for s in secoes)
