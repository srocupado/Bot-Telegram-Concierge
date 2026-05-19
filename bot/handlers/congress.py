from __future__ import annotations

import logging
from datetime import datetime

import httpx
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User
from bot.services.congress import (
    USER_AGENT,
    CongressScrapeError,
    fetch_week_mps,
    format_week_message,
)
from bot.services.scheduler import BRT, CONGRESS_HOUR

logger = logging.getLogger(__name__)

router = Router(name="congress")


@router.message(Command("congresso_on"))
async def cmd_congress_on(message: Message, user: User, session: AsyncSession) -> None:
    user.congress_subscribed = True
    await session.commit()
    await message.answer(
        "🏛️ Inscrito no resumo semanal de MPs. Toda segunda às 07:00 (BRT)."
    )


@router.message(Command("congresso_off"))
async def cmd_congress_off(message: Message, user: User, session: AsyncSession) -> None:
    user.congress_subscribed = False
    await session.commit()
    await message.answer("🏛️ Resumo semanal de MPs cancelado.")


def _parse_hhmm(s: str) -> tuple[int, int] | None:
    parts = s.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


@router.message(Command("congresso_at"))
async def cmd_congress_at(
    message: Message, command: CommandObject, user: User, session: AsyncSession
) -> None:
    arg = (command.args or "").strip()
    if not arg:
        user.congress_hour = None
        user.congress_minute = None
        await session.commit()
        await message.answer(
            f"⏰ Horário do digest de MPs voltou pro default ({CONGRESS_HOUR:02d}:00 BRT)."
        )
        return
    parsed = _parse_hhmm(arg)
    if parsed is None:
        await message.answer(
            "Uso: /congresso_at HH:MM (ex: /congresso_at 08:15). "
            "Sem argumento volta pro default."
        )
        return
    user.congress_hour, user.congress_minute = parsed
    user.last_congress_digest_at = None
    await session.commit()
    await message.answer(
        f"⏰ Digest de MPs agendado para {parsed[0]:02d}:{parsed[1]:02d} BRT (segundas). "
        f"Marca de envio da semana zerada."
    )


@router.message(Command("congresso_reset"))
async def cmd_congress_reset(message: Message, user: User, session: AsyncSession) -> None:
    user.last_congress_digest_at = None
    await session.commit()
    await message.answer(
        "✅ Marca de envio da semana zerada. No próximo tick o digest sai "
        "(se for segunda e a hora agendada já passou)."
    )


@router.message(Command("congresso_agora"))
async def cmd_congress_agora(message: Message) -> None:
    today = datetime.now(BRT).date()
    try:
        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            items = await fetch_week_mps(client, today)
    except CongressScrapeError:
        logger.exception("congresso_agora scrape failed")
        await message.answer(
            "⚠️ Não consegui acessar a agenda do Congresso agora. "
            "Tenta de novo em alguns minutos."
        )
        return

    text = format_week_message(items, today)
    try:
        await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logger.exception("HTML send failed in /congresso_agora")
        await message.answer(text, parse_mode=None, disable_web_page_preview=True)
