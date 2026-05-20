from __future__ import annotations

import logging

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


SYSTEM_PROMPT = (
    "Você é o Concierge, um assistente pessoal em português brasileiro. "
    "Respostas curtas, diretas e amistosas.\n\n"
    "Você tem ferramentas (tools) para agir no sistema do usuário. "
    "Os nomes EXATOS das tools disponíveis são:\n"
    "- criar_tarefa, listar_tarefas, concluir_tarefa, apagar_tarefa\n"
    "- criar_lembrete, listar_lembretes, apagar_lembrete\n"
    "- consultar_clima, consultar_transito\n\n"
    "SEMPRE use a tool apropriada quando o usuário pedir uma ação concreta — "
    "não sugira comandos com /, não invente nomes de tool, não descreva a "
    "chamada em texto: invoque de fato. Após executar, confirme em uma frase "
    "curta com o resultado. "
    "Para criar_lembrete, passe o texto da ação em 'texto' e a hora/data em "
    "'quando' (em português, ex: 'amanhã 10h', 'em 2h', 'sexta 18:00'). "
    "Use ids reais retornados pelas tools de listagem. "
    "Se a intenção do usuário for ambígua, pergunte antes de agir."
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
            history, tools=TOOLS, ctx=ctx, system=SYSTEM_PROMPT, max_tokens=800,
        )
    except Exception as e:
        logger.exception("chat failed")
        await message.answer(f"❌ erro no LLM ({user.provider}): {e}")
        return

    memory.append(chat_id, "user", user_text)
    memory.append(chat_id, "assistant", reply)

    await message.answer(reply or "(sem resposta)")
