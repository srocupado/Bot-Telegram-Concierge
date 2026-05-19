from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import DigestLog, User
from bot.services.briefing import build_traffic_briefing
from bot.utils.timez import now_in

router = Router(name=__name__)
logger = logging.getLogger(__name__)


@router.message(Command("transito"))
async def cmd_transito(message: Message, user: User, session: AsyncSession) -> None:
    text = await build_traffic_briefing()
    await message.answer(text, parse_mode="Markdown")

    today_local = now_in(user.timezone).date()
    session.add(DigestLog(user_id=user.id, kind="traffic_manual", sent_date=today_local))
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("digest_log traffic_manual commit failed")
