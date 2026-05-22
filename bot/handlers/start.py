from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.db.models import User

router = Router(name=__name__)


HELP_TEXT = (
    "🤖 <b>Concierge</b>\n\n"
    "<b>Como falar com o bot</b>\n"
    "• <b>Texto livre</b>: o LLM agente entende e aciona ferramentas "
    "(criar tarefa/lembrete, ver clima/trânsito, agendar, buscar na web). "
    "Ex: <i>\"me lembre de pagar boleto amanhã 10h\"</i>, "
    "<i>\"qual o trânsito pra casa\"</i>, <i>\"resumo das notícias hoje\"</i>.\n"
    "• <b>Voz</b>: áudio é transcrito (Gemini). Frases como "
    "<i>\"trânsito pra casa\"</i>, <i>\"rota pro trabalho\"</i>, "
    "<i>\"pauta de MP do congresso\"</i> viram comandos diretos. "
    "Demais transcrições vão pro chat agente.\n"
    "• <b>Foto</b>: o bot analisa via LLM (OCR, identificação, leitura "
    "de boleto/placa/print). Pode acionar tools com base no que viu.\n\n"
    "<b>Comandos rápidos</b>\n"
    "<u>Trânsito</u>\n"
    "• <code>/transito_agora casa</code> | <code>/transito_agora trabalho</code> — consulta agora\n"
    "• <code>/transito_on</code> / <code>/transito_off</code> — digest diário (seg-sex)\n"
    "• <code>/transito_at HH:MM</code> — muda horário do digest\n"
    "• <code>/transito_reset</code> — zera marca de envio de hoje\n"
    "• <code>/transito_alerta_on</code> / <code>/transito_alerta_off</code> — alerta se rota ≥30% acima do habitual\n"
    "<u>Medidas Provisórias</u>\n"
    "• <code>/congresso_agora</code> — resumo da semana\n"
    "• <code>/congresso_on</code> / <code>/congresso_off</code> — digest semanal (segunda)\n"
    "• <code>/congresso_at HH:MM</code> — muda horário\n"
    "• <code>/congresso_reset</code> — zera marca da semana\n"
    "<u>Rota</u>\n"
    "• <code>/rota casa</code> | <code>/rota trabalho</code> — atalhos\n"
    "• <code>/rota &lt;endereço&gt;</code> — geocoda e calcula a partir da sua localização\n"
    "<u>Tarefas e lembretes</u>\n"
    "• <code>/nova &lt;texto&gt;</code>, <code>/tarefas</code>, <code>/feito &lt;id&gt;</code>\n"
    "• <code>/lembrar &lt;texto&gt; em 2h | amanhã 09:00</code>\n"
    "• <code>/lembretes</code>, <code>/apagar_lembrete &lt;id&gt;</code>\n"
    "• <code>/agendar_comando &lt;tipo&gt; [args] &lt;quando&gt;</code> — agenda ação (transito/congresso/clima/chat)\n"
    "<u>LLM</u>\n"
    "• <code>/ping</code> — testa LLM atual\n"
    "• <code>/provider anthropic|openai|gemini</code> — troca LLM do chat\n"
    "• <code>/provider_visao anthropic|openai|gemini|auto</code> — LLM só pra fotos "
    "(auto = mesmo do chat). Não precisa chave nova — usa a do provider escolhido.\n"
    "• <code>/reset</code> — limpa contexto da conversa livre\n\n"
    "<b>Limites</b>: áudio até <code>VOICE_MAX_SECONDS</code> (default 120s); "
    "imagem até 5 MB. Em conversa casual sobre trânsito/MPs, transcrição "
    "literal cai no chat (não dispara comando)."
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
