from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.db.models import User

router = Router(name=__name__)


HELP_TEXT = (
    "🤖 *Concierge*\n\n"
    "*Trânsito casa↔trabalho* (réplica do Telegram-Travels):\n"
    "• /trafego_now <casa|trabalho> — força consulta agora\n"
    "• /trafego_on / /trafego_off — assina/desassina digest diário (seg-sex)\n"
    "• /trafego_at HH:MM — muda horário do digest (sem arg volta ao default)\n"
    "• /trafego_reset — zera marca de envio de hoje\n\n"
    "*Medidas Provisórias* (réplica do Telegram-Travels):\n"
    "• /congresso_now — força resumo da semana agora\n"
    "• /congresso_on / /congresso_off — assina/desassina digest semanal (segunda)\n"
    "• /congresso_at HH:MM — muda horário do digest\n"
    "• /congresso_reset — zera marca de envio da semana\n\n"
    "*Tarefas e lembretes*:\n"
    "• /nova <texto> — cria tarefa\n"
    "• /tarefas — lista tarefas abertas\n"
    "• /feito <id> — marca tarefa como concluída\n"
    "• /lembrar <texto> em 2h | amanhã 09:00 — cria lembrete\n"
    "• /lembretes — lista lembretes pendentes\n\n"
    "*LLM*:\n"
    "• /ping — testa o LLM atual\n"
    "• /provider [anthropic|openai|gemini] — troca de LLM\n"
    "• /reset — limpa o contexto da conversa livre\n\n"
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
