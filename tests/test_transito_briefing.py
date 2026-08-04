"""Trânsito do briefing: falha e falta de config não podem sumir caladas.

Auditoria de 03/08/2026: falha do Maps → `return []` → o bloco 🚗
desaparecia do briefing, indistinguível de fim de semana — o dono saía
achando a rota normal quando o Maps nem tinha sido consultado. O clima já
tinha recebido exatamente esta correção; o trânsito ficou de fora.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from pydantic import SecretStr

from bot.services import proactive


class _FakeSession:
    async def commit(self):
        return None


def _user():
    return SimpleNamespace(id=1, timezone="America/Sao_Paulo")


def _segunda_7h() -> datetime:
    agora = datetime.now(proactive.BRT).replace(hour=7, minute=5)
    return agora - timedelta(days=agora.weekday())   # segunda-feira


def _dedup_fake(monkeypatch, marcados: set):
    async def _already(_s, _u, kind, key):
        return (kind, key) in marcados

    async def _mark(_s, _u, kind, key):
        marcados.add((kind, key))

    monkeypatch.setattr(proactive, "already_notified", _already)
    monkeypatch.setattr(proactive, "mark_notified", _mark)


def _config_ok(monkeypatch):
    monkeypatch.setattr(proactive.settings, "home_coords", "-15.79,-47.88")
    monkeypatch.setattr(proactive.settings, "work_coords", "-15.80,-47.86")
    monkeypatch.setattr(proactive.settings, "google_maps_api_key", SecretStr("k"))
    monkeypatch.setattr(proactive.settings, "route_google_maps_url", "")


def test_falha_do_maps_e_dita(monkeypatch) -> None:
    _config_ok(monkeypatch)
    _dedup_fake(monkeypatch, set())

    from bot.services import traffic

    async def _boom(*a, **kw):
        raise traffic.TrafficError("Maps 500")

    monkeypatch.setattr(traffic, "fetch_traffic_with_alternative", _boom)
    facts = asyncio.run(proactive.collect_transito(_FakeSession(), _user(), _segunda_7h()))
    assert [f.kind for f in facts] == ["transito_falhou"]
    assert "NÃO assuma via livre" in facts[0].text


def test_config_faltando_avisa_uma_vez(monkeypatch) -> None:
    monkeypatch.setattr(proactive.settings, "home_coords", "")
    monkeypatch.setattr(proactive.settings, "work_coords", "")
    monkeypatch.setattr(proactive.settings, "google_maps_api_key", None)
    marcados: set = set()
    _dedup_fake(monkeypatch, marcados)

    f1 = asyncio.run(proactive.collect_transito(_FakeSession(), _user(), _segunda_7h()))
    assert [f.kind for f in f1] == ["transito_sem_config"]
    assert "sem a linha de trânsito" in f1[0].text
    f2 = asyncio.run(proactive.collect_transito(_FakeSession(), _user(), _segunda_7h()))
    assert f2 == [], "aviso de config repetiu (devia ser 1x)"


def test_fim_de_semana_segue_em_silencio(monkeypatch) -> None:
    """Sábado sem linha de trânsito é o comportamento esperado — nada de
    aviso nem de falso alarme."""
    _config_ok(monkeypatch)
    _dedup_fake(monkeypatch, set())
    sabado = _segunda_7h() + timedelta(days=5)
    facts = asyncio.run(proactive.collect_transito(_FakeSession(), _user(), sabado))
    assert facts == []


def test_clima_sem_coords_agora_marca_o_1x(monkeypatch) -> None:
    """Conserto de carona: o '1x' do clima_sem_coords nunca era marcado (o
    pós-envio pula a categoria 'clima') e o aviso repetia todo dia."""
    marcados: set = set()
    _dedup_fake(monkeypatch, marcados)
    monkeypatch.setattr(proactive.settings, "home_coords", "")

    from bot.services import viagem
    monkeypatch.setattr(viagem, "effective_coords", lambda _u: None)
    monkeypatch.setattr(viagem, "effective_tz", lambda _u: "America/Sao_Paulo")

    agora = datetime.now(proactive.BRT)
    user = SimpleNamespace(id=1, viagem_destino=None, timezone="America/Sao_Paulo")
    f1 = asyncio.run(proactive.collect_clima(_FakeSession(), user, agora))
    assert [f.kind for f in f1] == ["clima_sem_coords"]
    f2 = asyncio.run(proactive.collect_clima(_FakeSession(), user, agora))
    assert f2 == [], "aviso de HOME_COORDS repetiu (devia ser 1x)"
