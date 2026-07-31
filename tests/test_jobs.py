"""Trabalhos longos em background (bot/services/jobs.py).

Regressão que originou o módulo: a nota técnica do DOU rodava dentro do
handler do botão, segurando a sessão de banco do update por minutos enquanto o
usuário esperava. O contrato aqui é: dispara e volta na hora, não duplica, e
falha não vira silêncio.
"""
from __future__ import annotations

import asyncio
import logging

from bot.services import jobs


def test_dispara_e_devolve_na_hora() -> None:
    async def main() -> float:
        marcos: list[str] = []

        async def demorado() -> None:
            await asyncio.sleep(0.3)
            marcos.append("job terminou")

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert jobs.spawn("k1", lambda: demorado()) is True
        gasto = loop.time() - t0          # tempo que o "handler" levou
        marcos.append("handler respondeu")
        await asyncio.sleep(0.4)
        assert marcos == ["handler respondeu", "job terminou"]
        return gasto

    assert asyncio.run(main()) < 0.05


def test_nao_dispara_duplicado() -> None:
    async def main() -> None:
        contador = {"n": 0}

        async def job() -> None:
            contador["n"] += 1
            await asyncio.sleep(0.2)

        assert jobs.spawn("k2", lambda: job()) is True
        assert jobs.spawn("k2", lambda: job()) is False, "segundo clique duplicou"
        assert jobs.job_em_andamento("k2")
        await asyncio.sleep(0.3)
        assert contador["n"] == 1
        # terminou → a chave libera pra um pedido novo
        assert not jobs.job_em_andamento("k2")
        assert jobs.spawn("k2", lambda: job()) is True
        await asyncio.sleep(0.3)

    asyncio.run(main())


def test_falha_do_job_vai_pro_log_e_nao_derruba_nada(caplog) -> None:
    async def main() -> None:
        async def explode() -> None:
            raise RuntimeError("boom")

        with caplog.at_level(logging.ERROR):
            assert jobs.spawn("k3", lambda: explode()) is True
            await asyncio.sleep(0.05)
        assert any("boom" in r.getMessage() or r.exc_info for r in caplog.records)

    asyncio.run(main())


def test_chave_livre_depois_do_fim() -> None:
    async def main() -> None:
        async def rapido() -> None:
            return None

        jobs.spawn("k4", lambda: rapido())
        await asyncio.sleep(0.05)
        assert "k4" not in jobs.jobs_ativos()

    asyncio.run(main())
