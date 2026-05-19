from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import DigestLog, User
from bot.services.congress import fetch_mps, format_mps_message
from bot.utils.timez import now_in

router = Router(name=__name__)
logger = logging.getLogger(__name__)


@router.message(Command("mp"))
async def cmd_mp(message: Message, user: User, session: AsyncSession) -> None:
    items = await fetch_mps(limit=10)
    text = format_mps_message(items)
    await message.answer(text, parse_mode="Markdown")

    today_local = now_in(user.timezone).date()
    session.add(DigestLog(user_id=user.id, kind="mp_manual", sent_date=today_local))
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("digest_log mp_manual commit failed")
