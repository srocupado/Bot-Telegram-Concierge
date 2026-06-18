"""/buscar <termo> — busca web one-shot.

Backend de leitura de página (search_and_read): SearXNG+Jina como primário e
Firecrawl como fallback (conforme WEBSEARCH_BACKEND), com síntese curta pelo
provider do usuário. Cobre o caso de voz: a transcrição mapeia "busca X /
pesquisa X / procura X / google X" → /buscar, então fazer o /buscar ler página
dá leitura de página também por voz.

Último recurso (se nenhum backend de leitura estiver configurado, ou todos
falharem) → busca NATIVA one-shot:
- user.provider == 'anthropic' → Anthropic com web_search (server-side)
- senão → Gemini com google_search nativa
A chamada nativa é one-shot (sem tool use customizado), então não esbarra na
limitação do Gemini que impede combinar busca com function calling.
"""
from __future__ import annotations

import asyncio
import logging
import unicodedata

import anthropic
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from google import genai
from google.genai import types

from bot.config import settings
from bot.db.models import User
from bot.handlers.chat import answer_llm
from bot.services.chat_memory import memory
from bot.services.llm.factory import get_provider

logger = logging.getLogger(__name__)

router = Router(name="search")


_SEARCH_SYSTEM = (
    "Você é um assistente de pesquisa web em português brasileiro. "
    "Use a busca pra responder a consulta do usuário de forma curta, objetiva "
    "e com fontes no final. Se for sobre notícias, traga 3-5 manchetes com fonte.\n"
    "FORMATAÇÃO (Telegram): para destaque use *um asterisco* (NUNCA **dois**); "
    "NÃO use títulos '#'. Itens de lista começam com '- '. As fontes vão no "
    "final como links inline no formato [Título](URL) — NÃO use notas de rodapé "
    "tipo [[1]] nem âncoras (#r1); cada fonte numa linha começando com '- '."
)

# Prompt de síntese: o conteúdo das páginas já vem pronto (search_and_read);
# o LLM só destila a resposta. Pede pra usar SÓ o material fornecido (reduz
# alucinação) e citar os links.
_SYNTH_PROMPT = (
    "Consulta do usuário: {query}\n\n"
    "Abaixo, resultados de busca já COM o conteúdo das páginas lido. "
    "Responda a consulta usando SOMENTE esses dados, de forma curta e "
    "objetiva. Se a resposta exige dado específico (horário, preço, etc.), "
    "extraia-o do conteúdo. No final, liste as fontes usadas como links inline "
    "[Título](URL), uma por linha começando com '- ' — sem notas de rodapé "
    "[[1]] nem âncoras (#). Se o material não responder, diga isso.\n\n{context}"
)


async def _synth(query: str, context: str, user: User) -> str:
    """Sintetiza a resposta curta a partir do contexto (páginas lidas ou
    resultados do Google Shopping) com o provider do usuário."""
    provider = get_provider(user.provider)
    messages = [{"role": "user", "content": _SYNTH_PROMPT.format(query=query, context=context)}]
    return await provider.chat(messages, system=_SEARCH_SYSTEM, max_tokens=2000)


# Detecção de intenção de PREÇO de produto → roteia pro buscar_preco (Google
# Shopping: preço + loja + link do anúncio), em vez da busca web genérica.
_PRICE_INTENT = {
    "preco", "precos", "valor", "valores", "quanto", "custa", "custo",
    "onde", "comprar", "qual", "menor", "mais", "barato", "baratos", "qto",
}
_PRICE_CONNECTORS = {"o", "a", "os", "as", "do", "da", "de", "dos", "das", "e", "um", "uma"}


def _norm(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s.casefold())
        if not unicodedata.combining(c)
    )


def _is_price_query(q: str) -> bool:
    n = _norm(q)
    return (
        any(k in n for k in ("preco", "quanto custa", "onde comprar",
                             "mais barato", "menor preco", "qto custa"))
        or n.startswith("valor d")
    )


def _strip_price_words(q: str) -> str:
    """Remove o prefixo de intenção de preço pra sobrar só o produto.
    'preço do sérum X' → 'sérum X'; 'quanto custa o Y' → 'Y'."""
    toks = q.split()
    i = 0
    while i < len(toks) and _norm(toks[i]) in (_PRICE_INTENT | _PRICE_CONNECTORS):
        i += 1
    return " ".join(toks[i:]).strip() or q


def _anthropic_search(query: str) -> str:
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=2000,
        system=_SEARCH_SYSTEM,
        messages=[{"role": "user", "content": query}],
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,
        }],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return "".join(parts).strip()


def _gemini_search(query: str) -> str:
    client = genai.Client(api_key=settings.gemini_api_key)
    config = types.GenerateContentConfig(
        system_instruction=_SEARCH_SYSTEM,
        tools=[types.Tool(google_search=types.GoogleSearch())],
        max_output_tokens=2000,
    )
    resp = client.models.generate_content(
        model=settings.gemini_model,
        contents=query,
        config=config,
    )
    return (resp.text or "").strip()


async def _native_search(query: str, user: User) -> tuple[str, str]:
    """Busca nativa (fallback). Retorna (engine, resultado)."""
    use_anthropic = user.provider == "anthropic" and bool(settings.anthropic_api_key)
    if use_anthropic:
        return "anthropic", await asyncio.to_thread(_anthropic_search, query)
    if settings.gemini_api_key:
        return "gemini", await asyncio.to_thread(_gemini_search, query)
    raise RuntimeError("nenhum motor de busca nativo disponível")


@router.message(Command("buscar"))
async def cmd_buscar(message: Message, command: CommandObject, user: User) -> None:
    if not user.is_authorized:
        return
    query = (command.args or "").strip()
    if not query:
        await message.answer(
            "Uso: /buscar <termo>\n"
            "Ex: /buscar notícias políticas hoje",
            parse_mode=None,
        )
        return

    from bot.services.websearch import backend_available

    is_price = _is_price_query(query)
    # "rich" = consegue resposta com leitura/estruturada: backend web, ou (preço
    # + SerpAPI). O buscar_preco tem fallback web próprio.
    has_rich = backend_available() or (is_price and settings.serpapi_key is not None)
    has_native = (
        (user.provider == "anthropic" and bool(settings.anthropic_api_key))
        or bool(settings.gemini_api_key)
    )
    if not has_rich and not has_native:
        await message.answer(
            "⚠️ Busca indisponível: configure o backend de leitura de página "
            "(SEARXNG_URL e/ou FIRECRAWL_API_KEY) ou ANTHROPIC_API_KEY/"
            "GEMINI_API_KEY (busca nativa).",
            parse_mode=None,
        )
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    engine = ""
    result = ""
    # 1) Preço de produto → buscar_preco (Google Shopping). 2) Senão, leitura de
    # página (SearXNG→Firecrawl). 3) Fallback nativo.
    if has_rich:
        try:
            if is_price:
                from bot.services.precos import buscar_preco
                engine = "preco"
                context = await buscar_preco(_strip_price_words(query))
            else:
                engine = settings.websearch_backend
                from bot.services.websearch import search_and_read
                context = await search_and_read(query, read_content=True)
            result = await _synth(query, context, user)
        except Exception as e:
            logger.warning("/buscar (%s) falhou (%s) — tentando nativo", engine or "rich", e)
            engine, result = "", ""

    if not result:
        try:
            engine, result = await _native_search(query, user)
        except Exception as e:
            logger.exception("/buscar falhou (firecrawl+nativo)")
            await message.answer(f"❌ erro na busca: {e}", parse_mode=None)
            return

    if not result:
        await message.answer("(sem resposta)", parse_mode=None)
        return

    logger.info("/buscar served via %s, %d chars", engine, len(result))
    # Grava na MESMA memória do chat livre: o /buscar é handler separado, mas o
    # usuário trata como conversa ('tem link?', 'e o preço?'). Sem isto, o
    # follow-up no chat não enxerga o que foi buscado aqui e responde sobre um
    # produto antigo do contexto.
    memory.append(message.chat.id, "user", query)
    memory.append(message.chat.id, "assistant", result)
    # Mesma renderização do chat livre: converte o markdown do LLM (**negrito**,
    # bullets, citações) pro subset do Telegram, com fallback pra texto puro.
    # Sem isso o /buscar mostrava os asteriscos crus (enviava parse_mode=None).
    await answer_llm(message, result, disable_web_page_preview=True)
