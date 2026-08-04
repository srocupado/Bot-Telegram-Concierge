"""Clima: Google Weather como fonte primária, Open-Meteo como fallback DITO.

Motivo (04/08/2026): o dono comparou as previsões contra o céu real de
Brasília e o Google vinha acertando mais. A chave é a mesma do trânsito
(GOOGLE_MAPS_API_KEY); sem chave, tudo segue 100% Open-Meteo como antes.
Regra do projeto: fallback nunca é silencioso — a origem alternativa
aparece na resposta e a falha DUPLA continua estourando WeatherError.
"""
from __future__ import annotations

import asyncio

import httpx
import respx
from pydantic import SecretStr

from bot.services import weather

_COORDS = "-15.79,-47.88"

_G_RESP = {
    "forecastDays": [
        {
            "displayDate": {"year": 2026, "month": 8, "day": 5},
            "minTemperature": {"degrees": 17.2, "unit": "CELSIUS"},
            "maxTemperature": {"degrees": 28.4, "unit": "CELSIUS"},
            "daytimeForecast": {
                "weatherCondition": {
                    "type": "PARTLY_CLOUDY",
                    "description": {"text": "Parcialmente nublado",
                                    "languageCode": "pt-BR"},
                },
                "precipitation": {"probability": {"percent": 40, "type": "RAIN"},
                                  "qpf": {"quantity": 1.5, "unit": "MILLIMETERS"}},
            },
            "nighttimeForecast": {
                "weatherCondition": {"type": "CLOUDY",
                                     "description": {"text": "Nublado"}},
                "precipitation": {"probability": {"percent": 20},
                                  "qpf": {"quantity": 0.5}},
            },
        },
    ],
}

_OM_RESP = {
    "daily": {
        "time": ["2026-08-05"],
        "temperature_2m_min": [16.0],
        "temperature_2m_max": [27.0],
        "precipitation_probability_max": [10],
        "precipitation_sum": [0.0],
        "weather_code": [1],
    },
}


def _com_chave(monkeypatch) -> None:
    monkeypatch.setattr(weather.settings, "google_maps_api_key", SecretStr("k"))


def _sem_chave(monkeypatch) -> None:
    monkeypatch.setattr(weather.settings, "google_maps_api_key", None)


async def _forecast(days: int = 1):
    async with httpx.AsyncClient() as client:
        return await weather.fetch_forecast(client, _COORDS, days=days)


def test_google_e_a_fonte_primaria(monkeypatch) -> None:
    _com_chave(monkeypatch)

    async def _main():
        with respx.mock:
            g = respx.route(host="weather.googleapis.com").respond(200, json=_G_RESP)
            om = respx.route(host="api.open-meteo.com").respond(200, json=_OM_RESP)
            dias = await _forecast()
            return dias, g.called, om.called

    dias, g_called, om_called = asyncio.run(_main())
    assert g_called and not om_called
    d = dias[0]
    assert d.fonte == weather.FONTE_GOOGLE
    assert d.date_iso == "2026-08-05"
    assert (d.temp_min_c, d.temp_max_c) == (17.2, 28.4)
    assert d.precip_prob_pct == 40, "probabilidade = max(dia, noite)"
    assert d.precip_mm == 2.0, "qpf = dia + noite"
    assert d.condition_emoji == "⛅"
    assert d.condition_label == "parcialmente nublado", (
        "label vem do description.text pt-BR do Google"
    )


def test_google_falhou_cai_no_open_meteo_com_origem_dita(monkeypatch, caplog) -> None:
    _com_chave(monkeypatch)

    async def _main():
        with respx.mock:
            respx.route(host="weather.googleapis.com").respond(500, text="boom")
            respx.route(host="api.open-meteo.com").respond(200, json=_OM_RESP)
            return await _forecast()

    with caplog.at_level("WARNING"):
        dias = asyncio.run(_main())
    assert dias[0].fonte == weather.FONTE_OM_FALLBACK
    assert "Google Weather falhou" in " | ".join(r.getMessage() for r in caplog.records)
    # A origem alternativa aparece pro dono, não só no log.
    texto = weather.format_week_forecast(dias)
    assert "Open-Meteo" in texto and "indisponível" in texto
    linha = weather.format_weather_line(
        weather.WeatherInfo(16, 27, 10, 0, "🌤️", "x", fonte=weather.FONTE_OM_FALLBACK))
    assert "Open-Meteo" in linha


def test_sem_chave_nem_tenta_o_google(monkeypatch) -> None:
    _sem_chave(monkeypatch)

    async def _main():
        with respx.mock:
            g = respx.route(host="weather.googleapis.com").respond(200, json=_G_RESP)
            respx.route(host="api.open-meteo.com").respond(200, json=_OM_RESP)
            dias = await _forecast()
            return dias, g.called

    dias, g_called = asyncio.run(_main())
    assert not g_called
    assert dias[0].fonte == weather.FONTE_OM
    # Sem fallback não há marca de origem — comportamento antigo intacto.
    assert "Open-Meteo" not in weather.format_week_forecast(dias)


def test_pedido_acima_de_10_dias_vai_direto_ao_open_meteo(monkeypatch) -> None:
    """Google cobre 10 dias; pedir 16 tem que vir a série INTEIRA de uma
    fonte só — emendar duas previsões no meio da lista confunde mais que
    ajuda."""
    _com_chave(monkeypatch)

    async def _main():
        with respx.mock:
            g = respx.route(host="weather.googleapis.com").respond(200, json=_G_RESP)
            respx.route(host="api.open-meteo.com").respond(200, json=_OM_RESP)
            dias = await _forecast(days=16)
            return dias, g.called

    dias, g_called = asyncio.run(_main())
    assert not g_called
    assert dias[0].fonte == weather.FONTE_OM


def test_falha_dupla_continua_estourando(monkeypatch) -> None:
    """Google fora E Open-Meteo fora → WeatherError (os callers transformam
    em 'não consegui checar' — nunca silêncio nem previsão inventada)."""
    _com_chave(monkeypatch)

    async def _main():
        with respx.mock:
            respx.route(host="weather.googleapis.com").respond(500)
            respx.route(host="api.open-meteo.com").respond(503)
            try:
                await _forecast()
            except weather.WeatherError:
                return True
            return False

    assert asyncio.run(_main()) is True


def test_fetch_today_herda_a_cascata(monkeypatch) -> None:
    _com_chave(monkeypatch)

    async def _main():
        with respx.mock:
            respx.route(host="weather.googleapis.com").respond(200, json=_G_RESP)
            async with httpx.AsyncClient() as client:
                return await weather.fetch_today_weather(client, _COORDS)

    w = asyncio.run(_main())
    assert w.fonte == weather.FONTE_GOOGLE
    assert (w.temp_min_c, w.temp_max_c) == (17.2, 28.4)


def test_emoji_google_precipitacao_antes_do_generico() -> None:
    assert weather._emoji_google("LIGHT_RAIN_SHOWERS") == "🌦️"
    assert weather._emoji_google("RAIN") == "🌧️"
    assert weather._emoji_google("THUNDERSTORM") == "⛈️"
    assert weather._emoji_google("CLEAR") == "☀️"
    assert weather._emoji_google("TIPO_NOVO_QUALQUER") == "🌡️"
