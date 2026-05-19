from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.services.chat_memory import memory

router = Router(name=__name__)


@router.message(Command("reset"))
async def cmd_reset(message: Message) -> None:
    memory.reset(message.chat.id)
    await message.answer("🧹 Contexto da conversa limpo.")
