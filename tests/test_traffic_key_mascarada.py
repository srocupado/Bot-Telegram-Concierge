"""A chave da Google Maps API não pode vazar na mensagem de erro.

Bug da auditoria de 03/08/2026: `httpx.HTTPStatusError.__str__` inclui a
URL completa da requisição — com `&key=AIzaSy…` — e o TrafficError a
embutia crua. A mensagem ia pro log persistido (docker logs) e, pior,
voltava como tool result pro provedor de LLM (`tools.py: f"erro: {e}"`),
que podia ecoá-la no chat. O geocoding.py já mascarava; o traffic.py não.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from bot.services.traffic import DIRECTIONS_ENDPOINT, TrafficError, fetch_traffic

_KEY = "AIzaSySEGREDO-nao-pode-vazar"


def test_erro_http_mascara_a_chave() -> None:
    async def _main() -> str:
        with respx.mock:
            respx.route(url__startswith=DIRECTIONS_ENDPOINT).respond(403, text="Forbidden")
            async with httpx.AsyncClient() as client:
                with pytest.raises(TrafficError) as ei:
                    await fetch_traffic(client, _KEY, "-15.7,-47.8", "-15.8,-47.9", [])
        return str(ei.value)

    msg = asyncio.run(_main())
    assert _KEY not in msg, "chave da API vazou na mensagem de erro"
    assert "***" in msg, "máscara ausente — o erro perdeu o contexto da URL?"
