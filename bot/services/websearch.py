"""Busca web com LEITURA de conteúdo — usada pela tool `buscar_web` e pelo
`/buscar`. Diferente do `web_search` server-side (que devolve síntese a partir
de *snippets*), aqui a página é LIDA: retorna o markdown já renderizado, então
funciona pra dados que só existem dentro do corpo da página e mudam com o tempo
— horários de sessão de cinema, funcionamento, preços, cardápios, tabelas.

Dois backends, encadeados primário → fallback (mesmo contrato `search_and_read`
→ texto pronto pro LLM):

  • "searxng" (padrão/primário) — custo ZERO, self-hosted:
        1. SearXNG (metabusca) → links (GET /search?...&format=json).
        2. Jina Reader (https://r.jina.ai/<url>) → lê cada link e devolve
           markdown renderizado (com JS). Tier gratuito; JINA_API_KEY sobe o
           rate limit. Exige SEARXNG_URL.
  • "firecrawl" (fallback) — turnkey: search + scrape (render de JS) num call.
        Qualidade alta out-of-the-box; gasta créditos (free tier tem teto).

O PRIMÁRIO vem de WEBSEARCH_BACKEND; se ele falhar (rede, JSON desabilitado,
engines indisponíveis...) e WEBSEARCH_FALLBACK=true, o outro é tentado. Um
backend sem credencial é PULADO (não conta como falha). Ver README → "Busca web".
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from bot.config import settings

logger = logging.getLogger(__name__)

FIRECRAWL_SEARCH_ENDPOINT = "https://api.firecrawl.dev/v1/search"
JINA_READER_PREFIX = "https://r.jina.ai/"

# Tetos pra controlar custo/tokens e o tamanho enviado ao LLM.
_DEFAULT_LIMIT = 5
_MAX_RESULTS = 10
_MAX_CHARS_PER_PAGE = 3500
_TIMEOUT_S = 60.0  # scrape/leitura com render de JS pode demorar.


class WebSearchError(Exception):
    pass


def _configured(backend: str) -> bool:
    if backend == "searxng":
        return bool(settings.searxng_url)
    return settings.firecrawl_api_key is not None


def _backend_order() -> list[str]:
    """Primário (WEBSEARCH_BACKEND) seguido do outro, se o fallback estiver on."""
    primary = settings.websearch_backend
    other = "firecrawl" if primary == "searxng" else "searxng"
    return [primary, other] if settings.websearch_fallback else [primary]


def backend_available() -> bool:
    """True se ALGUM backend (searxng/firecrawl) está configurado."""
    return any(_configured(b) for b in ("searxng", "firecrawl"))


async def search_and_read(
    query: str,
    *,
    limit: int = _DEFAULT_LIMIT,
    read_content: bool = True,
) -> str:
    """Busca na web e (por padrão) LÊ as páginas, devolvendo texto pronto pro
    LLM sintetizar com as fontes. Tenta o backend primário e cai pro fallback
    se ele falhar. Levanta WebSearchError se todos os configurados falharem (ou
    se nenhum estiver configurado).

    read_content=False → só títulos/links/descrição (sem ler página): mais
    rápido e barato.
    """
    limit = max(1, min(limit, _MAX_RESULTS))
    errors: list[str] = []
    ran_any = False
    for backend in _backend_order():
        if not _configured(backend):
            continue
        ran_any = True
        try:
            return await _run_backend(backend, query, limit, read_content)
        except WebSearchError as e:
            logger.warning("buscar_web: backend '%s' falhou — %s", backend, e)
            errors.append(f"{backend}: {e}")

    if not ran_any:
        raise WebSearchError(
            "nenhum backend de busca configurado (defina SEARXNG_URL e/ou FIRECRAWL_API_KEY)"
        )
    raise WebSearchError("todos os backends de busca falharam — " + "; ".join(errors))


async def _run_backend(backend: str, query: str, limit: int, read_content: bool) -> str:
    if backend == "searxng":
        return await _searxng_backend(query, limit, read_content)
    return await _firecrawl_backend(query, limit, read_content)


# ───────────────────────── SearXNG + Jina ─────────────────────────────────

async def _searxng_backend(query: str, limit: int, read_content: bool) -> str:
    base = (settings.searxng_url or "").rstrip("/")
    if not base:
        raise WebSearchError("SEARXNG_URL não configurada")

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        # 1) Metabusca → links.
        try:
            resp = await client.get(f"{base}/search", params={"q": query, "format": "json"})
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise WebSearchError(f"SearXNG request failed: {e}") from e
        try:
            raw = resp.json().get("results") or []
        except ValueError as e:  # devolveu HTML → format=json desabilitado
            raise WebSearchError(
                "SearXNG não retornou JSON — habilite 'json' em search.formats no settings.yml"
            ) from e
        results = raw[:limit]
        # Vazio é tratado como falha pra acionar o fallback (ex.: engines 429).
        if not results:
            raise WebSearchError("SearXNG sem resultados (engines indisponíveis?)")

        # 2) Leitura de cada link via Jina Reader (concorrente).
        if read_content:
            await _attach_jina_markdown(client, results)

    norm = [
        {
            "title": r.get("title"),
            "url": r.get("url"),
            "description": r.get("content"),
            "markdown": r.get("markdown"),
        }
        for r in results
    ]
    logger.info("buscar_web[searxng]: %d resultados para %r (read=%s)", len(norm), query, read_content)
    return _format_results(query, norm)


async def _attach_jina_markdown(client: httpx.AsyncClient, results: list[dict]) -> None:
    """Lê cada URL via Jina Reader e grava em result['markdown']. Falha de uma
    página não derruba as outras (cai pro snippet do SearXNG)."""
    headers = {"X-Return-Format": "markdown"}
    if settings.jina_api_key is not None:
        headers["Authorization"] = f"Bearer {settings.jina_api_key.get_secret_value()}"

    async def _read(item: dict) -> None:
        url = (item.get("url") or "").strip()
        if not url:
            return
        try:
            r = await client.get(f"{JINA_READER_PREFIX}{url}", headers=headers, timeout=_TIMEOUT_S)
            r.raise_for_status()
            item["markdown"] = r.text
        except httpx.HTTPError as e:
            logger.warning("jina read falhou p/ %s: %s", url, e)

    await asyncio.gather(*(_read(r) for r in results))


# ─────────────────────────── Firecrawl ────────────────────────────────────

async def _firecrawl_backend(query: str, limit: int, read_content: bool) -> str:
    if settings.firecrawl_api_key is None:
        raise WebSearchError("FIRECRAWL_API_KEY não configurada")

    body: dict = {"query": query, "limit": limit}
    if read_content:
        body["scrapeOptions"] = {"formats": ["markdown"]}  # search + scrape
    headers = {
        "Authorization": f"Bearer {settings.firecrawl_api_key.get_secret_value()}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(FIRECRAWL_SEARCH_ENDPOINT, json=body, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise WebSearchError(f"Firecrawl request failed: {e}") from e

    payload = resp.json()
    if not payload.get("success", True):
        raise WebSearchError(f"Firecrawl error: {payload.get('error') or payload}")
    results = payload.get("data") or []
    if not results:
        raise WebSearchError("Firecrawl sem resultados")

    logger.info("buscar_web[firecrawl]: %d resultados para %r (read=%s)", len(results), query, read_content)
    return _format_results(query, results)


# ─────────────────────────── Formatação ───────────────────────────────────

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
