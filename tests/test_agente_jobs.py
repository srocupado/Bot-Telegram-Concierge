"""Tarefas do agente sob jobs.spawn + drenagem no shutdown.

Bug da auditoria de 03/08/2026: os 5 disparos do agente usavam
`asyncio.create_task` cru — o event loop guarda só weakref, e a task MAIS
LONGA do bot (até 2h) podia ser coletada pelo GC no meio: sumia sem log, o
lock do runner ficava preso pra sempre ("já existe uma tarefa em
andamento" até o restart) e nem o /agente_parar salvava. É o footgun que o
próprio jobs.py existe pra corrigir (e que memoria.py já pagou uma vez).

E no shutdown, o fim do asyncio.run matava as tasks de jobs sem aviso a
cada deploy — jobs.drenar dá até N segundos pra terminarem e cancela com
registro o que sobrar.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot.handlers import agent
from bot.services import jobs


@pytest.fixture(autouse=True)
def _jobs_limpos():
    for k in list(jobs._jobs):
        jobs._jobs.pop(k, None)
    yield
    for k in list(jobs._jobs):
        jobs._jobs.pop(k, None)


def test_start_background_task_roda_sob_jobs_com_dedup(monkeypatch) -> None:
    """A task fica registrada no jobs (referência forte) e um segundo pedido
    com a primeira ainda viva volta 'busy' em vez de duplicar."""
    trava = None
    rodou: list[str] = []

    async def _fake_run(bot, chat_id, prompt, sid, *, scheduled=False):
        rodou.append(prompt)
        await trava.wait()

    monkeypatch.setattr(agent, "_run_and_report", _fake_run)
    monkeypatch.setattr(agent, "_bot", object())
    monkeypatch.setattr(agent, "runner", SimpleNamespace(enabled=True, busy=False))

    async def _main():
        nonlocal trava
        trava = asyncio.Event()
        r1 = agent.start_background_task("tarefa 1", 42)
        assert jobs.job_em_andamento("agente:42"), (
            "task do agente fora do registro de jobs (sem referência forte)"
        )
        r2 = agent.start_background_task("tarefa 2", 42)
        trava.set()
        await asyncio.sleep(0)
        return r1, r2

    r1, r2 = asyncio.run(_main())
    assert r1 == "started"
    assert r2 == "busy", "segundo disparo devia ser recusado pelo dedup"
    assert rodou == ["tarefa 1"], "a segunda tarefa não podia ter rodado"


def test_drenar_espera_o_rapido_e_cancela_o_pendurado() -> None:
    fim: list[str] = []

    async def _rapido():
        await asyncio.sleep(0.01)
        fim.append("rapido")

    async def _pendurado():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            fim.append("cancelado")
            raise

    async def _main():
        jobs.spawn("rapido", _rapido)
        jobs.spawn("pendurado", _pendurado)
        return await jobs.drenar(0.5)

    cancelados = asyncio.run(_main())
    assert cancelados == ["pendurado"]
    assert "rapido" in fim, "job rápido devia ter terminado no drain"
    assert "cancelado" in fim, "job pendurado devia ter sido CANCELADO, não abandonado"


def test_drenar_sem_jobs_e_instantaneo() -> None:
    assert asyncio.run(jobs.drenar(5.0)) == []
