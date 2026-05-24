"""Comandos do monitor de MPs no Diário Oficial (Inlabs/DOU).

/mp_dou_on  /mp_dou_off  — assina/desassina o digest diário (18h BRT).
/mp_dou_agora [AAAA-MM-DD] — força a busca de hoje (ou data dada).
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User
from bot.services.dou_monitor import DouError, deliver_to_user

logger = logging.getLogger(__name__)
router = Router(name="dou_mp")


@router.message(Command("mp_dou_on"))
async def cmd_on(message: Message, user: User, session: AsyncSession) -> None:
    if not user.is_authorized:
        return
    user.dou_mp_subscribed = True
    await session.commit()
    await message.answer(
        f"✅ Monitor de MPs no DOU ativado. Você recebe as MPs novas "
        f"todo dia às {settings.dou_mp_hour:02d}h (se houver). "
        "Use /mp_dou_agora pra checar agora.",
        parse_mode=None,
    )


@router.message(Command("mp_dou_off"))
async def cmd_off(message: Message, user: User, session: AsyncSession) -> None:
    if not user.is_authorized:
        return
    user.dou_mp_subscribed = False
    await session.commit()
    await message.answer("🔕 Monitor de MPs no DOU desativado.", parse_mode=None)


@router.message(Command("mp_dou_agora"))
async def cmd_agora(
    message: Message, command: CommandObject, user: User, session: AsyncSession,
) -> None:
    if not user.is_authorized:
        return
    arg = (command.args or "").strip()
    if arg:
        try:
            target = date.fromisoformat(arg)
        except ValueError:
            await message.answer("Data inválida. Use AAAA-MM-DD.", parse_mode=None)
            return
    else:
        target = datetime.now(ZoneInfo(user.timezone)).date()

    await message.answer(
        f"🔎 Buscando MPs publicadas no DOU em {target.strftime('%d/%m/%Y')}…",
        parse_mode=None,
    )
    try:
        n = await deliver_to_user(message.bot, session, user, target)
    except DouError as e:
        await message.answer(f"⚠️ {e}", parse_mode=None)
        return
    except Exception:
        logger.exception("mp_dou_agora failed")
        await message.answer("⚠️ Erro ao consultar o DOU.", parse_mode=None)
        return
    if n == 0:
        await message.answer(
            "Nenhuma MP nova publicada nessa data (ou já notificadas).",
            parse_mode=None,
        )
