"""Busca de PREÇO de produto (tool buscar_preco) — HÍBRIDO.

Por que híbrido: o Google Shopping é ótimo pra DESCOBRIR quem vende (loja +
link direto), mas o preço dele vem de feed das lojas e chega atrasado —
frequentemente errado, e com confusão de variante ('Fly More' vs 'Fly Smart').
Já a leitura da página traz o preço REAL daquele momento, mas não sabe onde
procurar sozinha.

Fluxo:
  1) Google Shopping (SerpAPI) → lista de ofertas em ordem de relevância;
  2) LÊ a página do 1º resultado (Jina, atravessa anti-bot em boa parte das
     lojas) → preço CONFIRMADO na fonte;
  3) devolve as duas coisas rotuladas, com precedência explícita da página.

Degradação em cada etapa, sempre dizendo o que aconteceu:
  - página não abriu (marketplace que bloqueia, ex. Mercado Livre) → lista do
    Shopping marcada como NÃO confirmada;
  - Shopping sem itens/fora/sem cota → busca web com leitura (comportamento
    antigo).

Obs: a cota SerpAPI é compartilhada com voo/hotel.
"""
from __future__ import annotations

import logging
import re as _re_mod

from bot.config import settings
from bot.services.travels.serpapi_client import (
    SerpAPIClient,
    SerpAPIError,
    extract_shopping_results,
    format_shopping,
)

logger = logging.getLogger(__name__)

# Trecho da página lido pra confirmar preço. Menor que o teto do ler_pagina
# (50k): aqui só interessa a região do preço, e isso entra no contexto do LLM
# em TODA consulta de preço — 8k chars ≈ 2k tokens é o equilíbrio.
_PAGINA_CHARS = 8000


# Domínios que NÃO são a loja: agregador/redirect do próprio Google. Ler um
# desses e chamar de "confirmado na página da loja" seria carimbar procedência
# falsa em cima do MESMO feed defasado que a confirmação deveria checar —
# `product_link` do SerpAPI é sempre google.com/shopping/product/..., e item
# patrocinado vem como google.com/aclk?... / google.com/url?...
_DOMINIOS_NAO_LOJA = (
    "google.com", "google.com.br", "googleadservices.com", "googleusercontent.com",
)


def _e_pagina_de_loja(url: str) -> bool:
    from urllib.parse import urlparse

    p = urlparse(url or "")
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower().removeprefix("www.")
    return bool(host) and not any(
        host == d or host.endswith("." + d) for d in _DOMINIOS_NAO_LOJA
    )


async def _confirmar_na_pagina(url: str) -> str | None:
    """Lê a página da oferta e devolve o trecho pro LLM conferir o preço.
    None se não abrir (loja que bloqueia, timeout) OU se a URL não for da
    loja (agregador/redirect do Google — ver _DOMINIOS_NAO_LOJA)."""
    if not url or not url.startswith("http"):
        return None
    if not _e_pagina_de_loja(url):
        logger.info(
            "buscar_preco: link do 1º resultado não é da loja (%s) — sem confirmação",
            url[:80],
        )
        return None
    from bot.services.websearch import WebSearchError, read_url

    try:
        return await read_url(url, max_chars=_PAGINA_CHARS)
    except WebSearchError as e:
        logger.info("buscar_preco: não confirmou na página %s (%s)", url[:80], e)
        return None
    except Exception:
        logger.warning("buscar_preco: erro inesperado lendo %s", url[:80], exc_info=True)
        return None


# Contexto que marca um número como DINHEIRO (teto, orçamento) e não modelo.
_DINHEIRO_CTX = _re_mod.compile(
    r"(r\$|reais?|abaixo de|até|ate|menos de|max(?:imo)?|teto|or[çc]amento|"
    r"custa|pre[çc]o|valor)\s*$", _re_mod.IGNORECASE,
)


def _tokens_numericos(texto: str, checar_contexto: bool = False) -> set[str]:
    """Tokens com dígito ('360', '13', '4070', '9600x') — é o que distingue
    MODELO ('Avata 2' vs 'Avata 360'). Minúsculas, sem pontuação.

    Descarta só o que é claramente DINHEIRO: valor formatado ('6.900,00') e,
    quando `checar_contexto`, número precedido de marcador de preço ('abaixo
    de 6900', 'até 300 reais'). Números de 4 dígitos e sufixo 'x' NÃO são mais
    descartados às cegas — isso engolia RTX 4070 e Ryzen 9600X, justamente a
    família onde trocar de modelo é o erro mais caro.
    """
    out: set[str] = set()
    for m in _re_mod.finditer(r"\d[\w.,]*", (texto or "").lower()):
        t = m.group(0).rstrip(".,")
        if "," in t or "." in t:               # 6.900,00 / 3.303,00 → dinheiro
            continue
        if checar_contexto and _DINHEIRO_CTX.search((texto or "").lower()[:m.start()]):
            continue                            # "abaixo de 6900", "até 300"
        out.add(t)
        # '256gb' também casa como '256' (título costuma separar: "256 GB")
        mnum = _re_mod.match(r"^(\d+)[a-z]+$", t)
        if mnum:
            out.add(mnum.group(1))
    return out


def _aviso_modelo_divergente(query: str, items: list[dict], user_text: str = "") -> str:
    """Aviso quando os resultados são de OUTRO modelo. Compara com os títulos:
    - os tokens da QUERY (pega o Shopping devolvendo outro produto), e
    - os tokens do TEXTO ORIGINAL do usuário (pega o LLM REESCREVENDO a busca:
      caso real — usuário pediu 'Avata 360', o modelo não conhecia o produto
      [lançado após seu treino], 'corrigiu' pra 'Avata 2' ANTES de buscar, e
      o aviso antigo ficava mudo porque query e títulos combinavam entre si).
    Devolve '' quando tudo bate."""
    alvo = _tokens_numericos(query) | _tokens_numericos(user_text, checar_contexto=True)
    if not alvo:
        return ""
    titulos = " ".join(str(i.get("title") or "") for i in items)
    achados = _tokens_numericos(titulos)

    def _satisfeito(tok: str) -> bool:
        if tok in achados:
            return True
        # '256gb' é satisfeito por '256' no título ("256 GB" separado) e
        # vice-versa — unidade colada não pode virar divergência de modelo.
        m = _re_mod.match(r"^(\d+)[a-z]+$", tok)
        if m and m.group(1) in achados:
            return True
        return any(_re_mod.match(rf"^{tok}[a-z]+$", a) for a in achados)

    faltando = {t for t in alvo if not _satisfeito(t)}
    if not faltando:
        return ""
    return (
        f"⚠️ MODELO DIVERGENTE: o usuário pediu algo com "
        f"'{', '.join(sorted(faltando))}' e os resultados NÃO contêm isso no "
        "título — são de OUTRO modelo/versão (talvez a busca tenha sido "
        "reescrita; produtos recentes existem mesmo que você não os conheça — "
        "seu treino tem data de corte). DIGA a troca ao usuário explicitamente, "
        "apresente os preços como sendo do modelo ENCONTRADO, e refaça a busca "
        "com as palavras EXATAS do usuário se ainda não fez.\n\n"
    )


async def buscar_preco(query: str, user_text: str = "") -> str:
    # 1) Google Shopping — descobre QUEM vende (loja + link direto).
    items: list[dict] = []
    if settings.serpapi_key is not None:
        try:
            async with SerpAPIClient(settings.serpapi_key.get_secret_value()) as serpapi:
                raw = await serpapi.search_shopping(query)
            items = extract_shopping_results(raw)
            logger.info("buscar_preco[shopping]: %d itens para %r", len(items), query)
        except SerpAPIError as e:
            logger.warning("buscar_preco: SerpAPI falhou (%s) — fallback web", e)

    if items:
        lista = _aviso_modelo_divergente(query, items, user_text) + format_shopping(query, items)
        # 2) Confirma o preço lendo a página do 1º (mais relevante).
        primeiro = items[0]
        pagina = await _confirmar_na_pagina(primeiro.get("link") or "")
        if pagina:
            logger.info("buscar_preco: preço confirmado na página de %s",
                        primeiro.get("source") or "?")
            return (
                lista
                + "\n\n=== PÁGINA DO 1º RESULTADO (fonte direta) ===\n"
                + pagina
                + "\n\nPRECEDÊNCIA: o preço que vale é o desta PÁGINA — a lista "
                "acima vem do Google Shopping, que usa feed das lojas e costuma "
                "estar desatualizado. Se os dois divergirem, informe o da página "
                "e diga que o do Shopping estava defasado. Cite a loja e o link."
            )
        return (
            lista
            + "\n\n⚠️ NÃO consegui abrir a página do 1º resultado pra confirmar "
            "(loja bloqueia leitura automática). Os preços acima são do Google "
            "Shopping (feed das lojas) e podem estar DESATUALIZADOS — apresente-os "
            "como referência, não como preço atual, e mande o usuário conferir no "
            "link."
        )

    # 3) Sem Shopping (sem itens, sem cota ou fora do ar): busca web com leitura.
    from bot.services.websearch import WebSearchError, search_and_read

    try:
        context = await search_and_read(f"preço {query} Brasil comprar", read_content=True)
    except WebSearchError as e:
        return f"erro: não consegui preços (SerpAPI e busca web indisponíveis): {e}"
    return (
        "(Google Shopping sem resultado — preço vindo da busca web com leitura "
        "de página; os links podem ser de página de busca, não do anúncio)\n\n"
        + context
    )
