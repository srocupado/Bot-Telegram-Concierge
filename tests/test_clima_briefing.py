"""Clima do briefing: falha e falta de config não podem sumir caladas.

Os dois caminhos devolviam `[]`. Briefing sem linha de clima é
indistinguível de briefing com clima que não deu notícia — o dono não tinha
como saber que devia haver algo ali. Mesma regra do DOU: fonte externa que
falha é DITA.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from bot.services import proactive


class _FakeSession:
    async def commit(self):
        return None


def _user():
    return SimpleNamespace(
        id=1, viagem_destino=None, viagem_ativa=False, timezone="America/Sao_Paulo",
    )


def _rodar(monkeypatch, *, coords, erro=None, ja_avisado=False):
    async def _already(*a, **kw):
        return ja_avisado

    async def _mark(*a, **kw):
        return None

    monkeypatch.setattr(proactive, "already_notified", _already)
    monkeypatch.setattr(proactive, "mark_notified", _mark)
    monkeypatch.setattr(proactive.settings, "home_coords", coords)
    monkeypatch.setattr(proactive.settings, "timezone", "America/Sao_Paulo")

    from bot.services import viagem, weather
    monkeypatch.setattr(viagem, "effective_coords", lambda _u: None)
    monkeypatch.setattr(viagem, "effective_tz", lambda _u: "America/Sao_Paulo")

    async def _fetch(_client, _coords, tz=None):
        if erro:
            raise erro
        return {"tmin": 18, "tmax": 29, "chuva": 0}

    monkeypatch.setattr(weather, "fetch_today_weather", _fetch)
    monkeypatch.setattr(weather, "format_weather_line", lambda _w: "🌤 18°/29°")

    agora = datetime.now(proactive.BRT)
    return asyncio.run(proactive.collect_clima(_FakeSession(), _user(), agora))


def test_previsao_ok_vira_linha_normal(monkeypatch) -> None:
    facts = _rodar(monkeypatch, coords="-15.79,-47.88")
    assert [f.kind for f in facts] == ["clima_hoje"]


def test_falha_da_api_e_dita(monkeypatch) -> None:
    """Antes: warning no log e nada pro dono — que lia o briefing como
    'dia sem nada' quando na verdade ninguém checou."""
    facts = _rodar(
        monkeypatch, coords="-15.79,-47.88", erro=TimeoutError("open-meteo fora"),
    )
    assert [f.kind for f in facts] == ["clima_falhou"]
    assert "Não consegui checar" in facts[0].text
    assert "NÃO assuma" in facts[0].text


def test_falta_de_config_avisa_uma_vez(monkeypatch) -> None:
    """Sem HOME_COORDS o clima nunca sai. Dizer isso uma vez é o que separa
    'não configurado' de 'quebrado em silêncio'."""
    facts = _rodar(monkeypatch, coords=None)
    assert [f.kind for f in facts] == ["clima_sem_coords"]
    assert "HOME_COORDS" in facts[0].text

    # Já avisado antes → não repete todo dia.
    assert _rodar(monkeypatch, coords=None, ja_avisado=True) == []


@pytest.mark.parametrize("erro", [TimeoutError("t"), ValueError("json ruim")])
def test_qualquer_falha_vira_aviso(monkeypatch, erro) -> None:
    """Timeout, JSON corrompido, DNS — todos silenciavam igual."""
    facts = _rodar(monkeypatch, coords="-15.79,-47.88", erro=erro)
    assert [f.kind for f in facts] == ["clima_falhou"]
