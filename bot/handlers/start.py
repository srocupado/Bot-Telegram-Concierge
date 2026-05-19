from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.db.models import User

router = Router(name=__name__)


HELP_TEXT = (
    "🤖 *Concierge*\n\n"
    "Comandos disponíveis:\n"
    "• /transito — força o briefing de trânsito + clima agora\n"
    "• /mp — força o resumo de Medidas Provisórias agora\n"
    "• /nova <texto> — cria uma tarefa\n"
    "• /tarefas — lista tarefas abertas\n"
    "• /feito <id> — marca tarefa como concluída\n"
    "• /lembrar <texto> em 2h | amanhã 09:00 — cria lembrete\n"
    "• /lembretes — lista lembretes pendentes\n"
    "• /ping — testa o LLM atual\n"
    "• /provider [anthropic|openai|gemini] — troca de LLM\n"
    "• /reset — limpa o contexto da conversa livre\n"
    "• /help — esta ajuda\n\n"
    "Mensagens de texto livre são enviadas ao LLM com contexto curto."
)


@router.message(Command("start"))
async def cmd_start(message: Message, user: User) -> None:
    if user.is_authorized:
        await message.answer("Olá de novo! Digite /help para ver os comandos.")
        return
    await message.answer(
        "Olá! 👋 Este bot é de uso restrito.\n\nDigite a senha de acesso para continuar."
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="Markdown")
