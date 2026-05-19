from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.db.models import User

router = Router(name=__name__)


HELP_TEXT = (
    "🤖 <b>Concierge</b>\n\n"
    "<b>Trânsito casa↔trabalho</b>:\n"
    "• <code>/transito_agora casa</code> | <code>/transito_agora trabalho</code> — força consulta agora\n"
    "• <code>/transito_on</code> / <code>/transito_off</code> — assina/desassina digest diário (seg-sex)\n"
    "• <code>/transito_at HH:MM</code> — muda horário do digest (sem arg volta ao default)\n"
    "• <code>/transito_reset</code> — zera marca de envio de hoje\n\n"
    "<b>Medidas Provisórias</b>:\n"
    "• <code>/congresso_agora</code> — força resumo da semana agora\n"
    "• <code>/congresso_on</code> / <code>/congresso_off</code> — assina/desassina digest semanal (segunda)\n"
    "• <code>/congresso_at HH:MM</code> — muda horário do digest\n"
    "• <code>/congresso_reset</code> — zera marca de envio da semana\n\n"
    "<b>Tarefas e lembretes</b>:\n"
    "• <code>/nova &lt;texto&gt;</code> — cria tarefa\n"
    "• <code>/tarefas</code> — lista tarefas abertas\n"
    "• <code>/feito &lt;id&gt;</code> — marca tarefa como concluída\n"
    "• <code>/lembrar &lt;texto&gt; em 2h | amanhã 09:00</code> — cria lembrete\n"
    "• <code>/lembretes</code> — lista lembretes pendentes\n\n"
    "<b>LLM</b>:\n"
    "• <code>/ping</code> — testa o LLM atual (mostra provider e modelo)\n"
    "• <code>/provider anthropic|openai|gemini</code> — troca de LLM\n"
    "• <code>/reset</code> — limpa o contexto da conversa livre\n\n"
    "<b>Voz</b>:\n"
    "• Mande um áudio — o bot transcreve via Gemini e executa o comando "
    "(se começar com /) ou responde no chat livre.\n\n"
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
    await message.answer(HELP_TEXT, parse_mode="HTML", disable_web_page_preview=True)
