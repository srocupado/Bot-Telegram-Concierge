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


def test_get_file_direto_devolve_o_file_path() -> None:
    from bot.handlers.voice import _get_file_direto

    bot = SimpleNamespace(token="123:ABC")

    async def _main():
        with respx.mock:
            respx.route(host="api.telegram.org", path__regex=r".*getFile").respond(
                200, json={"ok": True, "result": {"file_path": "voice/file_7.oga"}},
            )
            return await _get_file_direto(bot, "FILEID")

    assert asyncio.run(_main()) == "voice/file_7.oga"


def test_get_file_direto_not_ok_levanta_sem_vazar_token() -> None:
    from bot.handlers.voice import _get_file_direto

    bot = SimpleNamespace(token="123:ABC")

    async def _main():
        with respx.mock:
            respx.route(host="api.telegram.org", path__regex=r".*getFile").respond(
                200, json={"ok": False, "description": "file not found"},
            )
            try:
                await _get_file_direto(bot, "FILEID")
            except RuntimeError as e:
                return str(e)
            raise AssertionError("devia ter levantado")

    msg = asyncio.run(_main())
    assert "not-ok" in msg
    assert "123:ABC" not in msg, "token do bot vazou na mensagem de erro"
