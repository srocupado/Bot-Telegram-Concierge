"""Alarme de "não consegui checar o DOU" em dois estágios.

Incidente real (03/08/2026): o bot respondeu "Nenhuma MP nova em 03/08" às
~08:55 (checagem manual OK) e, minutos depois, a janela proativa falhou no
Inlabs e gritou "NÃO assuma que não houve MP em 03/08" — contradição que
treina o dono a ignorar o alarme (e alarme ignorado perde MP tão bem quanto
silêncio).

Regra: com checagem COMPLETA recente (≤ _OK_RECENTE_H) do mesmo dia, a falha
vira linha INFORMATIVA com o contexto apurado ("chequei às HH:MM, sem MP até
então"); sem checagem recente — ou com ela envelhecida — o alarme forte segue
igual. A falha NUNCA deixa de ser dita; muda só o que o bot pode AFIRMAR.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from bot.services import dou_monitor, proactive
from bot.services.dou_monitor import BRT


class _FakeSession:
    def __init__(self, *respostas):
        self._respostas = list(respostas)

    async def scalars(self, _stmt):
        return list(self._respostas.pop(0)) if self._respostas else []

    async def commit(self):
        return None


@pytest.fixture(autouse=True)
def _ultima_ok_limpa():
    dou_monitor._ultima_ok.clear()
    yield
    dou_monitor._ultima_ok.clear()


def _facts_de_falha(monkeypatch):
    """Roda collect_mp com o fetch de HOJE falhando; devolve os facts mp_fail."""
    hoje = datetime.now(proactive.BRT).date()

    async def _fetch(_d):
        raise dou_monitor.DouError("Inlabs recusou a sessão")

    async def _false(*a, **kw):
        return False

    async def _none(*a, **kw):
        return None

    monkeypatch.setattr(dou_monitor, "fetch_mps", _fetch)
    # Portal público DESLIGADO: estes testes cobrem a semântica Inlabs-only
    # do alarme (o fallback do portal tem suíte própria, test_dou_portal.py).
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", False)
    monkeypatch.setattr(proactive, "already_notified", _false)
    monkeypatch.setattr(proactive, "mark_notified", _none)
    monkeypatch.setattr(proactive, "unmark_notified", _none)

    user = SimpleNamespace(id=99, dou_mp_subscribed=True,
                           dou_ultimo_dia_ok=hoje - timedelta(days=1))
    session = _FakeSession([], [], [])
    facts = asyncio.run(proactive.collect_mp(session, user, [hoje]))
    return hoje, [f for f in facts if f.kind == "mp_fail"]


def test_sem_checagem_ok_recente_alarme_forte(monkeypatch) -> None:
    hoje, falhas = _facts_de_falha(monkeypatch)
    assert len(falhas) == 1
    assert falhas[0].key.startswith("fail:")
    assert "NÃO assuma" in falhas[0].text


def test_com_checagem_ok_recente_vira_linha_informativa(monkeypatch) -> None:
    """O caso do incidente: checagem OK minutos antes → nada de gritar."""
    hoje = datetime.now(proactive.BRT).date()
    quando = datetime.now(BRT) - timedelta(minutes=10)
    dou_monitor._ultima_ok[hoje] = (quando, 0)

    _, falhas = _facts_de_falha(monkeypatch)
    assert len(falhas) == 1
    assert falhas[0].key == f"failsoft:{hoje.isoformat()}"
    assert "NÃO assuma" not in falhas[0].text
    assert f"chequei às {quando.strftime('%H:%M')}" in falhas[0].text
    assert "sem MP até então" in falhas[0].text
    assert "sigo re-checando" in falhas[0].text


def test_checagem_ok_envelhecida_volta_ao_alarme_forte(monkeypatch) -> None:
    """OK de manhã + Inlabs fora a janela seguinte INTEIRA (> _OK_RECENTE_H):
    o contexto envelheceu — o bot não pode mais suavizar, volta a gritar."""
    hoje = datetime.now(proactive.BRT).date()
    velha = datetime.now(BRT) - timedelta(hours=proactive._OK_RECENTE_H + 1)
    dou_monitor._ultima_ok[hoje] = (velha, 0)

    _, falhas = _facts_de_falha(monkeypatch)
    assert len(falhas) == 1
    assert falhas[0].key.startswith("fail:")
    assert "NÃO assuma" in falhas[0].text


def test_linha_informativa_diz_quantas_mps_ja_viu(monkeypatch) -> None:
    hoje = datetime.now(proactive.BRT).date()
    dou_monitor._ultima_ok[hoje] = (datetime.now(BRT) - timedelta(minutes=5), 2)

    _, falhas = _facts_de_falha(monkeypatch)
    assert "2 MP(s) detectada(s) até então" in falhas[0].text


# ─────────────── /mp_fila com o contexto da última checagem ───────────────

def test_fmt_fila_mostra_ultima_checagem() -> None:
    from bot.handlers.dou_mp import _fmt_fila_mp
    d = date(2026, 8, 3)
    fila = {
        "notas": [], "dias": [(d, 14)], "abertos": [d],
        "janelas_hoje": [19], "manutencao": False,
        "ultima_ok": {d: (datetime(2026, 8, 3, 8, 55, tzinfo=BRT), 0)},
    }
    out = _fmt_fila_mp(fila)
    assert "03/08/2026 — re-checo hoje às 19h" in out
    assert "já checado 03/08 08:55" in out
    assert "sem MP até então" in out


def test_fmt_fila_sem_ultima_ok_nao_inventa_contexto() -> None:
    """Snapshot sem 'ultima_ok' (ou dia nunca checado): sem sufixo — nada de
    inventar apuração que não existe."""
    from bot.handlers.dou_mp import _fmt_fila_mp
    fila = {"notas": [], "dias": [(date(2026, 8, 3), 14)], "manutencao": False}
    out = _fmt_fila_mp(fila)
    assert "já checado" not in out


def test_listar_fila_separa_dia_aberto_de_fechado() -> None:
    """'abertos' traz só o dia que ainda pode receber edição (fecha às 6h do
    dia seguinte); dia passado preso por falha fica de fora — é ele que
    carrega o contador de desistência no /mp_fila."""
    class _Sess:
        def __init__(self, rows):
            self._rows = rows

        async def scalars(self, _stmt):
            return list(self._rows)

    hoje = datetime.now(BRT).date()
    velho = hoje - timedelta(days=5)
    rows = [SimpleNamespace(kind="mp_pendente", key=hoje.isoformat()),
            SimpleNamespace(kind="mp_pendente", key=velho.isoformat())]
    fila = asyncio.run(proactive.listar_fila_mp(_Sess(rows), 1, hoje))
    assert fila["abertos"] == [hoje]
    assert isinstance(fila["janelas_hoje"], list)


def test_listar_fila_inclui_ultima_ok() -> None:
    """listar_fila_mp anexa o registro em memória aos dias listados —
    read-only: consultar não muda nada."""
    class _Sess:
        def __init__(self, rows):
            self._rows = rows

        async def scalars(self, _stmt):
            return list(self._rows)

    d = date(2026, 8, 2)
    quando = datetime(2026, 8, 2, 9, 30, tzinfo=BRT)
    dou_monitor._ultima_ok[d] = (quando, 1)
    rows = [SimpleNamespace(kind="mp_pendente", key=d.isoformat())]

    fila = asyncio.run(proactive.listar_fila_mp(_Sess(rows), 1, date(2026, 8, 3)))
    assert fila["ultima_ok"] == {d: (quando, 1)}


# ─────────── dedup do coletor: união com dou_seen_mps ───────────

def test_mp_entregue_com_nota_nao_e_reanunciada(monkeypatch) -> None:
    """MP entregue via /mp_dou_agora (dou_seen_mps, sem ProactiveNotice
    'mp'): a janela seguinte NÃO pode re-anunciá-la com botão de nota — é a
    mesma união da conferência com a Câmara ("o dono FICOU SABENDO?")."""
    from bot.services.dou_monitor import MPList

    hoje = datetime.now(proactive.BRT).date()
    mp = {"numero": "1381", "ano": 2026, "ementa": "Dispõe sobre teste."}

    async def _fetch(_d):
        return MPList([mp])

    async def _false(*a, **kw):
        return False

    async def _none(*a, **kw):
        return None

    monkeypatch.setattr(dou_monitor, "fetch_mps", _fetch)
    # Portal público DESLIGADO: estes testes cobrem a semântica Inlabs-only
    # do alarme (o fallback do portal tem suíte própria, test_dou_portal.py).
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", False)
    monkeypatch.setattr(proactive, "already_notified", _false)
    monkeypatch.setattr(proactive, "mark_notified", _none)
    monkeypatch.setattr(proactive, "unmark_notified", _none)

    user = SimpleNamespace(id=7, dou_mp_subscribed=True,
                           dou_ultimo_dia_ok=hoje - timedelta(days=1))
    # 1ª resposta: dou_seen_mps com a MP JÁ ENTREGUE; demais: vazias.
    session = _FakeSession(
        [SimpleNamespace(numero="1381", ano=2026)], [], [], [],
    )
    facts = asyncio.run(proactive.collect_mp(session, user, [hoje]))
    assert [f for f in facts if f.kind == "mp"] == [], (
        "MP já entregue com nota foi re-anunciada (duplicata sistemática)"
    )


def test_curto_circuito_para_de_insistir_no_inlabs(monkeypatch) -> None:
    """Primeira data falhou → as seguintes NÃO pagam a cascata de timeouts:
    viram pendência direto (re-checadas quando o Inlabs voltar)."""
    tentativas = {"n": 0}

    async def _fetch(_d):
        tentativas["n"] += 1
        raise dou_monitor.DouError("Inlabs fora")

    async def _false(*a, **kw):
        return False

    async def _none(*a, **kw):
        return None

    monkeypatch.setattr(dou_monitor, "fetch_mps", _fetch)
    # Portal público DESLIGADO: estes testes cobrem a semântica Inlabs-only
    # do alarme (o fallback do portal tem suíte própria, test_dou_portal.py).
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", False)
    monkeypatch.setattr(proactive, "already_notified", _false)
    monkeypatch.setattr(proactive, "mark_notified", _none)
    monkeypatch.setattr(proactive, "unmark_notified", _none)

    hoje = datetime.now(proactive.BRT).date()
    user = SimpleNamespace(id=7, dou_mp_subscribed=True,
                           dou_ultimo_dia_ok=hoje - timedelta(days=1))
    session = _FakeSession([], [], [])
    facts = asyncio.run(proactive.collect_mp(
        session, user, [hoje - timedelta(days=1), hoje],
    ))
    assert tentativas["n"] == 1, (
        f"insistiu {tentativas['n']}x no Inlabs morto — cascata de timeouts no tick"
    )
    falha = next(f for f in facts if f.kind == "mp_fail")
    assert (hoje - timedelta(days=1)).strftime("%d/%m") in falha.text
    assert hoje.strftime("%d/%m") in falha.text, (
        "a data pulada pelo curto-circuito sumiu do aviso"
    )
