from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User
from bot.services.chat_memory import memory
from bot.services.llm.base import ToolContext
from bot.services.llm.factory import get_provider
from bot.services.tools import TOOLS

router = Router(name=__name__)
logger = logging.getLogger(__name__)


_SYSTEM_PROMPT_TEMPLATE = (
    "Você é o Concierge, um assistente pessoal em português brasileiro. "
    "Respostas curtas, diretas e amistosas.\n\n"
    "Data/hora atual: {now_local} ({tz}).\n\n"
    "Você tem ferramentas (tools) para agir no sistema do usuário. "
    "Os nomes EXATOS das tools disponíveis são:\n"
    "- criar_tarefa, listar_tarefas, concluir_tarefa, apagar_tarefa\n"
    "- criar_lembrete, listar_lembretes, apagar_lembrete, agendar_comando\n"
    "- consultar_clima, consultar_transito\n\n"
    "Quando rodando no Anthropic ou Gemini, você tem busca web nativa "
    "(Anthropic web_search / Gemini google_search). Use SEMPRE que o usuário "
    "pedir notícias, eventos atuais, cotações, resultados ou informação que "
    "dependa de dados recentes. Cite fontes brevemente. (No OpenAI essa "
    "capacidade não está disponível; responda com o que sabe e avise se "
    "precisar de dado fresco.)\n\n"
    "Quando a mensagem contém imagem, analise-a (OCR, identificação, "
    "extração de dados). Se a imagem implicar ação concreta (ex: foto de "
    "boleto sugere criar_lembrete pra pagar; placa de endereço sugere "
    "agendar_comando trânsito), invoque a tool após extrair os dados.\n\n"
    "SEMPRE use a tool apropriada quando o usuário pedir uma ação concreta — "
    "não sugira comandos com /, não invente nomes de tool, não descreva a "
    "chamada em texto: invoque de fato. Após executar, confirme em uma frase "
    "curta com o resultado.\n\n"
    "Para criar_lembrete:\n"
    "- 'texto' é o conteúdo do lembrete (ex: 'comprar morangos').\n"
    "- 'quando_iso' é a data/hora ABSOLUTA no formato ISO local "
    "'YYYY-MM-DDTHH:MM', calculada a partir da Data/hora atual acima.\n"
    "- 'às 16h' / 'às 4 da tarde' / '16:00' = hora absoluta 16:00 do dia indicado.\n"
    "- 'em 2h' = relativo: some 2h à Data/hora atual.\n"
    "- Sempre confira mentalmente: 'às' vira hora absoluta, 'em' vira soma.\n"
    "Use ids reais retornados pelas tools de listagem.\n\n"
    "Para consultar_transito: aceita atalhos 'casa' e 'trabalho' como origem/destino "
    "(mapeiam pra HOME_COORDS/WORK_COORDS do servidor). Quando o usuário disser só "
    "'trânsito pra casa', interprete como origem='trabalho' e destino='casa'. "
    "Quando ele disser 'trânsito pro trabalho', interprete como origem='casa' "
    "e destino='trabalho'.\n\n"
    "Para agendar_comando (executar AÇÃO automática no futuro, não só lembrete texto):\n"
    "- Use quando o usuário quer DISPARAR um comando no horário marcado, ex: "
    "'lembre de mostrar o trânsito pra casa às 15h' → agendar_comando("
    "tipo='transito_casa', quando_iso=...).\n"
    "- Use criar_lembrete (não agendar_comando) quando ele quer só um aviso "
    "de texto ('me lembre de tomar remédio').\n\n"
    "Se a intenção do usuário for ambígua, pergunte antes de agir."
)


def _build_system_prompt(tz_name: str) -> str:
    now_local = datetime.now(ZoneInfo(tz_name))
    return _SYSTEM_PROMPT_TEMPLATE.format(
        now_local=now_local.strftime("%Y-%m-%d %H:%M %A"),
        tz=tz_name,
    )


@router.message(F.text & ~F.text.startswith("/"))
async def free_chat(message: Message, user: User, session: AsyncSession) -> None:
    if not user.is_authorized:
        return

    chat_id = message.chat.id
    user_text = message.text or ""
    if not user_text.strip():
        return

    history = memory.get(chat_id)
    history.append({"role": "user", "content": user_text})

    try:
        provider = get_provider(user.provider)
        ctx = ToolContext(user=user, session=session, tz=user.timezone)
        reply = await provider.chat_with_tools(
            history, tools=TOOLS, ctx=ctx,
            system=_build_system_prompt(user.timezone),
            max_tokens=800,
        )
    except Exception as e:
        logger.exception("chat failed")
        await message.answer(
            f"❌ erro no LLM ({user.provider}): {e}", parse_mode=None,
        )
        return

    memory.append(chat_id, "user", user_text)
    memory.append(chat_id, "assistant", reply)

    await message.answer(reply or "(sem resposta)")
