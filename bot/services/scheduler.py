from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import settings
from bot.db.models import DigestLog, Reminder, User
from bot.services.briefing import build_traffic_briefing
from bot.services.congress import fetch_mps, format_mps_message
from bot.services.reminders import due_reminders, mark_sent
from bot.utils.timez import is_within_window, parse_hhmm

logger = logging.getLogger(__name__)


async def scheduler_loop(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    logger.info(
        "scheduler started",
        extra={"tick": settings.scheduler_tick_seconds, "traffic": settings.traffic_daily_time, "mp": settings.mp_weekly_time},
    )
    while True:
        try:
            await _tick(bot, session_factory)
        except Exception:
            logger.exception("scheduler tick failed")
        await asyncio.sleep(settings.scheduler_tick_seconds)


async def _tick(bot: Bot, session_factory: async_sessionmaker[AsyncSession]) -> None:
    traffic_time = parse_hhmm(settings.traffic_daily_time)
    mp_time = parse_hhmm(settings.mp_weekly_time)
    now_utc = datetime.now(timezone.utc)

    async with session_factory() as session:
        users = await _authorized_users(session)
        for user in users:
            tz = ZoneInfo(user.timezone)
            now_local = now_utc.astimezone(tz)

            # Trânsito: seg-sex, na janela traffic_daily_time
            if now_local.weekday() < 5 and is_within_window(now_local, traffic_time):
                await _maybe_send_digest(
                    bot, session, user,
                    kind="traffic_daily",
                    today_local=now_local.date(),
                    build=build_traffic_briefing,
                )

            # MP: segunda, na janela mp_weekly_time
            if now_local.weekday() == 0 and is_within_window(now_local, mp_time):
                await _maybe_send_digest(
                    bot, session, user,
                    kind="mp_weekly",
                    today_local=now_local.date(),
                    build=_build_mp_message,
                )

            # Lembretes: qualquer hora
            await _send_due_reminders(bot, session, user, now_utc)


async def _authorized_users(session: AsyncSession) -> list[User]:
    result = await session.execute(select(User).where(User.is_authorized.is_(True)))
    return list(result.scalars().all())


async def _build_mp_message() -> str:
    items = await fetch_mps(limit=10)
    return format_mps_message(items)


async def _maybe_send_digest(
    bot: Bot,
    session: AsyncSession,
    user: User,
    *,
    kind: str,
    today_local,
    build,
) -> None:
    log = DigestLog(user_id=user.id, kind=kind, sent_date=today_local)
    session.add(log)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return  # já enviado hoje

    try:
        text = await build()
        await bot.send_message(user.chat_id, text, parse_mode="Markdown")
        logger.info("digest sent", extra={"user_id": user.id, "kind": kind})
    except Exception:
        logger.exception("digest send failed; reverting log", extra={"kind": kind})
        # Remove o registro pra permitir nova tentativa no próximo tick.
        try:
            await session.delete(log)
            await session.commit()
        except Exception:
            await session.rollback()


async def _send_due_reminders(bot: Bot, session: AsyncSession, user: User, now_utc: datetime) -> None:
    items = await due_reminders(session, user.id, now_utc)
    for rem in items:
        try:
            await bot.send_message(user.chat_id, f"🔔 *Lembrete*: {rem.text}", parse_mode="Markdown")
            await mark_sent(session, rem)
            logger.info("reminder sent", extra={"user_id": user.id, "reminder_id": rem.id})
        except Exception:
            logger.exception("reminder send failed", extra={"reminder_id": rem.id})
            await session.rollback()
