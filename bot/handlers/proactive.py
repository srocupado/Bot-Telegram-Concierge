"""Comandos do agente proativo (opt-in).

/proativo_on  /proativo_off  — liga/desliga avisos automáticos.
/proativo                    — status (janelas, briefing).
/proativo_agora [briefing]   — força a checagem agora (teste; ignora dedup).
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User

logger = logging.getLogger(__name__)
router = Router(name="proactive")


@router.message(Command("proativo_on"))
async def cmd_on(message: Message, user: User, session: AsyncSession) -> None:
    if not user.is_authorized:
        return
    user.proactive_enabled = True
    await session.commit()
    horas = settings.proactive_hours
    await message.answer(
        f"✅ Agente proativo ativado. Vou te avisar nas janelas {horas}h (BRT) "
        f"sobre vencimentos, novidades de MP e hábitos, com um briefing às "
        f"{settings.proactive_briefing_hour:02d}h. Teste agora com /proativo_agora.",
        parse_mode=None,
    )


@router.message(Command("proativo_off"))
async def cmd_off(message: Message, user: User, session: AsyncSession) -> None:
    if not user.is_authorized:
        return
    user.proactive_enabled = False
    await session.commit()
    await message.answer("🔕 Agente proativo desativado.", parse_mode=None)


@router.message(Command("proativo"))
async def cmd_status(message: Message, user: User) -> None:
    if not user.is_authorized:
        return
    estado = "ligado ✅" if user.proactive_enabled else "desligado 🔕"
    await message.answer(
        f"Agente proativo: {estado}\n"
        f"Janelas: {settings.proactive_hours}h (BRT) · briefing às "
        f"{settings.proactive_briefing_hour:02d}h\n"
        f"Antecedência de vencimentos: {settings.proactive_lookahead_hours}h\n"
        f"MP: {'acompanhando' if user.dou_mp_subscribed else 'desligado (use /mp_dou_on)'}\n"
        "Use /proativo_on, /proativo_off ou /proativo_agora.",
        parse_mode=None,
    )


@router.message(Command("proativo_agora"))
async def cmd_agora(
    message: Message, command: CommandObject, user: User, session: AsyncSession,
) -> None:
    if not user.is_authorized:
        return
    arg = (command.args or "").strip().lower()
    window = "briefing" if arg.startswith("brief") else "regular"
    await message.answer("🔎 Checando agora…", parse_mode=None)
    from bot.services.proactive import run_for_user
    now_brt = datetime.now(ZoneInfo(user.timezone))
    try:
        sent = await run_for_user(message.bot, session, user, now_brt, window=window, force=True)
    except Exception:
        logger.exception("proativo_agora failed")
        await message.answer("⚠️ Erro na checagem proativa.", parse_mode=None)
        return
    if not sent:
        await message.answer("Nada a avisar agora. 👍", parse_mode=None)
