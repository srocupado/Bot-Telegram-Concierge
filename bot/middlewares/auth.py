from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User

logger = logging.getLogger(__name__)

# Comandos permitidos antes de autenticar. /help SAIU: ele expõe o guia
# completo de features (rotina, cidade, integrações) pra qualquer estranho que
# encontre o bot; quem ainda não passou a senha recebe o convite do /start.
PUBLIC_COMMANDS = {"/start"}

# Anti-força-bruta na senha: a senha é única e vale pra sempre, e o middleware
# aceitava tentativas ILIMITADAS em texto plano. Contagem por usuário do
# Telegram, em memória (reinício zera — o objetivo é frear a rajada, não
# manter cadastro de bloqueio).
_MAX_TENTATIVAS = 5
_JANELA_BLOQUEIO = timedelta(minutes=15)
_tentativas: dict[int, tuple[int, datetime]] = {}


def _bloqueado_ate(user_id: int) -> datetime | None:
    """Instante em que o bloqueio expira, ou None se pode tentar."""
    dados = _tentativas.get(user_id)
    if not dados:
        return None
    n, ultima = dados
    if n < _MAX_TENTATIVAS:
        return None
    fim = ultima + _JANELA_BLOQUEIO
    if datetime.now(timezone.utc) >= fim:
        _tentativas.pop(user_id, None)
        return None
    return fim


def _registrar_tentativa(user_id: int) -> None:
    n, _ = _tentativas.get(user_id, (0, None))
    _tentativas[user_id] = (n + 1, datetime.now(timezone.utc))


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
        if not isinstance(event, (Message, CallbackQuery)) or event.from_user is None:
            return await handler(event, data)

        session: AsyncSession | None = data.get("session")
        if session is None:
            return await handler(event, data)

        tg_user = event.from_user
        chat_id = (
            event.chat.id if isinstance(event, Message)
            else (event.message.chat.id if event.message else tg_user.id)
        )
        user = await session.get(User, tg_user.id)
        if user is None:
            user = User(
                id=tg_user.id,
                chat_id=chat_id,
                username=tg_user.username,
                first_name=tg_user.first_name,
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
            if user.chat_id != chat_id:
                user.chat_id = chat_id
                changed = True
            if user.username != tg_user.username:
                user.username = tg_user.username
                changed = True
            if changed:
                await session.commit()

        data["user"] = user

        # CallbackQuery não tem texto/comando — só bloqueia se não autorizado.
        if isinstance(event, CallbackQuery):
            if not user.is_authorized:
                await event.answer("🔒 Acesso restrito.", show_alert=True)
                return None
            return await handler(event, data)

        text = (event.text or "").strip()
        cmd = text.split()[0].lower() if text else ""
        if not user.is_authorized and cmd not in PUBLIC_COMMANDS:
            # Tratar texto digitado como tentativa de senha quando ainda não autorizado.
            if text and not text.startswith("/"):
                ate = _bloqueado_ate(tg_user.id)
                if ate is not None:
                    faltam = max(1, int((ate - datetime.now(timezone.utc)).total_seconds() // 60))
                    logger.warning(
                        "senha: tentativas bloqueadas para user_id=%s", tg_user.id,
                    )
                    await event.answer(
                        f"🔒 Muitas tentativas. Tente de novo em {faltam} min."
                    )
                    return None
                if text == settings.access_password:
                    _tentativas.pop(tg_user.id, None)
                    user.is_authorized = True
                    await session.commit()
                    await event.answer("✅ Autorizado. Digite /help para ver os comandos.")
                else:
                    _registrar_tentativa(tg_user.id)
                    logger.info("senha incorreta de user_id=%s", tg_user.id)
                    await event.answer("Senha incorreta. Digite a senha de acesso ou /start.")
                return None
            await event.answer("🔒 Acesso restrito. Digite /start para iniciar.")
            return None

        return await handler(event, data)
