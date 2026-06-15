"""Busca web com LEITURA de conteúdo via Firecrawl (search + scrape).

Diferente do `web_search` server-side da Anthropic/Gemini (que devolve uma
síntese a partir de *snippets*), o Firecrawl BUSCA e LÊ as páginas: retorna
o markdown já renderizado (com JS). Por isso funciona pra dados que só
existem DENTRO do corpo da página e mudam com o tempo — horários de sessão
de cinema, horário de funcionamento, preços atuais, cardápios, tabelas.

Teste de referência que motivou isto: "que horas tem sessão do filme X no
Cinemark do shopping Y?". Busca por snippet acha a página certa mas NÃO traz
os horários; só lendo a página (search + scrape) eles aparecem.

═══════════════════════════════════════════════════════════════════════════
NOTA / ALTERNATIVA — SearXNG + Jina Reader (se o Firecrawl não satisfizer)
═══════════════════════════════════════════════════════════════════════════
O Firecrawl foi escolhido por ser turnkey (busca + leitura + render de JS
numa chamada só) e ter qualidade alta out-of-the-box. PORÉM o free tier tem
teto de créditos. Se a QUALIDADE não satisfizer OU o CUSTO ficar alto, dá
pra trocar este backend por uma dupla self-hosted de custo ZERO, com o MESMO
mecanismo (busca + leitura de página com render de JS):

  1. SearXNG  → metabusca self-hosted; devolve os links.
     - Suba junto no docker-compose (imagem `searxng/searxng`), habilite o
       formato JSON no settings.yml e consulte GET /search?q=...&format=json.
  2. Jina Reader (https://r.jina.ai/<url>) → lê cada link e devolve markdown
     já renderizado. Tier gratuito generoso; com JINA_API_KEY o limite sobe.

O contrato desta função (`search_and_read` → texto pronto pro LLM) foi
mantido fino DE PROPÓSITO: trocar de backend é só reescrever o corpo daqui,
sem tocar na tool `buscar_web` nem no agente. Ver README → "Busca web".
═══════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import logging

import httpx

from bot.config import settings

logger = logging.getLogger(__name__)

FIRECRAWL_SEARCH_ENDPOINT = "https://api.firecrawl.dev/v1/search"

# Tetos pra controlar custo (créditos Firecrawl) e tokens enviados ao LLM.
_DEFAULT_LIMIT = 5
_MAX_RESULTS = 10
_MAX_CHARS_PER_PAGE = 3500
_TIMEOUT_S = 60.0  # scrape com render de JS pode demorar — timeout folgado.


class WebSearchError(Exception):
    pass


async def search_and_read(
    query: str,
    *,
    limit: int = _DEFAULT_LIMIT,
    read_content: bool = True,
) -> str:
    """Busca na web e (por padrão) LÊ as páginas, devolvendo texto pronto pro
    LLM sintetizar com as fontes. Levanta WebSearchError em falha de config
    ou de rede.

    read_content=False → só títulos/links/descrição (SERP), mais rápido e
    barato (não gasta créditos de scrape).
    """
    if settings.firecrawl_api_key is None:
        raise WebSearchError("FIRECRAWL_API_KEY não configurada no .env")

    body: dict = {"query": query, "limit": max(1, min(limit, _MAX_RESULTS))}
    if read_content:
        # Pede o markdown renderizado de cada resultado (search + scrape).
        body["scrapeOptions"] = {"formats": ["markdown"]}

    headers = {
        "Authorization": f"Bearer {settings.firecrawl_api_key.get_secret_value()}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(
                FIRECRAWL_SEARCH_ENDPOINT, json=body, headers=headers,
            )
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise WebSearchError(f"Firecrawl request failed: {e}") from e

    payload = resp.json()
    if not payload.get("success", True):
        raise WebSearchError(f"Firecrawl error: {payload.get('error') or payload}")

    results = payload.get("data") or []
    if not results:
        return f"(sem resultados para: {query})"

    logger.info("buscar_web: %d resultados para %r (read=%s)", len(results), query, read_content)
    return _format_results(query, results)


def _format_results(query: str, results: list[dict]) -> str:
    blocks: list[str] = [f"Resultados de busca para: {query}"]
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip() or "(sem título)"
        url = (r.get("url") or "").strip()
        desc = (r.get("description") or "").strip()
        content = (r.get("markdown") or "").strip()

        block = [f"\n[{i}] {title}", url]
        if desc:
            block.append(desc)
        if content:
            excerpt = content[:_MAX_CHARS_PER_PAGE]
            if len(content) > _MAX_CHARS_PER_PAGE:
                excerpt += " […]"
            block.append("--- conteúdo da página ---")
            block.append(excerpt)
        blocks.append("\n".join(block))
    return "\n".join(blocks)
