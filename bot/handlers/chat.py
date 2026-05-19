from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message

from bot.db.models import User
from bot.services.chat_memory import memory
from bot.services.llm.factory import get_provider

router = Router(name=__name__)
logger = logging.getLogger(__name__)


SYSTEM_PROMPT = (
    "Você é o Concierge, um assistente pessoal em português brasileiro. "
    "Respostas curtas, diretas e amistosas. Quando o usuário pedir algo que parece "
    "uma tarefa ou lembrete, sugira o comando correto (/nova, /lembrar)."
)


@router.message(F.text & ~F.text.startswith("/"))
async def free_chat(message: Message, user: User) -> None:
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
        reply = await provider.chat(history, system=SYSTEM_PROMPT, max_tokens=600)
    except Exception as e:
        logger.exception("chat failed")
        await message.answer(f"❌ erro no LLM ({user.provider}): {e}")
        return

    memory.append(chat_id, "user", user_text)
    memory.append(chat_id, "assistant", reply)

    await message.answer(reply or "(sem resposta)")
