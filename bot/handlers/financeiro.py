"""Setup da integração com gerenciador-financeiro (Firestore).

`/financeiro_setup` orquestra o fluxo:
  1. sem args → mostra estado + instrui próximo passo
  2. próximo doc JSON enviado pelo usuário (com flag awaiting ativa) →
     valida e salva o service account no kv_settings
  3. `/financeiro_setup uid <uid>` → grava o firebase_uid no user
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User
from bot.services.financeiro import (
    FinanceiroError,
    describe_credential,
    get_service_account_json,
    save_service_account_json,
)

logger = logging.getLogger(__name__)
router = Router(name="financeiro")

AWAITING_WINDOW = timedelta(minutes=5)
MAX_JSON_BYTES = 32 * 1024  # service accounts são ~2 KB; 32 é largo demais já


@router.message(Command("financeiro_setup"))
async def cmd_setup(
    message: Message, command: CommandObject, user: User, session: AsyncSession,
) -> None:
    if not user.is_authorized:
        return

    args = (command.args or "").strip()
    parts = args.split(maxsplit=1)

    # Sub-comando: /financeiro_setup uid <uid>
    if len(parts) >= 1 and parts[0].lower() == "uid":
        if len(parts) < 2:
            await message.answer(
                "Uso: <code>/financeiro_setup uid &lt;seu_firebase_uid&gt;</code>\n\n"
                "Pra descobrir: abra o gerenciador no navegador, DevTools → Console → "
                "<code>firebase.auth().currentUser.uid</code>",
                parse_mode="HTML",
            )
            return
        uid = parts[1].strip()
        if len(uid) < 6 or len(uid) > 64 or not all(c.isalnum() or c in "-_" for c in uid):
            await message.answer(
                "UID parece inválido (esperado alfanumérico, 6-64 chars).", parse_mode=None,
            )
            return
        user.firebase_uid = uid
        await session.commit()
        await message.answer(
            f"✅ UID salvo: <code>{uid}</code>\n\n"
            "Pronto pra usar: tente <i>'lança 250 no cartão, mercado, hoje'</i>.",
            parse_mode="HTML",
        )
        return

    # /financeiro_setup (sem args) — mostra estado + instrui
    sa_json = await get_service_account_json(session)
    has_sa = sa_json is not None
    has_uid = bool(user.firebase_uid)

    if has_sa and has_uid:
        await message.answer(
            f"✅ Tudo configurado.\n\n"
            f"• Service account: <code>{describe_credential(sa_json)}</code>\n"
            f"• Seu UID: <code>{user.firebase_uid}</code>\n\n"
            "Pra trocar a service account: envie um novo JSON agora.\n"
            "Pra trocar o UID: <code>/financeiro_setup uid &lt;novo&gt;</code>",
            parse_mode="HTML",
        )
    elif has_sa and not has_uid:
        await message.answer(
            f"⚠️ Service account OK ({describe_credential(sa_json)}), "
            f"mas falta seu UID do Firebase.\n\n"
            "No gerenciador (browser): DevTools → Console → "
            "<code>firebase.auth().currentUser.uid</code>\n"
            "Depois: <code>/financeiro_setup uid &lt;uid&gt;</code>",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "🔧 <b>Setup do gerenciador-financeiro</b>\n\n"
            "1. Firebase Console → Project Settings → Service Accounts → "
            "<b>Generate new private key</b> → baixe o JSON.\n"
            "2. Envie o JSON pra mim como <b>documento</b> (anexar arquivo).\n"
            "3. Depois eu vou pedir seu UID do Firebase.\n\n"
            "Você tem 5 minutos pra enviar o JSON.",
            parse_mode="HTML",
        )

    # Ativa janela de espera pra capturar o próximo JSON enviado.
    user.awaiting_firebase_json_until = datetime.now(timezone.utc) + AWAITING_WINDOW
    await session.commit()


def _is_json_doc(message: Message) -> bool:
    doc = message.document
    if not doc:
        return False
    if doc.mime_type == "application/json":
        return True
    name = (doc.file_name or "").lower()
    return name.endswith(".json")


@router.message(F.document)
async def on_document(message: Message, user: User, session: AsyncSession) -> None:
    """Captura JSON enviado dentro da janela de awaiting. Se não for JSON
    ou janela expirou, ignora (deixa o handler de PDF/etc cuidar)."""
    if not user.is_authorized:
        return
    if not _is_json_doc(message):
        return

    awaiting = user.awaiting_firebase_json_until
    if awaiting is None:
        return
    # SQLite via aiosqlite devolve naive UTC; normaliza pra aware.
    if awaiting.tzinfo is None:
        awaiting = awaiting.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > awaiting:
        return  # janela expirou — ignora silenciosamente

    doc = message.document
    if (doc.file_size or 0) > MAX_JSON_BYTES:
        await message.answer(
            f"⚠️ JSON muito grande (máx {MAX_JSON_BYTES // 1024} KB). "
            "Service accounts do Firebase têm ~2 KB.",
            parse_mode=None,
        )
        return

    try:
        buf = io.BytesIO()
        await message.bot.download(doc.file_id, destination=buf)
        json_str = buf.getvalue().decode("utf-8")
    except UnicodeDecodeError:
        await message.answer("⚠️ Arquivo não é UTF-8 válido.", parse_mode=None)
        return
    except Exception:
        logger.exception("json download failed")
        await message.answer("⚠️ Falha ao baixar o JSON.", parse_mode=None)
        return

    try:
        await save_service_account_json(session, json_str)
    except FinanceiroError as e:
        await message.answer(f"❌ {e}", parse_mode=None)
        return

    # Limpa janela de espera.
    user.awaiting_firebase_json_until = None
    await session.commit()

    summary = describe_credential(json_str)
    if user.firebase_uid:
        await message.answer(
            f"✅ Service account salva: <code>{summary}</code>\n\n"
            f"Seu UID já está configurado (<code>{user.firebase_uid}</code>). "
            "Pode lançar movimentações.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"✅ Service account salva: <code>{summary}</code>\n\n"
            "Agora preciso do seu UID do Firebase. No gerenciador (browser): "
            "DevTools → Console → <code>firebase.auth().currentUser.uid</code>\n"
            "Depois envie: <code>/financeiro_setup uid &lt;uid&gt;</code>",
            parse_mode="HTML",
        )
