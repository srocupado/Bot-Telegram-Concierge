from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User
from bot.services.llm.factory import SUPPORTED_PROVIDERS

router = Router(name=__name__)


@router.message(Command("provider"))
async def cmd_provider(
    message: Message, command: CommandObject, user: User, session: AsyncSession
) -> None:
    arg = (command.args or "").strip().lower()
    if not arg:
        opts = " | ".join(SUPPORTED_PROVIDERS)
        await message.answer(f"Provider atual: *{user.provider}*\n\nUse: /provider {opts}", parse_mode="Markdown")
        return
    if arg not in SUPPORTED_PROVIDERS:
        opts = ", ".join(SUPPORTED_PROVIDERS)
        await message.answer(f"Provider inválido. Opções: {opts}")
        return
    user.provider = arg
    await session.commit()
    await message.answer(f"✅ Provider definido como *{arg}*.", parse_mode="Markdown")
