"""Callbacks da lista de compras.

A lista em si é manipulada via tools do LLM (adicionar/listar/marcar/remover).
Aqui fica só a confirmação por botões antes de ZERAR a lista toda — pra evitar
que o agente apague tudo sem o usuário pedir explicitamente.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User
from bot.services.shopping import clear_all

logger = logging.getLogger(__name__)
router = Router(name="shopping")


def clear_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑️ Sim, limpar tudo", callback_data="clearshop:y"),
        InlineKeyboardButton(text="Cancelar", callback_data="clearshop:n"),
    ]])


@router.callback_query(F.data == "clearshop:n")
async def cb_clear_nao(query: CallbackQuery) -> None:
    await query.answer("Ok, mantive a lista.")
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data == "clearshop:y")
async def cb_clear_sim(query: CallbackQuery, user: User, session: AsyncSession) -> None:
    if not user.is_authorized:
        await query.answer()
        return
    n = await clear_all(session, user.id)
    await query.answer("Lista limpa.")
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.answer(
        f"🛒 Lista zerada ({n} item(ns) removido(s)).", parse_mode=None,
    )
