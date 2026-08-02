"""/mp_em_fila: visibilidade da fila do monitor de MP (notas aguardando
geração + dias pendentes de re-checagem). Read-only — consultar a fila não
pode alterá-la.
"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

from bot.services import proactive
from bot.services.proactive import _MP_RETRO_EXPIRA_DIAS
from bot.handlers.dou_mp import _fmt_alvo, _fmt_fila_mp


class _Sess:
    def __init__(self, rows):
        self._rows = rows

    async def scalars(self, _stmt):
        return list(self._rows)


def _n(kind, key):
    return SimpleNamespace(kind=kind, key=key)


# ───────────────────────── leitor da fila (serviço) ─────────────────────────

def test_listar_fila_le_notas_dias_e_manutencao() -> None:
    hoje = date(2026, 8, 2)
    rows = [
        _n("nota_pendente", "2026-08-01:1382"),
        _n("nota_pendente", "2026-07-30:all"),
        _n("mp_pendente", "2026-07-29"),          # dentro da janela
        _n("mp_pendente", "2026-07-01"),          # > 14 dias → fora do output
        _n("dou_manut", "now"),
    ]
    fila = asyncio.run(proactive.listar_fila_mp(_Sess(rows), 1, hoje))

    assert fila["manutencao"] is True
    assert (date(2026, 8, 1), "1382") in fila["notas"]
    assert (date(2026, 7, 30), "all") in fila["notas"]
    datas = dict(fila["dias"])
    assert date(2026, 7, 29) in datas, "dia dentro da janela sumiu da fila"
    assert date(2026, 7, 1) not in datas, "dia expirado não devia aparecer"
    assert datas[date(2026, 7, 29)] == _MP_RETRO_EXPIRA_DIAS - 4  # 02/08 - 29/07 = 4d


def test_listar_fila_e_read_only(monkeypatch) -> None:
    """Consultar a fila NÃO pode expirar/remover nada — senão ver a fila
    mudaria a fila. A expiração é responsabilidade do proativo."""
    tocou: list = []

    async def _spy(*a, **kw):
        tocou.append(a)

    monkeypatch.setattr(proactive, "unmark_notified", _spy)
    monkeypatch.setattr(proactive, "mark_notified", _spy)
    hoje = date(2026, 8, 2)
    rows = [_n("mp_pendente", "2026-06-01")]      # bem expirado
    asyncio.run(proactive.listar_fila_mp(_Sess(rows), 1, hoje))
    assert tocou == [], "listar a fila alterou o estado (efeito colateral)"


def test_listar_fila_ignora_chave_corrompida() -> None:
    hoje = date(2026, 8, 2)
    rows = [_n("mp_pendente", "lixo"), _n("nota_pendente", "sem-data")]
    fila = asyncio.run(proactive.listar_fila_mp(_Sess(rows), 1, hoje))
    assert fila["dias"] == []                  # data inválida descartada
    # nota corrompida (sem ":") não some: fica com data "?" e alvo "all" —
    # esconder uma nota pendente seria pior que mostrá-la degradada.
    assert fila["notas"] == [(None, "all")]


# ─────────────────────────── formatação (verbatim) ──────────────────────────

def test_fmt_alvo() -> None:
    assert _fmt_alvo("all") == "todas as MPs"
    assert _fmt_alvo("") == "todas as MPs"
    assert _fmt_alvo("1382") == "MP 1382"
    assert _fmt_alvo("1382,1383") == "MPs 1382, 1383"


def test_fmt_fila_vazia() -> None:
    out = _fmt_fila_mp({"notas": [], "dias": [], "manutencao": False})
    assert "Fila do DOU vazia" in out
    assert "manuten" not in out.lower()


def test_fmt_fila_vazia_com_manutencao() -> None:
    out = _fmt_fila_mp({"notas": [], "dias": [], "manutencao": True})
    assert "vazia" in out and "manutenção" in out


def test_fmt_fila_com_notas_e_dias() -> None:
    fila = {
        "notas": [(date(2026, 8, 1), "1382"), (date(2026, 7, 30), "all")],
        "dias": [(date(2026, 7, 29), 10)],
        "manutencao": True,
    }
    out = _fmt_fila_mp(fila)
    assert "MP 1382 de 01/08/2026" in out
    assert "todas as MPs de 30/07/2026" in out
    assert "29/07/2026 — re-checo por mais 10 dia(s)" in out
    assert "Inlabs em manutenção" in out
    assert "/mp_dou_agora" in out
