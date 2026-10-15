"""Falha passageira do portal virava "Inlabs instável" (bug real de 15/10/2026).

Log do Orange Pi, 13h05 BRT:
  portal do DOU falhou p/ 2026-10-15: busca no portal falhou:      ← vazio
  fetch_mps(2026-10-15) falhou: o Inlabs ... está em manutenção agora
Um minuto depois o portal respondia normal (edição confirmada, sem MP). Três
defeitos: o portal (fonte primária) não tinha nova tentativa; o erro saía sem
o tipo (str() de timeout do httpx é vazio); e o alarme culpava só o Inlabs,
escondendo que a fonte primária também tinha falhado.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

from bot.services import dou_monitor, dou_portal, proactive

_OK = ('<script id="x_params" type="application/json">'
       '{"jsonArray": []}</script>')


@pytest.fixture(autouse=True)
def _sem_pausa(monkeypatch):
    monkeypatch.setattr(dou_portal, "_BUSCA_PAUSA_S", 0)


def _buscar():
    async def _go():
        async with httpx.AsyncClient() as c:
            return await dou_portal._buscar(c, "q", date(2026, 10, 15))
    return asyncio.run(_go())


@respx.mock
def test_timeout_passageiro_ganha_nova_tentativa() -> None:
    rota = respx.get(dou_portal.BUSCA_URL).mock(side_effect=[
        httpx.ReadTimeout(""), httpx.Response(200, text=_OK)])
    assert _buscar() == []
    assert rota.call_count == 2


@respx.mock
def test_5xx_tambem_repete() -> None:
    rota = respx.get(dou_portal.BUSCA_URL).mock(side_effect=[
        httpx.Response(503), httpx.Response(200, text=_OK)])
    assert _buscar() == []
    assert rota.call_count == 2


@respx.mock
def test_404_nao_repete() -> None:
    rota = respx.get(dou_portal.BUSCA_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(dou_portal.PortalError):
        _buscar()
    assert rota.call_count == 1


@respx.mock
def test_esgotou_e_o_erro_diz_o_tipo() -> None:
    rota = respx.get(dou_portal.BUSCA_URL).mock(side_effect=httpx.ReadTimeout(""))
    with pytest.raises(dou_portal.PortalError) as e:
        _buscar()
    assert rota.call_count == dou_portal._BUSCA_TENTATIVAS
    assert "ReadTimeout" in str(e.value), "erro vazio no log não diz nada"


# ───────────── alarme diz QUAIS fontes falharam ─────────────

class _Sessao:
    async def scalars(self, _stmt):
        return []

    async def commit(self):
        return None


def _alarme(monkeypatch, *, portal_falha: bool, inlabs_exc: Exception) -> str:
    async def _portal(*a, **kw):
        if portal_falha:
            raise dou_portal.PortalError("busca no portal falhou (3x): ReadTimeout")
        return dou_portal.PortalDia([], False)    # inconclusivo

    async def _fetch(_d):
        raise inlabs_exc

    async def _false(*a, **kw):
        return False

    async def _none(*a, **kw):
        return None
    monkeypatch.setattr(dou_portal, "checar_dia_portal", _portal)
    monkeypatch.setattr(dou_monitor, "fetch_mps", _fetch)
    monkeypatch.setattr(dou_monitor, "inlabs_configurado", lambda: True)
    monkeypatch.setattr(dou_monitor, "ultima_checagem_ok", lambda _d: None)
    monkeypatch.setattr(proactive, "_janelas_restantes", lambda *_a: [19])
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", True)
    monkeypatch.setattr(proactive.settings, "dou_planalto_enabled", False)
    monkeypatch.setattr(proactive, "already_notified", _false)
    monkeypatch.setattr(proactive, "mark_notified", _none)
    monkeypatch.setattr(proactive, "unmark_notified", _none)
    user = SimpleNamespace(id=77, dou_mp_subscribed=True, is_authorized=True,
                           dou_ultimo_dia_ok=None)
    hoje = datetime.now(proactive.BRT).date()
    facts = asyncio.run(proactive.collect_mp(_Sessao(), user, [hoje]))
    return " | ".join(f.text for f in facts if f.kind == "mp_fail")


def test_portal_e_inlabs_falhando_o_alarme_cita_os_dois(monkeypatch) -> None:
    """O caso de 15/10: portal com erro + Inlabs em manutenção."""
    txt = _alarme(monkeypatch, portal_falha=True,
                  inlabs_exc=dou_monitor.InlabsMaintenanceError("manutenção"))
    assert "Não consegui checar" in txt
    assert "portal do DOU fora do ar e Inlabs em manutenção" in txt
    assert "Inlabs instável" not in txt


def test_inlabs_sem_resposta_nao_vira_manutencao(monkeypatch) -> None:
    txt = _alarme(monkeypatch, portal_falha=True,
                  inlabs_exc=dou_monitor.DouError("timeout"))
    assert "portal do DOU fora do ar e Inlabs sem resposta" in txt


def test_portal_inconclusivo_nao_e_chamado_de_fora_do_ar(monkeypatch) -> None:
    """Portal RESPONDEU (índice vazio): o aviso é o informativo do portal,
    nunca 'fora do ar'."""
    txt = _alarme(monkeypatch, portal_falha=False,
                  inlabs_exc=dou_monitor.InlabsMaintenanceError("manutenção"))
    assert "fora do ar" not in txt
    assert "portal público" in txt
