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


# ───────────── fila: "checando agora" com as fontes fora (25/10/2026) ─────────────

class _SessaoFila:
    def __init__(self, keys):
        self.keys = keys

    async def scalars(self, _stmt):
        return [SimpleNamespace(key=k) for k in self.keys]

    async def commit(self):
        return None


def _disparar(monkeypatch, keys, fontes_fora):
    from bot.services import jobs
    disparos = []
    monkeypatch.setattr(jobs, "job_em_andamento", lambda _k: False)
    monkeypatch.setattr(jobs, "spawn", lambda chave, fab: disparos.append(chave) or True)
    hoje = datetime.now(proactive.BRT).date().isoformat()
    datas = asyncio.run(proactive._processar_notas_pendentes(
        None, _SessaoFila([f"{hoje}:{k}" for k in keys]), SimpleNamespace(id=1),
        fontes_fora=fontes_fora))
    return datas, disparos


def test_checagem_do_dia_nao_dispara_com_as_fontes_fora(monkeypatch) -> None:
    """O job de entrada "all" só consulta o Inlabs: com a janela tendo
    provado as fontes fora, ele falhava calado e a linha prometia resultado."""
    datas, disparos = _disparar(monkeypatch, ["all"], fontes_fora=True)
    assert datas == [] and disparos == []


def test_nota_com_numero_continua_disparando_com_inlabs_fora(monkeypatch) -> None:
    """Entrada com número tenta o PORTAL antes — vale disparar."""
    datas, disparos = _disparar(monkeypatch, ["1400"], fontes_fora=True)
    assert len(disparos) == 1


def test_checagem_do_dia_dispara_com_as_fontes_de_pe(monkeypatch) -> None:
    _datas, disparos = _disparar(monkeypatch, ["all"], fontes_fora=False)
    assert len(disparos) == 1


def test_linha_da_fila_cita_portal_e_inlabs_fora(monkeypatch) -> None:
    hoje = datetime.now(proactive.BRT).date()

    class _S(_Sessao):
        async def scalars(self, stmt):
            sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            if "'nota_pendente'" in sql:
                return [SimpleNamespace(key=f"{hoje.isoformat()}:all",
                                        kind="nota_pendente", sent_at=None)]
            return []

    async def _vazio(*a, **kw):
        return []
    monkeypatch.setattr(proactive, "_mp_dias_pendentes", _vazio)
    monkeypatch.setattr(proactive, "_cobrir_lacuna", _vazio)

    async def _portal(*a, **kw):
        raise dou_portal.PortalError("busca no portal falhou (3x): 502")

    async def _fetch(_d):
        raise dou_monitor.DouError("falha ao autenticar no Inlabs: timeout")

    async def _none(*a, **kw):
        return None

    async def _false(*a, **kw):
        return False
    monkeypatch.setattr(dou_portal, "checar_dia_portal", _portal)
    monkeypatch.setattr(dou_monitor, "fetch_mps", _fetch)
    monkeypatch.setattr(dou_monitor, "inlabs_configurado", lambda: True)
    monkeypatch.setattr(dou_monitor, "ultima_checagem_ok", lambda _d: None)
    monkeypatch.setattr(proactive, "_janelas_restantes", lambda *_a: [19])
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", True)
    monkeypatch.setattr(proactive.settings, "dou_planalto_enabled", False)
    monkeypatch.setattr(proactive, "already_notified", _false)
    monkeypatch.setattr(proactive, "mark_notified", _none)
    user = SimpleNamespace(id=77, dou_mp_subscribed=True, is_authorized=True,
                           dou_ultimo_dia_ok=None)
    facts = asyncio.run(proactive.collect_mp(_S(), user, [hoje]))
    fila = [f.text for f in facts if f.kind == "nota_fila"]
    assert fila and "portal do DOU e Inlabs sem resposta" in fila[0]
    assert "checando agora" not in fila[0]


# ───────────── rede do Planalto dita no aviso (26/10/2026) ─────────────
# Dono: "Não temos o Planalto como rede de segurança? Pq ela não foi
# verificada?" — ela rodava, mas o resultado não aparecia no aviso.

def _alarme_com_planalto(monkeypatch, sonda, *, datas=None, pendentes=(),
                         todos=False):
    from bot.services import dou_planalto

    class _S(_Sessao):
        async def scalars(self, stmt):
            sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            if "dou_seen" in sql:
                return [SimpleNamespace(numero="1398", ano=2026)]
            return []

    async def _vazio(*a, **kw):
        return []

    async def _pend(*a, **kw):
        return list(pendentes)

    async def _portal(*a, **kw):
        raise dou_portal.PortalError("busca no portal falhou (3x): 502")

    async def _fetch(_d):
        raise dou_monitor.DouError("falha ao autenticar no Inlabs: timeout")

    async def _none(*a, **kw):
        return None

    async def _false(*a, **kw):
        return False
    monkeypatch.setattr(dou_planalto, "sondar_novas", sonda)
    monkeypatch.setattr(dou_portal, "checar_dia_portal", _portal)
    monkeypatch.setattr(dou_monitor, "fetch_mps", _fetch)
    monkeypatch.setattr(dou_monitor, "inlabs_configurado", lambda: True)
    monkeypatch.setattr(dou_monitor, "ultima_checagem_ok", lambda _d: None)
    monkeypatch.setattr(proactive, "_janelas_restantes", lambda *_a: [19])
    monkeypatch.setattr(proactive, "_mp_dias_pendentes", _pend)
    monkeypatch.setattr(proactive, "_cobrir_lacuna", _vazio)
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", True)
    monkeypatch.setattr(proactive.settings, "dou_planalto_enabled", True)
    monkeypatch.setattr(proactive, "already_notified", _false)
    monkeypatch.setattr(proactive, "mark_notified", _none)
    user = SimpleNamespace(id=77, dou_mp_subscribed=True, is_authorized=True,
                           dou_ultimo_dia_ok=None)
    hoje = datetime.now(proactive.BRT).date()
    facts = asyncio.run(proactive.collect_mp(_S(), user, datas or [hoje]))
    if todos:
        return facts
    return " | ".join(f.text for f in facts if f.kind == "mp_fail")


def test_dou_fora_diz_que_o_planalto_conferiu_sem_mp_nova(monkeypatch) -> None:
    async def _sonda(ultimo, anos):
        assert ultimo == 1398
        return []
    txt = _alarme_com_planalto(monkeypatch, _sonda)
    assert "Não consegui checar" in txt
    assert "Rede do Planalto conferida agora: nenhuma MP depois da 1.398/2026" in txt


def test_dou_fora_e_planalto_fora_diz_os_dois(monkeypatch) -> None:
    async def _sonda(ultimo, anos):
        raise RuntimeError("planalto timeout")
    txt = _alarme_com_planalto(monkeypatch, _sonda)
    assert "Planalto (rede de segurança) também não respondeu" in txt


# ───────────── baixa pela rede do Planalto (dono, 26/10/2026) ─────────────

def _sem_novas(ultimo, anos):
    async def _s(*a, **kw):
        return []
    return _s(ultimo, anos)


def _kinds(facts, kind):
    return [f.text for f in facts if f.kind == kind]


def test_dia_encerrado_com_dou_fora_recebe_baixa_pelo_planalto(monkeypatch) -> None:
    from datetime import timedelta
    ontem = datetime.now(proactive.BRT).date() - timedelta(days=2)
    facts = _alarme_com_planalto(monkeypatch, _sem_novas, datas=[ontem], todos=True)
    assert _kinds(facts, "mp_fail") == [], "dia baixado não pode alarmar"
    retro = _kinds(facts, "mp_retro")
    assert retro and "baixado pela rede do Planalto" in retro[0]
    assert "nenhuma MP depois da 1.398/2026" in retro[0]


def test_dia_aberto_nao_recebe_baixa_pelo_planalto(monkeypatch) -> None:
    hoje = datetime.now(proactive.BRT).date()
    facts = _alarme_com_planalto(monkeypatch, _sem_novas, datas=[hoje], todos=True)
    assert _kinds(facts, "mp_retro") == []
    assert any("Não consegui checar" in t for t in _kinds(facts, "mp_fail"))


def test_planalto_fora_nao_da_baixa(monkeypatch) -> None:
    from datetime import timedelta
    d = datetime.now(proactive.BRT).date() - timedelta(days=2)

    async def _fora(*a, **kw):
        raise RuntimeError("planalto timeout")
    facts = _alarme_com_planalto(monkeypatch, _fora, datas=[d], todos=True)
    assert _kinds(facts, "mp_retro") == []
    assert any("Não consegui checar" in t for t in _kinds(facts, "mp_fail"))


def test_retroativa_pendente_tambem_baixa_pelo_planalto(monkeypatch) -> None:
    from datetime import timedelta
    hoje = datetime.now(proactive.BRT).date()
    velho = hoje - timedelta(days=3)
    facts = _alarme_com_planalto(monkeypatch, _sem_novas, datas=[hoje],
                                 pendentes=[velho], todos=True)
    retro = _kinds(facts, "mp_retro")
    assert any(velho.strftime("%d/%m") in t and "rede do Planalto" in t for t in retro)
    assert len(retro) == 1, "sem a linha genérica duplicada"
