from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import settings
from bot.db.session import SessionLocal, init_db
from bot.handlers import chat as chat_handler
from bot.handlers import congress as congress_handler
from bot.handlers import ping as ping_handler
from bot.handlers import provider as provider_handler
from bot.handlers import reminders as reminders_handler
from bot.handlers import reset as reset_handler
from bot.handlers import route as route_handler
from bot.handlers import start as start_handler
from bot.handlers import tasks as tasks_handler
from bot.handlers import traffic as traffic_handler
from bot.handlers import document as document_handler
from bot.handlers import photo as photo_handler
from bot.handlers import reminder_callbacks as reminder_callbacks_handler
from bot.handlers import voice as voice_handler
from bot.logging_setup import setup_logging
from bot.middlewares.auth import AuthMiddleware
from bot.middlewares.db import DBSessionMiddleware
from bot.services.scheduler import scheduler_loop

logger = logging.getLogger(__name__)


def _build_dispatcher() -> Dispatcher:
    dp = Dispatcher()

    # middlewares: DB primeiro (provê session), depois auth (consome session)
    dp.message.middleware(DBSessionMiddleware())
    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(DBSessionMiddleware())
    dp.callback_query.middleware(AuthMiddleware())

    # routers (ordem importa: comandos antes do catch-all de chat livre)
    dp.include_router(start_handler.router)
    dp.include_router(ping_handler.router)
    dp.include_router(provider_handler.router)
    dp.include_router(reset_handler.router)
    dp.include_router(traffic_handler.router)
    dp.include_router(congress_handler.router)
    dp.include_router(tasks_handler.router)
    dp.include_router(reminders_handler.router)
    dp.include_router(route_handler.router)  # /rota + F.location + botão cancelar
    dp.include_router(voice_handler.router)  # voz: transcreve + roteia
    dp.include_router(photo_handler.router)  # foto: multimodal → chat agente
    dp.include_router(document_handler.router)  # PDF: multimodal → chat agente
    dp.include_router(reminder_callbacks_handler.router)  # botões snooze/done
    dp.include_router(chat_handler.router)  # catch-all texto livre
    return dp


async def main() -> None:
    setup_logging(settings.log_level)
    logger.info("starting concierge bot")

    await init_db()

    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    dp = _build_dispatcher()

    scheduler_task = asyncio.create_task(scheduler_loop(bot, SessionLocal))
    try:
        logger.info("starting polling")
        await dp.start_polling(bot, handle_signals=True)
    finally:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
        await bot.session.close()
