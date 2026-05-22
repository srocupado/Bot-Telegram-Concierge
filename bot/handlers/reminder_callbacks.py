"""Callback handlers para botões inline de lembretes (snooze / done)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Reminder, User

logger = logging.getLogger(__name__)

router = Router(name="reminder_callbacks")


@router.callback_query(F.data.startswith("snz:"))
async def cb_snooze(query: CallbackQuery, user: User, session: AsyncSession) -> None:
    """callback_data = 'snz:<minutos>:<reminder_id>'"""
    try:
        _, mins_s, rid_s = query.data.split(":", 2)
        mins = int(mins_s)
        rid = int(rid_s)
    except (ValueError, AttributeError):
        await query.answer("⚠️ callback inválido", show_alert=True)
        return

    rem = await session.get(Reminder, rid)
    if rem is None or rem.user_id != user.id:
        await query.answer("⚠️ lembrete não encontrado", show_alert=True)
        return

    new_due = datetime.now(timezone.utc) + timedelta(minutes=mins)
    rem.due_at = new_due
    rem.sent = False
    rem.sent_at = None
    await session.commit()

    local = new_due.astimezone(ZoneInfo(user.timezone))
    await query.answer(f"💤 Adiado para {local.strftime('%H:%M')}")
    try:
        await query.message.edit_text(
            f"💤 <i>Adiado:</i> {rem.text}\n   → {local.strftime('%d/%m %H:%M')}",
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception:
        logger.exception("failed to edit snoozed reminder message")


@router.callback_query(F.data.startswith("done:"))
async def cb_done(query: CallbackQuery, user: User, session: AsyncSession) -> None:
    """callback_data = 'done:<reminder_id>'"""
    try:
        _, rid_s = query.data.split(":", 1)
        rid = int(rid_s)
    except (ValueError, AttributeError):
        await query.answer("⚠️ callback inválido", show_alert=True)
        return

    rem = await session.get(Reminder, rid)
    if rem is None or rem.user_id != user.id:
        await query.answer("⚠️ lembrete não encontrado", show_alert=True)
        return

    rem.sent = True
    rem.sent_at = datetime.now(timezone.utc)
    await session.commit()

    await query.answer("✅ Concluído")
    try:
        await query.message.edit_text(
            f"✅ <i>Concluído:</i> {rem.text}",
            parse_mode="HTML",
            reply_markup=None,
        )
    except Exception:
        logger.exception("failed to edit done reminder message")
