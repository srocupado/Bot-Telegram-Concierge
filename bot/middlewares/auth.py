from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User

logger = logging.getLogger(__name__)

# Comandos permitidos antes de autenticar.
PUBLIC_COMMANDS = {"/start", "/help"}


class AuthMiddleware(BaseMiddleware):
    """Garante usuário criado e bloqueia mensagens de não autorizados.

    O fluxo de senha em si fica no handler /start; aqui só checamos
    is_authorized e short-circuit com uma mensagem padrão.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.from_user is None:
            return await handler(event, data)

        session: AsyncSession | None = data.get("session")
        if session is None:
            return await handler(event, data)

        tg_user = event.from_user
        user = await session.get(User, tg_user.id)
        if user is None:
            user = User(
                id=tg_user.id,
                chat_id=event.chat.id,
                username=tg_user.username,
                is_authorized=False,
                provider=settings.ai_provider,
                timezone=settings.timezone,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            logger.info("created user", extra={"user_id": tg_user.id, "username": tg_user.username})
        else:
            # Mantém chat_id e username atualizados.
            changed = False
            if user.chat_id != event.chat.id:
                user.chat_id = event.chat.id
                changed = True
            if user.username != tg_user.username:
                user.username = tg_user.username
                changed = True
            if changed:
                await session.commit()

        data["user"] = user

        text = (event.text or "").strip()
        cmd = text.split()[0].lower() if text else ""
        if not user.is_authorized and cmd not in PUBLIC_COMMANDS:
            # Tratar texto digitado como tentativa de senha quando ainda não autorizado.
            if text and not text.startswith("/"):
                if text == settings.access_password:
                    user.is_authorized = True
                    await session.commit()
                    await event.answer("✅ Autorizado. Digite /help para ver os comandos.")
                else:
                    await event.answer("Senha incorreta. Digite a senha de acesso ou /start.")
                return None
            await event.answer("🔒 Acesso restrito. Digite /start para iniciar.")
            return None

        return await handler(event, data)
