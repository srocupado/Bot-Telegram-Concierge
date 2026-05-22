"""/buscar <termo> — busca web one-shot.

Escolha de provider automática:
- user.provider == 'anthropic' → Anthropic com web_search (server-side tool)
- caso contrário → Gemini com google_search nativa

A chamada é one-shot (sem tool use customizado), então não compete com a
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

logger = logging.getLogger(__name__)

router = Router(name="search")


_SEARCH_SYSTEM = (
    "Você é um assistente de pesquisa web em português brasileiro. "
    "Use a busca pra responder a consulta do usuário de forma curta, objetiva "
    "e com fontes (titulos/links breves no final). Se for sobre notícias, "
    "traga 3-5 manchetes com fonte."
)


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

    # Escolha de motor: Anthropic se ativo + key disponível, senão Gemini.
    use_anthropic = user.provider == "anthropic" and bool(settings.anthropic_api_key)
    use_gemini = bool(settings.gemini_api_key)

    if not use_anthropic and not use_gemini:
        await message.answer(
            "⚠️ Nem ANTHROPIC_API_KEY nem GEMINI_API_KEY configurados — "
            "/buscar precisa de pelo menos um.",
            parse_mode=None,
        )
        return

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        if use_anthropic:
            engine = "anthropic"
            result = await asyncio.to_thread(_anthropic_search, query)
        else:
            engine = "gemini"
            result = await asyncio.to_thread(_gemini_search, query)
    except Exception as e:
        logger.exception("/buscar failed via %s", "anthropic" if use_anthropic else "gemini")
        await message.answer(f"❌ erro na busca: {e}", parse_mode=None)
        return

    if not result:
        await message.answer("(sem resposta)", parse_mode=None)
        return

    logger.info("/buscar served via %s, %d chars", engine, len(result))
    await message.answer(result, parse_mode=None, disable_web_page_preview=True)
