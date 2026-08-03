"""Sessão do bot forçada a IPv4 (medição de 03/08/2026 no Orange Pi).

De dentro do container: connect v6 à api.telegram.org = ENETUNREACH;
v4 = 0.2s. Com o AAAA morto no cache de DNS de 1h do aiogram
(ttl_dns_cache=3600), todo estabelecimento de conexão novo tentava um
caminho quebrado. family=AF_INET tira a classe inteira da equação.

O ajuste usa o atributo privado `_connector_init` do AiohttpSession
(aceitável com aiogram PINADO em 3.20.0.post0) — estes testes quebram
ANTES do runtime se um bump de versão mudar a forma do atributo.
"""
from __future__ import annotations

import asyncio
import socket

import aiohttp
from aiogram.client.session.aiohttp import AiohttpSession


def test_connector_init_aceita_family() -> None:
    sess = AiohttpSession()
    assert isinstance(sess._connector_init, dict), (
        "aiogram mudou a forma de _connector_init — revise o ajuste no runner"
    )
    sess._connector_init["family"] = socket.AF_INET

    async def _main():
        # O connector real tem que aceitar o kwarg — é assim que a sessão o cria.
        conn = sess._connector_type(**sess._connector_init)
        try:
            return conn._family
        finally:
            await conn.close()

    assert asyncio.run(_main()) == socket.AF_INET


def test_tcpconnector_family_e_kwarg_suportado() -> None:
    async def _main():
        conn = aiohttp.TCPConnector(family=socket.AF_INET)
        try:
            return conn._family
        finally:
            await conn.close()

    assert asyncio.run(_main()) == socket.AF_INET
