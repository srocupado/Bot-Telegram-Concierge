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

import anthropic
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from google import genai
from google.genai import types

from bot.config import settings
from bot.db.models import User
from bot.services.llm.factory import get_provider

logger = logging.getLogger(__name__)

router = Router(name="search")


_SEARCH_SYSTEM = (
    "Você é um assistente de pesquisa web em português brasileiro. "
    "Use a busca pra responder a consulta do usuário de forma curta, objetiva "
    "e com fontes (titulos/links breves no final). Se for sobre notícias, "
    "traga 3-5 manchetes com fonte."
)

# Prompt de síntese: o conteúdo das páginas já vem pronto (search_and_read);
# o LLM só destila a resposta. Pede pra usar SÓ o material fornecido (reduz
# alucinação) e citar os links.
_SYNTH_PROMPT = (
    "Consulta do usuário: {query}\n\n"
    "Abaixo, resultados de busca já COM o conteúdo das páginas lido. "
    "Responda a consulta usando SOMENTE esses dados, de forma curta e "
    "objetiva. Se a resposta exige dado específico (horário, preço, etc.), "
    "extraia-o do conteúdo. Cite os links usados no final. Se o material não "
    "responder, diga isso.\n\n{context}"
)


async def _read_and_synth(query: str, user: User) -> str:
    """Backend de leitura de página (SearXNG+Jina ou Firecrawl, conforme
    WEBSEARCH_BACKEND + fallback) lê as páginas; o provider do usuário sintetiza."""
    from bot.services.websearch import search_and_read

    context = await search_and_read(query, read_content=True)
    provider = get_provider(user.provider)
    messages = [{"role": "user", "content": _SYNTH_PROMPT.format(query=query, context=context)}]
    return await provider.chat(messages, system=_SEARCH_SYSTEM, max_tokens=2000)


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

    has_rich = backend_available()  # SearXNG (URL) e/ou Firecrawl (key)
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
    # 1) Leitura de página (SearXNG→Firecrawl) — preferida. 2) Fallback nativo.
    if has_rich:
        try:
            engine = settings.websearch_backend
            result = await _read_and_synth(query, user)
        except Exception as e:
            logger.warning("/buscar leitura de página falhou (%s) — tentando nativo", e)
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
    await message.answer(result, parse_mode=None, disable_web_page_preview=True)
