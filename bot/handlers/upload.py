"""Anexos de documento → workspace/uploads + /arquivos (owner-only).

Ordem no dispatcher: DEPOIS do financeiro (que captura o JSON do setup na
janela de awaiting) e ANTES do handler de PDF. Regras:
- caption "guarda"/"salva" (opcional: "como <nome>") força salvar QUALQUER
  documento, inclusive PDF;
- sem caption de salvar, PDF segue pro fluxo de análise multimodal
  (SkipHandler) e os demais tipos são salvos por padrão;
- não-owner: SkipHandler — pra eles o recurso não existe (PDF deles ainda
  cai na análise, comportamento antigo).
"""
from __future__ import annotations

import io
import logging
import re

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.config import settings
from bot.db.models import User
from bot.services.uploads import (
    MAX_UPLOAD_BYTES,
    delete_file,
    format_listing,
    human_size,
    sanitize_name,
    unique_path,
)

logger = logging.getLogger(__name__)

router = Router(name="upload")

_SAVE_RE = re.compile(
    r"^\s*(?:guarda|guardar|salva|salvar)\b(?:\s+como\s+(?P<nome>\S+))?",
    re.IGNORECASE,
)


def _is_owner(message: Message) -> bool:
    return bool(
        settings.owner_telegram_id
        and message.from_user
        and message.from_user.id == settings.owner_telegram_id
    )


@router.message(F.document)
async def on_document(message: Message, user: User) -> None:
    if not user.is_authorized or not _is_owner(message) or not message.document:
        raise SkipHandler
    doc = message.document
    save_match = _SAVE_RE.match(message.caption or "")
    is_pdf = doc.mime_type == "application/pdf" or (
        (doc.file_name or "").lower().endswith(".pdf")
    )
    if save_match is None and is_pdf:
        raise SkipHandler  # análise multimodal (handlers/document.py)

    if (doc.file_size or 0) > MAX_UPLOAD_BYTES:
        await message.answer(
            f"⚠️ Arquivo muito grande pra Bot API "
            f"(máx {MAX_UPLOAD_BYTES // 1024 // 1024} MB no download).",
            parse_mode=None,
        )
        return

    name = sanitize_name(
        (save_match.group("nome") if save_match else None)
        or doc.file_name
        or "arquivo"
    )
    try:
        buf = io.BytesIO()
        await message.bot.download(doc.file_id, destination=buf)
        path = unique_path(name)
        path.write_bytes(buf.getvalue())
    except Exception:
        logger.exception("upload: falha ao salvar %s", name)
        await message.answer("⚠️ Não consegui salvar o arquivo.", parse_mode=None)
        return
    await message.answer(
        f"📁 Salvo: uploads/{path.name} ({human_size(path.stat().st_size)})\n"
        "/arquivos lista · caption \"guarda como <nome>\" renomeia · no "
        f"/agente: \"pega o uploads/{path.name} e …\"",
        parse_mode=None,
    )


@router.message(Command("arquivos"))
async def cmd_arquivos(message: Message, command: CommandObject, user: User) -> None:
    if not user.is_authorized or not _is_owner(message):
        return  # não-owner nem fica sabendo que o comando existe
    args = (command.args or "").strip()
    if args.lower().startswith(("apagar ", "rm ")):
        name = args.split(None, 1)[1].strip()
        if delete_file(name):
            await message.answer(f"🗑 uploads/{name} apagado.", parse_mode=None)
        else:
            await message.answer(
                f"Não achei uploads/{name} (veja /arquivos).", parse_mode=None,
            )
        return
    await message.answer("📁 " + format_listing(), parse_mode=None)
