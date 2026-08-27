from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy import select

from bot.config import settings
from bot.db.models import User
from bot.db.session import SessionLocal, init_db
from bot.handlers import agent as agent_handler
from bot.handlers import chat as chat_handler
from bot.handlers import tooldyn as tooldyn_handler
from bot.handlers import congress as congress_handler
from bot.handlers import dou_mp as dou_mp_handler
from bot.handlers import translator as translator_handler
from bot.handlers import viagem as viagem_handler
from bot.handlers import proactive as proactive_handler
from bot.handlers import ping as ping_handler
from bot.handlers import provider as provider_handler
from bot.handlers import reminders as reminders_handler
from bot.handlers import reset as reset_handler
from bot.handlers import route as route_handler
from bot.handlers import search as search_handler
from bot.handlers import start as start_handler
from bot.handlers import tasks as tasks_handler
from bot.handlers import traffic as traffic_handler
from bot.handlers import document as document_handler
from bot.handlers import financeiro as financeiro_handler
from bot.handlers import photo as photo_handler
from bot.handlers import upload as upload_handler
from bot.handlers import reminder_callbacks as reminder_callbacks_handler
from bot.handlers import shopping as shopping_handler
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
    dp.include_router(dou_mp_handler.router)
    dp.include_router(translator_handler.router)  # /tradutor + /tradutor_provider
    dp.include_router(viagem_handler.router)  # /viagem (modo viagem)
    dp.include_router(proactive_handler.router)
    dp.include_router(tasks_handler.router)
    dp.include_router(reminders_handler.router)
    dp.include_router(route_handler.router)  # /rota + F.location + botão cancelar
    dp.include_router(search_handler.router)  # /buscar (Anthropic ou Gemini)
    dp.include_router(voice_handler.router)  # voz: transcreve + roteia
    dp.include_router(photo_handler.router)  # foto: multimodal → chat agente
    dp.include_router(financeiro_handler.router)  # /financeiro_setup + captura JSON (antes do PDF handler)
    dp.include_router(upload_handler.router)  # anexos → workspace/uploads + /arquivos (antes do PDF: caption "guarda" força salvar; sem caption, PDF passa)
    dp.include_router(document_handler.router)  # PDF: multimodal → chat agente
    dp.include_router(reminder_callbacks_handler.router)  # botões snooze/done
    dp.include_router(shopping_handler.router)  # botões confirmar limpeza da lista
    dp.include_router(agent_handler.router)  # /agente + continuação (antes do catch-all!)
    dp.include_router(tooldyn_handler.router)  # tools dinâmicas (/tool_nova etc.)
    dp.include_router(chat_handler.router)  # catch-all texto livre
    return dp


async def _notify_restart(bot: Bot) -> None:
    """Avisa usuários autorizados que o bot subiu (deploy/restart/crash recovery)."""
    if not settings.restart_notification_enabled:
        return
    now_local = datetime.now(ZoneInfo(settings.timezone))
    msg = f"🟢 _Concierge online_ — {now_local.strftime('%d/%m %H:%M')}"
    async with SessionLocal() as session:
        users = list((await session.scalars(
            select(User).where(User.is_authorized.is_(True))
        )).all())
    for u in users:
        try:
            await bot.send_message(u.id, msg, parse_mode="Markdown")
        except Exception:
            logger.exception("failed to send restart notification to %d", u.id)


async def main() -> None:
    setup_logging(settings.log_level)
    logger.info("starting concierge bot")

    # O executor default é compartilhado por: resolução de DNS do aiohttp
    # (getaddrinfo, usado em TODO send pro Telegram), chamadas de LLM
    # (asyncio.to_thread) e — antes do executor dedicado — Firestore. No
    # Orange Pi o pool default tem só ~8 threads (núcleos+4); com 2 usuários +
    # chamadas LLM/Firestore lentas em paralelo, ele saturava e o getaddrinfo
    # dos envios ficava na fila → "TelegramNetworkError: Request timeout"
    # (parecia rede, era falta de thread). Folga generosa resolve.
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=32, thread_name_prefix="default"
        )
    )

    await init_db()

    # Memória persistente: liga write-through/resumo e re-hidrata o contexto
    # de conversa que ainda está dentro do TTL (restart deixa de "esquecer").
    from bot.services import memoria
    from bot.services.chat_memory import memory
    memoria.attach(SessionLocal)
    await memoria.hydrate(SessionLocal, memory)

    # Sessão do bot FORÇADA a IPv4. Medido no Orange Pi (03/08/2026, de dentro
    # do container): connect v6 à api.telegram.org = ENETUNREACH; v4 = 0.2s.
    # O v6 morto é descartado rápido no caso "unreachable", mas é um caminho
    # quebrado tentado em conexão nova — e o aiogram cacheia o DNS (AAAA
    # incluso) por 1h (ttl_dns_cache=3600). Há modos de falha v6 piores que
    # o unreachable (blackhole que pendura até o timeout); com family=AF_INET
    # a classe inteira sai da equação. `_connector_init` é privado do
    # AiohttpSession — aceitável com a versão pinada (aiogram==3.20.0.post0);
    # o teste de regressão quebra se o atributo mudar de forma.
    import socket as _socket
    from aiogram.client.session.aiohttp import AiohttpSession
    bot_session = AiohttpSession()
    bot_session._connector_init["family"] = _socket.AF_INET
    # Keepalive LONGO (default do aiohttp: 15s). Medição de 03/08/2026 no
    # Orange Pi: o caminho até o DC do Telegram perde pacote de forma
    # intermitente e o custo cai TODO no handshake de conexão nova
    # (get_file_s medidos: 0.5→17.7s, na escada de retransmissão 1/3/7/15s),
    # enquanto conexões já abertas transferem em <1s. Reusar conexões por
    # mais tempo reduz drasticamente quantos handshakes se paga — burst de
    # áudios/comandos vira UM handshake. Trade-off: reuso de conexão que o
    # servidor fechou pode dar um ServerDisconnected ocasional (o retry das
    # camadas acima cobre).
    bot_session._connector_init["keepalive_timeout"] = 55.0
    bot = Bot(
        settings.bot_token,
        session=bot_session,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    dp = _build_dispatcher()

    # Agente de execução: bot global pro caminho da tool executar_agente +
    # aviso ao owner se o restart matou uma tarefa no meio.
    agent_handler.set_bot(bot)
    await agent_handler.notify_stale_task(bot)

    # Tools dinâmicas (criadas via /tool_nova, aprovadas pelo dono): carrega
    # do volume persistente. Arquivo quebrado NÃO derruba o boot — vira aviso
    # explícito ao dono (regra: falha nunca é silêncio).
    from bot.services import tools_dinamicas
    problemas_td = tools_dinamicas.carregar_todas()
    if problemas_td and settings.owner_telegram_id:
        try:
            await bot.send_message(
                settings.owner_telegram_id,
                "⚠️ Tool(s) dinâmica(s) NÃO carregada(s) no boot:\n- "
                + "\n- ".join(problemas_td)
                + "\nElas ficam FORA do catálogo até conserto (/tool_nova) "
                "ou remoção (/tool_rm).",
                parse_mode=None,
            )
        except Exception:
            logger.warning("aviso de tools dinâmicas quebradas não entregue",
                           exc_info=True)

    await _notify_restart(bot)

    scheduler_task = asyncio.create_task(scheduler_loop(bot, SessionLocal))
    # Mede o atraso do event loop: é o que distingue "o bot está esperando a
    # rede" de "o bot está TRAVADO" quando uma requisição longa parece segurar
    # todas as outras. Ver bot/services/loop_watchdog.py.
    from bot.services.loop_watchdog import watchdog_loop
    watchdog_task = asyncio.create_task(watchdog_loop())
    try:
        logger.info("starting polling")
        await dp.start_polling(bot, handle_signals=True)
    finally:
        # Drena os jobs em background (nota do DOU, agente) ANTES de derrubar
        # o loop: o fim do asyncio.run matava essas tasks sem aviso a cada
        # deploy. 15s cabem no stop_grace_period do compose (30s) com folga
        # pro fechamento do bot.session.
        from bot.services import jobs
        with contextlib.suppress(Exception):
            await jobs.drenar(15.0)
        for t in (scheduler_task, watchdog_task):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await bot.session.close()
