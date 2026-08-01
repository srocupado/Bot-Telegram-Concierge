"""Baixa da pendência retroativa vale também no /proativo_agora.

O bloco pós-envio era `if sent and not force`, então a execução manual pulava
tudo — inclusive o `unmark_notified` da pendência. Consequências observadas em
01/08/2026: dois `/proativo_agora` seguidos repetiram
"✅ Checagem retroativa do DOU de 29/07 concluída", e cada execução re-baixou
os ZIPs daquele dia (~100-200MB no Orange Pi) para chegar à mesma conclusão.

Dedup de aviso e baixa de estado são coisas diferentes: o force pula o
primeiro de propósito (teste não pode silenciar a janela real), mas o dia foi
REALMENTE re-checado e o resultado foi entregue.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from bot.services import proactive

RETRO = date(2026, 7, 29)


class _FakeSession:
    async def scalars(self, _stmt):
        return []

    async def commit(self):
        return None


def _rodar(monkeypatch, *, force: bool):
    """run_for_user com um único fato mp_retro; devolve (marcados, desmarcados)."""
    marcados: list[tuple[str, str]] = []
    desmarcados: list[tuple[str, str]] = []

    async def _nada(*a, **kw):
        return []

    async def _retro(*a, **kw):
        return [proactive.ProactiveFact(
            "mp", "mp_retro", f"retro:{RETRO.isoformat()}",
            "✅ Checagem retroativa do DOU de 29/07 concluída — nenhuma MP nova.",
        )]

    async def _mark(_s, _uid, kind, key):
        marcados.append((kind, key))

    async def _unmark(_s, _uid, kind, key):
        desmarcados.append((kind, key))

    async def _false(*a, **kw):
        return False

    async def _send(*a, **kw):
        return True

    async def _redigir(_user, texto):
        return texto

    monkeypatch.setattr(proactive, "collect_mp", _retro)
    for coletor in ("collect_vencimentos", "collect_tarefas", "collect_nudges",
                    "collect_carteira", "collect_clima", "collect_transito",
                    "collect_moeda_viagem"):
        monkeypatch.setattr(proactive, coletor, _nada)
    monkeypatch.setattr(proactive, "mark_notified", _mark)
    monkeypatch.setattr(proactive, "unmark_notified", _unmark)
    monkeypatch.setattr(proactive, "already_notified", _false)
    monkeypatch.setattr(proactive, "_send", _send)
    monkeypatch.setattr(proactive, "_redigir", _redigir)
    monkeypatch.setattr(proactive, "_processar_notas_pendentes", _nada)

    user = SimpleNamespace(
        id=99, timezone="America/Sao_Paulo", dou_mp_subscribed=True,
        dou_ultimo_dia_ok=date.today() - timedelta(days=1), provider="anthropic",
    )
    asyncio.run(proactive.run_for_user(
        None, _FakeSession(), user, datetime.now(proactive.BRT),
        window="regular", force=force,
    ))
    return marcados, desmarcados


@pytest.mark.parametrize("force", [False, True])
def test_pendencia_recebe_baixa_com_e_sem_force(monkeypatch, force) -> None:
    """A baixa é estado, não dedup: vale nos dois modos.

    Sem isso, cada /proativo_agora re-baixa o DOU do dia e repete a linha
    "✅ concluída" indefinidamente.
    """
    _, desmarcados = _rodar(monkeypatch, force=force)
    assert ("mp_pendente", RETRO.isoformat()) in desmarcados


def test_force_continua_sem_consumir_dedup(monkeypatch) -> None:
    """O motivo de o force pular o bloco existia e não pode ser perdido:
    execução de teste não silencia a janela real."""
    marcados, _ = _rodar(monkeypatch, force=True)
    assert marcados == [], "force marcou dedup e vai silenciar a janela real"


def test_sem_force_marca_dedup_normalmente(monkeypatch) -> None:
    marcados, _ = _rodar(monkeypatch, force=False)
    assert ("mp_retro", f"retro:{RETRO.isoformat()}") in marcados
