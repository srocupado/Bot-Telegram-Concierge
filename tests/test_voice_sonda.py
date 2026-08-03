"""Sondas de diagnóstico do timeout de voz (03/08/2026, caso em aberto).

get_file estourava 20s com o polling vivo — as sondas rodam NO momento da
falha e discriminam: rede do Pi (httpx em conexão nova) × sessão do aiogram
(get_me pela mesma sessão). O teste garante que as sondas não estouram
exceção própria (telemetria não pode piorar a falha que observa).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import respx

from bot.handlers.voice import _diagnostico_pos_timeout


def test_sondas_logam_e_nao_estouram(caplog) -> None:
    async def _get_me():
        return SimpleNamespace(username="bot")

    bot = SimpleNamespace(get_me=_get_me)

    async def _main():
        with respx.mock:
            respx.route(host="api.telegram.org").respond(200, text="ok")
            await _diagnostico_pos_timeout(bot)

    with caplog.at_level("WARNING"):
        asyncio.run(_main())
    msgs = " | ".join(r.getMessage() for r in caplog.records)
    assert "conexão NOVA (httpx): HTTP 200" in msgs
    assert "get_me pela sessão do bot: ok" in msgs


def test_sondas_sobrevivem_a_tudo_falhando(caplog) -> None:
    async def _get_me_hang():
        await asyncio.sleep(60)

    bot = SimpleNamespace(get_me=_get_me_hang)

    async def _main():
        with respx.mock:
            respx.route(host="api.telegram.org").mock(
                side_effect=httpx.ConnectError("unreachable"),
            )
            # get_me penduraria 60s — o wait_for(8) interno tem que cortar.
            await asyncio.wait_for(_diagnostico_pos_timeout(bot), timeout=30)

    with caplog.at_level("WARNING"):
        asyncio.run(_main())
    msgs = " | ".join(r.getMessage() for r in caplog.records)
    assert "conexão NOVA (httpx) FALHOU" in msgs
    assert "get_me pela sessão do bot FALHOU" in msgs
