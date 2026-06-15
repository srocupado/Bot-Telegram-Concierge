"""Busca de PREÇO de produto (tool buscar_preco).

Primário: SerpAPI Google Shopping → preço + loja + link DIRETO do anúncio
(fonte estruturada, vence o anti-bot/login wall dos marketplaces).

Degradação graciosa: se o SerpAPI falhar (cota esgotada = 429/erro, ou fora do
ar) OU não retornar itens, cai pro `buscar_web` (cadeia SearXNG→Firecrawl) —
aí o preço ainda vem (via snippet/página), só sem os links por loja. O pior
caso é o comportamento da busca web comum, nunca um erro duro.

Obs: a cota SerpAPI é compartilhada com voo/hotel.
"""
from __future__ import annotations

import logging

from bot.config import settings
from bot.services.travels.serpapi_client import (
    SerpAPIClient,
    SerpAPIError,
    extract_shopping_results,
    format_shopping,
)

logger = logging.getLogger(__name__)


async def buscar_preco(query: str) -> str:
    # 1) SerpAPI Google Shopping (fonte estruturada).
    if settings.serpapi_key is not None:
        try:
            async with SerpAPIClient(settings.serpapi_key.get_secret_value()) as serpapi:
                raw = await serpapi.search_shopping(query)
            items = extract_shopping_results(raw)
            if items:
                logger.info("buscar_preco[shopping]: %d itens para %r", len(items), query)
                return format_shopping(query, items)
            logger.info("buscar_preco[shopping]: sem itens para %r — fallback web", query)
        except SerpAPIError as e:
            logger.warning("buscar_preco: SerpAPI falhou (%s) — fallback web", e)

    # 2) Fallback: busca web com leitura de página.
    from bot.services.websearch import WebSearchError, search_and_read

    try:
        context = await search_and_read(f"preço {query} Brasil comprar", read_content=True)
    except WebSearchError as e:
        return f"erro: não consegui preços (SerpAPI e busca web indisponíveis): {e}"
    return (
        "(Google Shopping indisponível — preço APROXIMADO da busca web; os links "
        "podem ser de página de busca, não do anúncio específico)\n\n" + context
    )
