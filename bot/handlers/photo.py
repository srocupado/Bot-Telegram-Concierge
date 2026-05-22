"""Handler de mensagens com foto.

Fluxo: baixa a maior versão da foto, monta mensagem multimodal (texto da
caption + imagem) e roteia pro chat agente. Não persiste a imagem (só a
caption e a resposta do LLM ficam em chat_memory pra economizar RAM).
"""
from __future__ import annotations

import io
import logging

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User
from bot.handlers.chat import _build_system_prompt
from bot.config import settings
from bot.services.chat_memory import memory
from bot.services.llm.base import ToolContext, make_image_message
from bot.services.llm.factory import get_provider
from bot.services.tools import TOOLS

logger = logging.getLogger(__name__)

router = Router(name="photo")

# Limite defensivo de tamanho — providers geralmente aceitam até 5-10 MB,
# mas imagens grandes drenam tokens e RAM. Telegram serve várias resoluções;
# pegamos a maior que caiba aqui.
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB


@router.message(F.photo)
async def cmd_photo(message: Message, user: User, session: AsyncSession) -> None:
    if not user.is_authorized:
        return
    if not message.photo:
        return

    # message.photo é lista ordenada por tamanho crescente; pegamos a maior
    # que caiba no limite.
    chosen = None
    for size in reversed(message.photo):
        if (size.file_size or 0) <= MAX_IMAGE_BYTES:
            chosen = size
            break
    if chosen is None:
        await message.answer("⚠️ Imagem muito grande. Manda uma versão menor.")
        return

    try:
        buf = io.BytesIO()
        await message.bot.download(chosen.file_id, destination=buf)
        image_bytes = buf.getvalue()
    except Exception:
        logger.exception("photo download failed")
        await message.answer("⚠️ Não consegui baixar a imagem.")
        return

    caption = (message.caption or "").strip()
    chat_id = message.chat.id

    # Histórico só-texto (não persistimos a imagem em chat_memory).
    history = memory.get(chat_id)
    history.append(make_image_message(caption, image_bytes, mime_type="image/jpeg"))

    # Ordem: override por usuário (/provider_visao) → env VISION_PROVIDER → /provider atual.
    vision_provider_name = (
        user.vision_provider or settings.vision_provider or user.provider
    )
    try:
        provider = get_provider(vision_provider_name)
        ctx = ToolContext(user=user, session=session, tz=user.timezone)
        reply = await provider.chat_with_tools(
            history, tools=TOOLS, ctx=ctx,
            system=_build_system_prompt(user.timezone),
            max_tokens=4000,
        )
    except Exception as e:
        logger.exception("photo chat failed")
        await message.answer(
            f"❌ erro no LLM de visão ({vision_provider_name}): {e}",
            parse_mode=None,
        )
        return

    # Guarda só texto no memory pra próximo turno (sem reusar bytes da imagem).
    memory.append(chat_id, "user", f"[imagem]{(' ' + caption) if caption else ''}")
    memory.append(chat_id, "assistant", reply)

    await message.answer(reply or "(sem resposta)")
