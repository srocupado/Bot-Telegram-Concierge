from __future__ import annotations

from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User
from bot.services.reminders import (
    ReminderParseError,
    create_reminder,
    list_pending,
    parse_reminder,
)

router = Router(name=__name__)


@router.message(Command("lembrar"))
async def cmd_lembrar(
    message: Message, command: CommandObject, user: User, session: AsyncSession
) -> None:
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            "Uso: /lembrar <texto> + data/hora\n"
            "Exemplos:\n"
            "• /lembrar ligar pro João em 2h\n"
            "• /lembrar reunião amanhã 09:00\n"
            "• /lembrar pegar encomenda sexta 18h"
        )
        return

    try:
        clean_text, due_utc = parse_reminder(raw, user.timezone)
    except ReminderParseError as e:
        await message.answer(f"⚠️ {e}")
        return

    rem = await create_reminder(session, user.id, clean_text, due_utc)
    local = due_utc.astimezone(ZoneInfo(user.timezone))
    await message.answer(
        f"🔔 *{clean_text}*\n"
        f"   marcado para {local.strftime('%d/%m %H:%M')} _(#{rem.id})_",
        parse_mode="Markdown",
    )


@router.message(Command("lembretes"))
async def cmd_lembretes(message: Message, user: User, session: AsyncSession) -> None:
    items = await list_pending(session, user.id)
    if not items:
        await message.answer("📭 Nenhum lembrete pendente.")
        return
    tz = ZoneInfo(user.timezone)
    lines = ["🔔 *Lembretes pendentes*\n"]
    for r in items:
        local = r.due_at.astimezone(tz)
        lines.append(f"• #{r.id} — {local.strftime('%d/%m %H:%M')} — {r.text}")
    await message.answer("\n".join(lines), parse_mode="Markdown")
