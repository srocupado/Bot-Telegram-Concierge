"""Manutenção do Inlabs surge na linha de status da fila (causa APURADA).

O dono perguntou: se o Inlabs está fora, o proativo não deveria avisar? A
checagem própria do proativo já avisa. O buraco era a NOTA na fila: quando a
re-tentativa em background batia na manutenção do Inlabs, era tratada em
silêncio e a linha seguia dizendo "gerando agora"/"tento na próxima janela".

Agora, quando a última re-tentativa bate em manutenção VERIFICADA (o Inlabs
declara), grava a marca `dou_manut` e a linha diz "aguardando o Inlabs voltar
(em manutenção)". Só a manutenção declarada — instabilidade genérica não
afirma causa (limpa a marca).
"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from bot.services import proactive
from bot.services.dou_monitor import DouError, InlabsMaintenanceError

D = date(2026, 8, 2)
KEY = f"{D.isoformat()}:all"


class _Sessao:
    async def get(self, _m, _id):
        return SimpleNamespace(id=_id, is_authorized=True, dou_mp_subscribed=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self):
        return None


@pytest.fixture
def reg(monkeypatch):
    estado = {"marcas": set(), "log": []}

    async def _already(_s, _uid, kind, key):
        return (kind, key) in estado["marcas"]

    async def _mark(_s, _uid, kind, key):
        estado["marcas"].add((kind, key))
        estado["log"].append(("mark", kind, key))

    async def _unmark(_s, _uid, kind, key):
        estado["marcas"].discard((kind, key))
        estado["log"].append(("unmark", kind, key))

    from bot.db import session as db_session
    monkeypatch.setattr(db_session, "SessionLocal", lambda: _Sessao())
    monkeypatch.setattr(proactive, "already_notified", _already)
    monkeypatch.setattr(proactive, "mark_notified", _mark)
    monkeypatch.setattr(proactive, "unmark_notified", _unmark)
    return estado


def _entregar(monkeypatch, *, erro=None, falhas=None):
    async def _deliver(bot, session, user, d, *, force, only_numeros):
        if erro:
            raise erro
        return (1, list(falhas or []), None)

    from bot.services import dou_monitor
    monkeypatch.setattr(dou_monitor, "deliver_to_user", _deliver)
    asyncio.run(proactive._entregar_nota_pendente(None, 42, D, None, KEY))


def test_manutencao_grava_marca_e_mantem_na_fila(reg, monkeypatch) -> None:
    _entregar(monkeypatch, erro=InlabsMaintenanceError("em manutenção"))
    assert ("dou_manut", D.isoformat()) in reg["marcas"]
    assert ("unmark", "nota_pendente", KEY) not in reg["log"], "não pode dar baixa"


def test_instabilidade_generica_nao_afirma_manutencao(reg, monkeypatch) -> None:
    reg["marcas"].add(("dou_manut", D.isoformat()))   # marca de um episódio anterior
    _entregar(monkeypatch, erro=DouError("cookie ausente"))
    assert ("dou_manut", D.isoformat()) not in reg["marcas"], (
        "instabilidade sem manutenção declarada não pode manter a marca"
    )


def test_sucesso_limpa_a_marca_e_baixa(reg, monkeypatch) -> None:
    reg["marcas"].add(("dou_manut", D.isoformat()))
    _entregar(monkeypatch, falhas=[])
    assert ("dou_manut", D.isoformat()) not in reg["marcas"]
    assert ("unmark", "nota_pendente", KEY) in reg["log"]


def test_falha_de_geracao_nao_e_manutencao(reg, monkeypatch) -> None:
    reg["marcas"].add(("dou_manut", D.isoformat()))
    _entregar(monkeypatch, falhas=["1400"])   # Inlabs OK, nota falhou
    assert ("dou_manut", D.isoformat()) not in reg["marcas"]
    assert ("unmark", "nota_pendente", KEY) not in reg["log"], "falha mantém na fila"


# ───────────────────── a linha de status reflete a marca ─────────────────────

def test_linha_diz_manutencao_quando_marcada(monkeypatch) -> None:
    from bot.services import dou_monitor

    async def _false(*a, **kw):
        return False

    async def _none(*a, **kw):
        return None

    async def _fetch(_d):
        return dou_monitor.MPList()

    monkeypatch.setattr(dou_monitor, "fetch_mps", _fetch)
    monkeypatch.setattr(proactive, "already_notified", _false)
    monkeypatch.setattr(proactive, "mark_notified", _none)
    monkeypatch.setattr(proactive, "unmark_notified", _none)
    monkeypatch.setattr(proactive.jobs, "spawn", lambda *a, **kw: True)
    monkeypatch.setattr(proactive.jobs, "job_em_andamento", lambda _k: False)

    from types import SimpleNamespace as NS

    class _Sess:
        def __init__(self, fila, manut):
            # collect_mp: scalars 1 = pendências retro (vazio), 2 = nota_fila,
            # 3 = dou_manut
            self._r = [[], fila, manut]

        async def scalars(self, _stmt):
            return list(self._r.pop(0)) if self._r else []

        async def commit(self):
            return None

    from datetime import timedelta
    fila = [NS(key=KEY)]
    manut = [NS(key=D.isoformat())]
    user = NS(id=42, dou_mp_subscribed=True,
              dou_ultimo_dia_ok=date.today() - timedelta(days=1))

    facts = asyncio.run(proactive.collect_mp(_Sess(fila, manut), user, []))
    linhas = [f.text for f in facts if f.kind == "nota_fila"]
    assert len(linhas) == 1
    assert "em manutenção" in linhas[0] and "na fila de checagem" in linhas[0]
    assert "gerando agora" not in linhas[0] and "próxima janela" not in linhas[0]


def test_inlabs_fora_no_run_nao_diz_gerando_agora(monkeypatch) -> None:
    """O caso que o dono viu: a checagem DESTE run falhou (Inlabs fora), mas a
    linha da nota dizia 'gerando agora'. Agora reflete o fato observado e
    sinaliza pra NÃO disparar job (que só falharia)."""
    from bot.services import dou_monitor
    from datetime import timedelta

    async def _raise(_d):
        raise dou_monitor.DouError("Inlabs fora")

    async def _false(*a, **kw):
        return False

    async def _none(*a, **kw):
        return None

    monkeypatch.setattr(dou_monitor, "fetch_mps", _raise)
    monkeypatch.setattr(proactive, "already_notified", _false)
    monkeypatch.setattr(proactive, "mark_notified", _none)
    monkeypatch.setattr(proactive, "unmark_notified", _none)

    from types import SimpleNamespace as NS

    class _Sess:
        def __init__(self):
            self._r = [[], [NS(key=KEY)], []]   # retro, nota_fila, dou_manut

        async def scalars(self, _s):
            return list(self._r.pop(0)) if self._r else []

        async def commit(self):
            return None

    user = NS(id=42, dou_mp_subscribed=True,
              dou_ultimo_dia_ok=date.today() - timedelta(days=1))
    facts = asyncio.run(proactive.collect_mp(_Sess(), user, [D]))
    linhas = [f.text for f in facts if f.kind == "nota_fila"]

    assert user.dou_fora_agora is True, "flag tem que barrar o disparo em run_for_user"
    assert len(linhas) == 1
    assert "na fila de checagem" in linhas[0] and "Inlabs" in linhas[0]
    assert "gerando" not in linhas[0], "não pode soar iminente"
    assert "manutenção" not in linhas[0], "instável genérico não afirma manutenção"
