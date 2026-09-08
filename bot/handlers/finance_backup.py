"""Backup e restore do gerenciador-financeiro por comando.

  /financeiro_backup            → gera agora e manda o JSON no chat
  /financeiro_backups           → lista as cópias locais
  /financeiro_restaurar [nome]  → prévia + botão de confirmação

O restore SOBRESCREVE o financeiro inteiro, então tem três travas:

  1. só o dono (a service account é global — ver _is_owner em financeiro.py);
  2. prévia com a contagem por seção ANTES de confirmar, nunca às cegas;
  3. foto do estado atual gravada antes de escrever (`-pre-restore-`), pra
     desfazer um restore errado restaurando a foto.

Fora do LLM de propósito: nenhuma tool chama restore. Sobrescrever o
financeiro por interpretação de frase solta não é risco que valha a
comodidade.
"""
from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User
from bot.services.finance_backup import (
    BackupError,
    extract_state,
    list_backups,
    resolve_backup,
    restore_state,
    resumo_state,
    run_backup,
    snapshot_pre_restore,
    write_backup,
)
from bot.services.financeiro import NotConfiguredError
from bot.utils import as_utc

logger = logging.getLogger(__name__)
router = Router(name="finance_backup")

RESTORE_WINDOW = timedelta(minutes=10)
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_TELEGRAM_BYTES = 45 * 1024 * 1024


def _is_owner(user: User) -> bool:
    return not settings.owner_telegram_id or user.id == settings.owner_telegram_id


def _fmt_bytes(n: int) -> str:
    return f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def _confirm_keyboard(nome: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="♻️ Restaurar", callback_data=f"finrest:{nome}"),
        InlineKeyboardButton(text="Cancelar", callback_data="finrest:n"),
    ]])


# ─────────────────────────────── backup ───────────────────────────────

@router.message(Command("financeiro_backup"))
async def cmd_backup(message: Message, user: User, session: AsyncSession) -> None:
    if not user.is_authorized or not _is_owner(user):
        return
    aviso = await message.answer("⏳ Lendo o Firestore…", parse_mode=None)
    try:
        arquivo, payload = await run_backup(session)
    except NotConfiguredError as e:
        await aviso.edit_text(f"⚠️ {e}", parse_mode=None)
        return
    except Exception:
        logger.exception("backup manual do financeiro falhou")
        await aviso.edit_text(
            "❌ Não consegui gerar o backup (falha ao ler o Firestore). "
            "Nada foi alterado. O backup do GitHub é independente — "
            "confira em Actions.",
            parse_mode=None,
        )
        return

    tamanho = arquivo.stat().st_size
    await aviso.edit_text(
        f"✅ Backup gerado: <code>{arquivo.name}</code>\n"
        f"{payload.get('count', 0)} usuário(s) · {_fmt_bytes(tamanho)}\n\n"
        f"Guardado em <code>{arquivo.parent}</code> "
        f"(retenção {settings.finance_backup_retention_days} dias).",
        parse_mode="HTML",
    )
    if tamanho <= MAX_TELEGRAM_BYTES:
        try:
            await message.answer_document(FSInputFile(arquivo, filename=arquivo.name))
        except Exception:
            logger.exception("envio do backup falhou")
            await message.answer(
                "⚠️ O arquivo foi gravado, mas não consegui enviá-lo aqui.",
                parse_mode=None,
            )


@router.message(Command("financeiro_backups"))
async def cmd_listar(message: Message, user: User) -> None:
    if not user.is_authorized or not _is_owner(user):
        return
    backups = list_backups()
    if not backups:
        await message.answer(
            "📭 Nenhum backup local ainda.\n\n"
            f"O automático roda às {settings.finance_backup_hour}h; "
            "pra gerar agora: <code>/financeiro_backup</code>",
            parse_mode="HTML",
        )
        return

    linhas = [f"💾 <b>Backups locais</b> ({len(backups)})\n"]
    for b in backups[:20]:
        marca = " ⏪ <i>pré-restore</i>" if b.pre_restore else ""
        linhas.append(f"• <code>{b.nome}</code> — {_fmt_bytes(b.size)}{marca}")
    if len(backups) > 20:
        linhas.append(f"\n<i>… e mais {len(backups) - 20}.</i>")
    linhas.append(
        f"\nRetenção: {settings.finance_backup_retention_days} dias. "
        "Restaurar: <code>/financeiro_restaurar &lt;nome&gt;</code>"
    )
    await message.answer("\n".join(linhas), parse_mode="HTML")


# ─────────────────────────────── restore ───────────────────────────────

async def _preview(message: Message, user: User, nome: str, payload: object) -> None:
    """Mostra o que será escrito e pede confirmação."""
    try:
        state = extract_state(payload, user.firebase_uid or "")
    except BackupError as e:
        await message.answer(f"❌ {e}", parse_mode=None)
        return
    await message.answer(
        "♻️ <b>Restaurar o financeiro</b>\n\n"
        f"Arquivo: <code>{nome}</code>\n"
        f"Conteúdo: {resumo_state(state)}\n\n"
        "⚠️ Isto <b>sobrescreve</b> o financeiro atual no Firestore — o app "
        "vai refletir a mudança em todos os dispositivos. Antes de escrever "
        "eu gravo uma foto do estado de agora, então dá pra voltar atrás.",
        parse_mode="HTML",
        reply_markup=_confirm_keyboard(nome),
    )


@router.message(Command("financeiro_restaurar"))
async def cmd_restaurar(
    message: Message, command: CommandObject, user: User, session: AsyncSession,
) -> None:
    if not user.is_authorized:
        return
    if not _is_owner(user):
        await message.answer(
            "O financeiro é do dono do bot — só ele restaura.", parse_mode=None,
        )
        return
    if not user.firebase_uid:
        await message.answer(
            "Falta seu UID do Firebase. Configure com "
            "<code>/financeiro_setup uid &lt;uid&gt;</code>",
            parse_mode="HTML",
        )
        return

    nome = (command.args or "").strip()
    if not nome:
        backups = list_backups()
        recentes = "\n".join(f"• <code>{b.nome}</code>" for b in backups[:5])
        # Fecha a janela da service account (ver financeiro.py): duas janelas
        # abertas fariam a ordem dos routers decidir, em silêncio, se o JSON
        # enviado vira credencial ou sobrescreve o financeiro.
        user.awaiting_finance_restore_until = (
            datetime.now(timezone.utc) + RESTORE_WINDOW
        )
        user.awaiting_firebase_json_until = None
        await session.commit()
        await message.answer(
            "♻️ <b>Restaurar o financeiro</b>\n\n"
            "Duas formas:\n"
            "1. <code>/financeiro_restaurar &lt;nome do arquivo&gt;</code> — "
            "de um backup local.\n"
            "2. <b>Envie o JSON agora</b> como documento — serve o artifact do "
            "GitHub (<code>gerenciador-backup.json</code>) ou o "
            "\"Exportar JSON\" do app. Janela de 10 minutos.\n\n"
            + (f"Backups locais mais recentes:\n{recentes}" if recentes
               else "<i>Nenhum backup local ainda.</i>"),
            parse_mode="HTML",
        )
        return

    caminho = resolve_backup(nome)
    if caminho is None:
        await message.answer(
            f"❌ Não achei <code>{nome}</code> nos backups locais. "
            "Veja a lista com <code>/financeiro_backups</code>.",
            parse_mode="HTML",
        )
        return
    try:
        payload = json.loads(caminho.read_text(encoding="utf-8"))
    except Exception:
        await message.answer(
            f"❌ <code>{nome}</code> não é JSON válido — arquivo corrompido. "
            "Não vou restaurar a partir dele.",
            parse_mode="HTML",
        )
        return
    await _preview(message, user, caminho.name, payload)


@router.message(F.document)
async def on_restore_document(
    message: Message, user: User, session: AsyncSession,
) -> None:
    """Captura o JSON enviado dentro da janela de restore. Fora dela,
    SkipHandler — `return` puro CONSOME o update no aiogram 3 e os handlers
    seguintes (service account, uploads, PDF) nunca rodariam."""
    if not user.is_authorized or not _is_owner(user):
        raise SkipHandler
    doc = message.document
    if not doc:
        raise SkipHandler
    nome_orig = (doc.file_name or "").lower()
    if doc.mime_type != "application/json" and not nome_orig.endswith(".json"):
        raise SkipHandler

    janela = as_utc(user.awaiting_finance_restore_until)
    if janela is None or datetime.now(timezone.utc) > janela:
        raise SkipHandler

    if (doc.file_size or 0) > MAX_UPLOAD_BYTES:
        await message.answer(
            f"⚠️ JSON muito grande (máx {MAX_UPLOAD_BYTES // 1024 // 1024} MB).",
            parse_mode=None,
        )
        return

    try:
        buf = io.BytesIO()
        await message.bot.download(doc.file_id, destination=buf)
        payload = json.loads(buf.getvalue().decode("utf-8"))
    except UnicodeDecodeError:
        await message.answer("⚠️ Arquivo não é UTF-8 válido.", parse_mode=None)
        return
    except json.JSONDecodeError as e:
        await message.answer(f"⚠️ JSON inválido: {e}", parse_mode=None)
        return
    except Exception:
        logger.exception("download do JSON de restore falhou")
        await message.answer("⚠️ Falha ao baixar o arquivo.", parse_mode=None)
        return

    # Valida ANTES de gravar: arquivo que não dá pra interpretar não vira
    # backup local (encheria a pasta de lixo restaurável).
    try:
        extract_state(payload, user.firebase_uid or "")
    except BackupError as e:
        await message.answer(f"❌ {e}", parse_mode=None)
        return

    user.awaiting_finance_restore_until = None
    await session.commit()

    agora = datetime.now(timezone.utc)
    caminho = write_backup(
        payload, hoje=agora.date(), sufixo=f"-upload-{agora.strftime('%H%M%S')}"
    )
    await _preview(message, user, caminho.name, payload)


@router.callback_query(F.data == "finrest:n")
async def cb_cancelar(query: CallbackQuery) -> None:
    await query.answer("Cancelado. Nada foi alterado.")
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("finrest:"))
async def cb_confirmar(
    query: CallbackQuery, user: User, session: AsyncSession,
) -> None:
    if not user.is_authorized or not _is_owner(user):
        await query.answer()
        return
    nome = (query.data or "").split(":", 1)[1]
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    caminho = resolve_backup(nome)
    if caminho is None:
        await query.answer("Arquivo sumiu.")
        await query.message.answer(
            f"❌ <code>{nome}</code> não está mais nos backups locais "
            "(pode ter sido podado). Nada foi alterado.",
            parse_mode="HTML",
        )
        return

    await query.answer("Restaurando…")
    try:
        payload = json.loads(caminho.read_text(encoding="utf-8"))
        state = extract_state(payload, user.firebase_uid or "")
    except BackupError as e:
        await query.message.answer(f"❌ {e}", parse_mode=None)
        return
    except Exception:
        logger.exception("leitura do backup falhou no confirm")
        await query.message.answer(
            "❌ Não consegui ler o arquivo. Nada foi alterado.", parse_mode=None,
        )
        return

    # Foto do estado atual ANTES de sobrescrever. Sem ela um restore errado
    # é irreversível — e é justamente quando alguém restaura que o estado
    # atual ainda não está em backup nenhum.
    foto = await snapshot_pre_restore(session)
    if foto is None:
        await query.message.answer(
            "❌ Não consegui gravar a foto do estado atual (Firestore fora?). "
            "<b>Não restaurei</b> — sem rede de segurança, um restore errado "
            "não teria volta. Tente de novo em instantes.",
            parse_mode="HTML",
        )
        return

    try:
        await restore_state(session, user, state)
    except Exception:
        logger.exception("restore do financeiro falhou")
        await query.message.answer(
            "❌ A escrita no Firestore falhou. O financeiro pode ter ficado "
            f"pela metade — confira no app. Foto do estado anterior: "
            f"<code>{foto.name}</code>",
            parse_mode="HTML",
        )
        return

    await query.message.answer(
        f"✅ <b>Financeiro restaurado</b> de <code>{nome}</code>\n"
        f"{resumo_state(state)}\n\n"
        f"Estado anterior guardado em <code>{foto.name}</code> — pra desfazer:\n"
        f"<code>/financeiro_restaurar {foto.name}</code>\n\n"
        "Abra o app pra confirmar (o sync em tempo real já deve ter puxado).",
        parse_mode="HTML",
    )
