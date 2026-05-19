from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User
from bot.services.tasks import create_task, list_open_tasks, mark_done

router = Router(name=__name__)


@router.message(Command("nova"))
async def cmd_nova(
    message: Message, command: CommandObject, user: User, session: AsyncSession
) -> None:
    text = (command.args or "").strip()
    if not text:
        await message.answer("Uso: /nova <descrição da tarefa>")
        return
    task = await create_task(session, user.id, text)
    await message.answer(f"✅ Tarefa #{task.id} criada.")


@router.message(Command("tarefas"))
async def cmd_tarefas(message: Message, user: User, session: AsyncSession) -> None:
    tasks = await list_open_tasks(session, user.id)
    if not tasks:
        await message.answer("📭 Nenhuma tarefa aberta. Use /nova para criar uma.")
        return
    now = datetime.now(timezone.utc)
    lines = ["📋 *Tarefas abertas*\n"]
    for t in tasks:
        age = _humanize_age(now - t.created_at)
        lines.append(f"• #{t.id} — {t.text}  _(há {age})_")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("feito"))
async def cmd_feito(
    message: Message, command: CommandObject, user: User, session: AsyncSession
) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Uso: /feito <id>")
        return
    task = await mark_done(session, user.id, int(arg))
    if task is None:
        await message.answer(f"Tarefa #{arg} não encontrada.")
        return
    await message.answer(f"✓ Tarefa #{task.id} concluída.")


def _humanize_age(delta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}min"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
