"""Ajustes de rede na sessão do bot (medições de 03/08/2026 no Orange Pi).

1) IPv4 forçado: de dentro do container, connect v6 à api.telegram.org =
   ENETUNREACH; v4 = 0.2s. Com o AAAA morto no cache de DNS de 1h do
   aiogram (ttl_dns_cache=3600), todo estabelecimento de conexão novo
   tentava um caminho quebrado. family=AF_INET tira a classe da equação.
2) Keepalive longo (55s vs default 15s): o caminho até o DC do Telegram
   perde pacote intermitente e o custo cai TODO no handshake de conexão
   nova (get_file_s medidos 0.5→17.7s na escada de retransmissão 1/3/7/15s;
   transferência em conexão aberta sempre <1s). Reusar conexões por mais
   tempo reduz quantos handshakes se paga.

Os ajustes usam o atributo privado `_connector_init` do AiohttpSession
(aceitável com aiogram PINADO em 3.20.0.post0) — estes testes quebram
ANTES do runtime se um bump de versão mudar a forma do atributo.
"""
from __future__ import annotations

import asyncio
import socket

import aiohttp
from aiogram.client.session.aiohttp import AiohttpSession


def test_connector_init_aceita_family_e_keepalive() -> None:
    sess = AiohttpSession()
    assert isinstance(sess._connector_init, dict), (
        "aiogram mudou a forma de _connector_init — revise o ajuste no runner"
    )
    # Mesmos dois kwargs que o runner injeta.
    sess._connector_init["family"] = socket.AF_INET
    sess._connector_init["keepalive_timeout"] = 55.0

    async def _main():
        # O connector real tem que aceitar os kwargs — é assim que a sessão o cria.
        conn = sess._connector_type(**sess._connector_init)
        try:
            return conn._family, conn._keepalive_timeout
        finally:
            await conn.close()

    family, keepalive = asyncio.run(_main())
    assert family == socket.AF_INET
    assert keepalive == 55.0


def test_tcpconnector_family_e_keepalive_sao_kwargs_suportados() -> None:
    async def _main():
        conn = aiohttp.TCPConnector(family=socket.AF_INET, keepalive_timeout=55.0)
        try:
            return conn._family, conn._keepalive_timeout
        finally:
            await conn.close()

    family, keepalive = asyncio.run(_main())
    assert family == socket.AF_INET
    assert keepalive == 55.0
