"""Bug #1 da auditoria: nota que FALHA na re-tentativa não pode sumir da fila.

Cenário: a nota está na fila (`nota_pendente`). A cada janela o proativo chama
`_entregar_nota_pendente` → `deliver_to_user(force=True)`. Se a geração falha
por algo que NÃO é DouError (Gemini 500, build_docx, send_document),
`_nota_e_docx` engole a exceção e `deliver_to_user` retorna normalmente.

Antes, `_entregar_nota_pendente` dava `unmark_notified(key)` INCONDICIONAL — a
entrada sumia, a nota nunca mais era re-tentada e o aviso de desistência (14
dias) nunca disparava. O pior modo de falha do projeto (perda silenciosa),
justamente na área que o commit do outbox dizia ter blindado.

Agora `deliver_to_user` devolve `(entregues, falhas)` e a baixa só ocorre com
`falhas == []`.
"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from bot.services import proactive

D = date(2026, 8, 1)
KEY = "2026-08-01:1381"


class _Sessao:
    async def get(self, _modelo, _id):
        return SimpleNamespace(id=_id, is_authorized=True, dou_mp_subscribed=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self):
        return None


@pytest.fixture
def espiao(monkeypatch):
    reg = {"unmark": []}

    async def _unmark(_s, _uid, kind, key):
        reg["unmark"].append((kind, key))

    # SessionLocal() é usado como `async with` dentro da função.
    from bot.db import session as db_session
    monkeypatch.setattr(db_session, "SessionLocal", lambda: _Sessao())
    monkeypatch.setattr(proactive, "unmark_notified", _unmark)
    return reg


def _entregar(monkeypatch, *, falhas):
    async def _deliver(bot, session, user, d, *, force, only_numeros):
        return (1, list(falhas))

    from bot.services import dou_monitor
    monkeypatch.setattr(dou_monitor, "deliver_to_user", _deliver)
    asyncio.run(proactive._entregar_nota_pendente(None, 42, D, ["1381"], KEY))


def test_nota_que_falha_fica_na_fila(espiao, monkeypatch) -> None:
    _entregar(monkeypatch, falhas=["1381"])
    assert espiao["unmark"] == [], (
        "a nota falhou mas a entrada foi baixada — sumiu da fila, sem re-tentativa"
    )


def test_nota_que_entrega_sai_da_fila(espiao, monkeypatch) -> None:
    _entregar(monkeypatch, falhas=[])
    assert espiao["unmark"] == [("nota_pendente", KEY)], (
        "entrega sem falha tem que dar baixa — senão re-gera pra sempre"
    )
