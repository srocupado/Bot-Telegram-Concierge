from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.db.models import User
from bot.services.llm.factory import get_provider_for_user

router = Router(name=__name__)
logger = logging.getLogger(__name__)


@router.message(Command("ping"))
async def cmd_ping(message: Message, user: User) -> None:
    try:
        provider = get_provider_for_user(user)
        model = getattr(provider, "model", "?")
        reply = await provider.ping()
        await message.answer(f"[{provider.name}/{model}] {reply or 'pong'}")
    except Exception as e:
        logger.exception("ping failed")
        await message.answer(f"❌ erro no LLM ({user.provider}): {e}")
