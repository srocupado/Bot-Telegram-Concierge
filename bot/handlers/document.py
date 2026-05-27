"""Handler de documentos (PDFs) enviados ao bot.

Filtra por mime_type 'application/pdf', baixa, e roteia pro chat agente
como mensagem multimodal (igual ao photo handler).
"""
from __future__ import annotations

import io
import logging

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User
from bot.handlers.chat import _build_system_prompt
from bot.services.chat_memory import memory
from bot.services.llm.base import ToolContext, make_document_message
from bot.services.llm.factory import get_provider
from bot.services.tools import TOOLS

logger = logging.getLogger(__name__)

router = Router(name="document")

MAX_PDF_BYTES = 10 * 1024 * 1024  # 10 MB


@router.message(F.document & F.document.mime_type == "application/pdf")
async def cmd_pdf(message: Message, user: User, session: AsyncSession) -> None:
    if not user.is_authorized or not message.document:
        return

    doc = message.document
    if (doc.file_size or 0) > MAX_PDF_BYTES:
        await message.answer(
            f"⚠️ PDF muito grande (máx {MAX_PDF_BYTES // 1024 // 1024} MB).",
            parse_mode=None,
        )
        return

    try:
        buf = io.BytesIO()
        await message.bot.download(doc.file_id, destination=buf)
        pdf_bytes = buf.getvalue()
    except Exception:
        logger.exception("pdf download failed")
        await message.answer("⚠️ Não consegui baixar o PDF.", parse_mode=None)
        return

    caption = (message.caption or "").strip()
    chat_id = message.chat.id

    history = memory.get(chat_id)
    history.append(make_document_message(caption, pdf_bytes, mime_type="application/pdf"))

    # Mesma estratégia do photo: usa VISION_PROVIDER ou /provider atual.
    vision_provider_name = (
        user.vision_provider or settings.vision_provider or user.provider
    )
    try:
        provider = get_provider(vision_provider_name, gemini_model=user.gemini_model)
        ctx = ToolContext(user=user, session=session, tz=user.timezone)
        reply = await provider.chat_with_tools(
            history, tools=TOOLS, ctx=ctx,
            system=_build_system_prompt(user.timezone),
            max_tokens=4000,
        )
    except Exception as e:
        logger.exception("pdf chat failed")
        await message.answer(
            f"❌ erro no LLM de visão ({vision_provider_name}): {e}",
            parse_mode=None,
        )
        return

    file_label = doc.file_name or "PDF"
    memory.append(chat_id, "user", f"[PDF: {file_label}]{(' ' + caption) if caption else ''}")
    memory.append(chat_id, "assistant", reply)

    await message.answer(reply or "(sem resposta)", parse_mode=None)
