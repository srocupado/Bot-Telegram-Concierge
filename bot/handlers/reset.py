from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User
from bot.services.chat_memory import memory

router = Router(name=__name__)


@router.message(Command("reset"))
async def cmd_reset(message: Message, user: User, session: AsyncSession) -> None:
    from bot.services.memoria import reset_recent

    memory.reset(message.chat.id)
    # Apaga também do SQL o que re-hidrataria num restart — senão a conversa
    # "resetada" ressuscita. Histórico antigo (busca) e resumo ficam.
    await reset_recent(session, user.id)
    await message.answer("🧹 Contexto da conversa limpo.")


@router.message(Command("reset_memoria"))
async def cmd_reset_memoria(
    message: Message, command: CommandObject, user: User, session: AsyncSession
) -> None:
    """Zera a memória de longo prazo: o resumo rolante (e, com 'tudo',
    também o histórico de conversas pesquisável)."""
    from bot.services.memoria import clear_all_history, clear_summary

    arg = (command.args or "").strip().lower()
    if arg == "tudo":
        memory.reset(message.chat.id)
        n = await clear_all_history(session, user.id)
        await message.answer(
            f"🧠 Memória zerada: resumo + {n} mensagens do histórico apagados. "
            "(Fatos do /lembrar_fato não são afetados — use esquecer_fato.)",
            parse_mode=None,
        )
        return
    had = await clear_summary(session, user.id)
    await message.answer(
        "🧠 Resumo de longo prazo apagado — ele se reconstrói das próximas conversas.\n"
        "Use /reset_memoria tudo pra apagar também o histórico pesquisável."
        if had else
        "Não havia resumo de longo prazo salvo.\n"
        "Use /reset_memoria tudo pra apagar o histórico pesquisável.",
        parse_mode=None,
    )
