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
import re

import httpx

from bot.config import settings

logger = logging.getLogger(__name__)

FIRECRAWL_SEARCH_ENDPOINT = "https://api.firecrawl.dev/v1/search"
JINA_READER_PREFIX = "https://r.jina.ai/"

# Tetos pra controlar custo/tokens, latência e o tamanho enviado ao LLM.
# _DEFAULT_LIMIT = nº de páginas buscadas E lidas por consulta. Lê via Jina é o
# passo mais lento (render de JS por página), então menos páginas = mais rápido.
# 3 costuma bastar pra dados objetivos (horário/preço); suba se a qualidade cair.
_DEFAULT_LIMIT = 3
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


def _normalize_site(site: str) -> str:
    """'https://www.loja.com.br/x' | 'www.loja.com.br' → 'loja.com.br' (domínio
    pro operador site:). Mantém subdomínio real (ex.: 'loja.exemplo.com')."""
    s = (site or "").strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = s.split("/")[0].strip()
    return s[4:] if s.startswith("www.") else s


async def search_and_read(
    query: str,
    *,
    limit: int = _DEFAULT_LIMIT,
    read_content: bool = True,
    site: str | None = None,
) -> str:
    """Busca na web e (por padrão) LÊ as páginas, devolvendo texto pronto pro
    LLM sintetizar com as fontes. Tenta o backend primário e cai pro fallback
    se ele falhar. Levanta WebSearchError se todos os configurados falharem (ou
    se nenhum estiver configurado).

    read_content=False → só títulos/links/descrição (sem ler página): mais
    rápido e barato.

    site='loja.com.br' → restringe ao domínio (operador `site:`), pro caso
    "quero o preço NESSA loja". Sem isso, o buscador podia nunca devolver a
    página da loja pedida e o modelo concluía que o site estava inacessível.
    """
    if site:
        dominio = _normalize_site(site)
        if dominio and f"site:{dominio}" not in query:
            query = f"site:{dominio} {query}".strip()
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


# 50k chars ≈ 12k tokens — caro se fosse rotina, aceitável porque ler_pagina é
# chamada deliberada. Motivo do aumento: página de BUSCA de loja chega a 68k
# brutos e, mesmo sem o menu, passava do teto e era truncada — e o produto
# certo podia estar justamente no pedaço cortado (foi o bug do "loja de
# parafusos"). Rede de segurança; o caminho bom é ler a página do PRODUTO.
_MAX_PAGE_CHARS = 50000

# Linha de MENU/navegação no markdown do Jina: bullet com UM link de texto
# curto, sem imagem e sem mais nada. Produto/artigo real traz imagem
# ([![Image N: ...]]), preço ou texto ao redor — e sobrevive ao filtro.
# Motivo: página de loja vinha com um menu de categorias GIGANTE na frente; o
# truncamento guardava o menu ("Parafusos, Porcas, Pregos…") e descartava os
# produtos, e o modelo concluía "essa loja é de parafusos" (caso real).
# Medido no site da Pires Martins: 47.687 → 19.948 chars, com os 63 preços
# preservados.
_NAV_LINE = re.compile(r"^\s*\*\s*\[[^\]\[]{1,45}\]\(https?://[^)]+\)\s*$")


# Piso de sobrevivência do filtro: quando a página INTEIRA é bullet-link curto
# (home de portal de notícias, índice de blog, sumário), o filtro levava junto
# TODAS as manchetes e o modelo concluía "não há notícias" — falha de leitura
# virando falso negativo, que é o pecado que este projeto não aceita. Nesse
# caso devolvemos o texto original: menu a mais é ruído; conteúdo a menos é
# mentira.
_NAV_MIN_RESTANTE = 400          # chars
_NAV_MIN_FRACAO = 0.15           # do tamanho original


def _strip_nav(texto: str) -> str:
    """Remove linhas de menu/navegação, preservando conteúdo (produtos, preços,
    texto corrido). Reduz drasticamente o boilerplate antes do truncamento.

    Se sobrar quase nada, desiste do filtro (ver _NAV_MIN_*)."""
    limpo = "\n".join(ln for ln in texto.split("\n") if not _NAV_LINE.match(ln))
    if texto and (
        len(limpo.strip()) < _NAV_MIN_RESTANTE
        and len(limpo.strip()) < len(texto.strip()) * _NAV_MIN_FRACAO
    ):
        logger.info(
            "_strip_nav: filtro comeria a página (%d → %d chars) — mantendo o "
            "original (provável índice/home de links)",
            len(texto.strip()), len(limpo.strip()),
        )
        return texto
    return limpo

# O Jina falha de forma TRANSITÓRIA sob rajada (o buscar_web lê todos os
# resultados em paralelo e o ler_pagina pode disparar junto): observado 422 numa
# URL que, repetida segundos depois, devolve 200 com o conteúdo. 429/5xx idem.
# Mesmo padrão de retry usado em camara/inlabs/brapi.
_JINA_RETRY_STATUS = {408, 422, 429, 500, 502, 503, 504}
_JINA_RETRIES = 3
# Teto de tempo do LOTE de leitura do buscar_web. Sem isto o pior caso era
# ~10 min (N URLs × 3 tentativas × 60s, em série) — o usuário ficava sem
# resposta e uma tarefa do agente agendado (timeout 900s) morria com duas
# chamadas ruins. Quem estourar o orçamento fica sem markdown e cai no
# snippet do buscador, que é degradação aceitável.
_LEITURA_BUDGET_S = 90.0


def _jina_headers() -> dict[str, str]:
    h = {"X-Return-Format": "markdown"}
    if settings.jina_api_key is not None:
        h["Authorization"] = f"Bearer {settings.jina_api_key.get_secret_value()}"
    return h


async def _jina_get(client: httpx.AsyncClient, url: str) -> str:
    """GET no Jina Reader com retry/backoff em falha transitória. Levanta o
    último erro httpx se todas as tentativas falharem."""
    last: Exception | None = None
    for tentativa in range(_JINA_RETRIES):
        try:
            r = await client.get(
                f"{JINA_READER_PREFIX}{url}", headers=_jina_headers(), timeout=_TIMEOUT_S,
            )
            r.raise_for_status()
            return r.text
        except httpx.HTTPStatusError as e:
            last = e
            if e.response.status_code not in _JINA_RETRY_STATUS:
                raise
            logger.warning(
                "jina %s em %s (tentativa %d/%d)",
                e.response.status_code, url, tentativa + 1, _JINA_RETRIES,
            )
        except httpx.HTTPError as e:
            last = e
            logger.warning("jina %s em %s (tentativa %d/%d)",
                           type(e).__name__, url, tentativa + 1, _JINA_RETRIES)
        if tentativa < _JINA_RETRIES - 1:
            await asyncio.sleep(1.5 * (tentativa + 1))
    raise last  # type: ignore[misc]


# Sites que barram IP de DATACENTER mas aceitam residencial. O Jina Reader
# busca a partir dos servidores DELE (datacenter), então a página volta como
# muro de login por mais que o bot esteja num IP doméstico. Medido em
# 02/08/2026 com o Mercado Livre: do IP do Orange Pi a URL do produto abre
# normal; do datacenter, todo caminho (browser, curl, 4 user-agents de rede
# social, Jina) cai em /gz/account-verification.
#
# Pra estes domínios a leitura sai DIRETO do bot. Perde-se a renderização de
# JS do Jina; ganha-se a página, que é o que importa.
_DIRETO_RE = re.compile(r"(^|\.)mercado(livre|libre)\.com(\.br)?$", re.IGNORECASE)

# Marcas de que caímos no muro mesmo indo direto — o ML responde 200 e
# redireciona, então status não denuncia nada.
_MURO_RE = re.compile(r"account-verification|/gz/|login\.mercadolivre", re.IGNORECASE)

_UA_NAVEGADOR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _direto(url: str) -> bool:
    from urllib.parse import urlparse
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return bool(_DIRETO_RE.search(host))


async def _ler_direto(url: str) -> str:
    """Busca a página pelo IP do próprio bot e extrai o texto.

    Levanta WebSearchError quando o destino final é o muro de login: dizer
    "não consegui ler porque o site exigiu login" é honesto; devolver o HTML
    do muro faria o LLM inventar resposta em cima de uma página de erro.
    """
    from bs4 import BeautifulSoup
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=_TIMEOUT_S, headers=_UA_NAVEGADOR,
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        final = str(r.url)
        if _MURO_RE.search(final):
            raise WebSearchError(
                "o site redirecionou para verificação/login em vez da página "
                f"(caiu em {final[:120]})"
            )
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "svg"]):
            tag.decompose()
        texto = soup.get_text("\n", strip=True)
    if len(texto) < 200:
        raise WebSearchError(f"a página voltou quase vazia ({len(texto)} chars)")
    logger.info("ler_pagina: %s lido DIRETO (%d chars)", url, len(texto))
    return texto


async def read_url(url: str, *, max_chars: int = _MAX_PAGE_CHARS) -> str:
    """Lê UMA página específica via Jina Reader e devolve o markdown renderizado.

    Complementa o search_and_read: serve pra 'vê o preço nesse link' (URL que o
    usuário colou) e pra ler a busca interna de uma loja. O Jina renderiza JS e
    atravessa proteção anti-bot que derruba fetch direto — testado no site da
    Pires Martins (403 direto por desafio Cloudflare, 200 via Jina).
    """
    u = (url or "").strip()
    if not u:
        raise WebSearchError("url vazia")
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    if _direto(u):
        # Domínio que barra datacenter: vai direto, sem passar pelo Jina.
        texto = await _ler_direto(u)
        bruto = len(texto)
        cortado = len(texto) > max_chars
        if cortado:
            texto = texto[:max_chars]
        logger.info("ler_pagina[direto]: %s (%d chars brutos → %d%s)",
                    u, bruto, len(texto), ", TRUNCADO" if cortado else "")
        return _montar_saida(u, texto, cortado)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT_S) as client:
            texto = await _jina_get(client, u)
    except httpx.HTTPStatusError as e:
        raise WebSearchError(
            f"não consegui ler a página (HTTP {e.response.status_code})"
        ) from e
    except httpx.HTTPError as e:
        raise WebSearchError(f"não consegui ler a página ({type(e).__name__})") from e
    if not texto.strip():
        raise WebSearchError("a página voltou vazia")
    bruto = len(texto)
    texto = _strip_nav(texto)
    cortado = len(texto) > max_chars
    if cortado:
        texto = texto[:max_chars]
    logger.info(
        "ler_pagina: %s (%d chars brutos → %d%s)",
        u, bruto, len(texto), ", TRUNCADO" if cortado else "",
    )
    return _montar_saida(u, texto, cortado)


def _montar_saida(u: str, texto: str, cortado: bool) -> str:
    """Cabeçalho + aviso de truncamento. Compartilhado pelas duas vias de
    leitura (Jina e direto) — texto duplicado divergiria com o tempo."""
    cabec = f"Conteúdo lido de {u}:\n\n"
    # Aviso forte: o modelo já concluiu "essa loja não tem o produto" a partir
    # de página truncada — ausência em trecho parcial NÃO é ausência.
    rodape = (
        "\n\n[⚠️ PÁGINA TRUNCADA — este é só um TRECHO. NÃO conclua que algo "
        "não existe/não está à venda por não aparecer aqui. Se não achou o que "
        "procurava, leia outra URL (ex.: a busca do site com o termo exato) "
        "antes de responder que não há.]"
    ) if cortado else ""
    return cabec + texto + rodape


async def _attach_jina_markdown(client: httpx.AsyncClient, results: list[dict]) -> None:
    """Lê cada URL via Jina Reader (com retry) e grava em result['markdown'].
    Falha de uma página não derruba as outras (cai pro snippet do SearXNG).

    Leitura SERIALIZADA de propósito: em paralelo, a rajada fazia o Jina
    devolver 422 em URL que sozinha responde 200 (caso real: página de produto
    da Pires Martins). Poucos resultados (_DEFAULT_LIMIT) — o custo em tempo é
    aceitável perto de perder a página."""
    import time as _time
    inicio = _time.monotonic()
    for item in results:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        if _time.monotonic() - inicio > _LEITURA_BUDGET_S:
            logger.warning(
                "buscar_web: orçamento de leitura (%.0fs) esgotado; %s e demais "
                "ficam só com o snippet", _LEITURA_BUDGET_S, url[:60],
            )
            break
        try:
            # _strip_nav TAMBÉM aqui: o fix do "essa loja é de parafusos" só
            # tinha entrado no ler_pagina. Aqui o corte é de 3.500 chars
            # (_MAX_CHARS_PER_PAGE) — MUITO menor —, então uma página de loja
            # com menu grande virava só menu no contexto do modelo.
            item["markdown"] = _strip_nav(await _jina_get(client, url))
        except httpx.HTTPError as e:
            logger.warning("jina read falhou p/ %s (após retries): %s", url, e)


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
