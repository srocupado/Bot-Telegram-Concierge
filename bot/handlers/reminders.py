from __future__ import annotations

from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import User
from bot.services.reminders import (
    ReminderParseError,
    create_reminder,
    delete_reminder,
    format_pending_list,
    list_pending,
    parse_reminder,
)

router = Router(name=__name__)


@router.message(Command("lembrar"))
async def cmd_lembrar(
    message: Message, command: CommandObject, user: User, session: AsyncSession
) -> None:
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            "Uso: /lembrar <texto> + data/hora\n"
            "Exemplos:\n"
            "• /lembrar ligar pro João em 2h\n"
            "• /lembrar reunião amanhã 09:00\n"
            "• /lembrar pegar encomenda sexta 18h"
        )
        return

    try:
        clean_text, due_utc = parse_reminder(raw, user.timezone)
    except ReminderParseError as e:
        await message.answer(f"⚠️ {e}")
        return

    rem = await create_reminder(session, user.id, clean_text, due_utc)
    local = due_utc.astimezone(ZoneInfo(user.timezone))
    await message.answer(
        f"🔔 *{clean_text}*\n"
        f"   marcado para {local.strftime('%d/%m %H:%M')} _(#{rem.id})_",
        parse_mode="Markdown",
    )


@router.message(Command("lembretes"))
async def cmd_lembretes(message: Message, user: User, session: AsyncSession) -> None:
    items = await list_pending(session, user.id)
    await message.answer(
        format_pending_list(items, user.timezone), parse_mode=None,
    )


@router.message(Command("agendar_comando"))
async def cmd_agendar_comando(
    message: Message, command: CommandObject, user: User, session: AsyncSession
) -> None:
    """Sintaxe: /agendar_comando <tipo> [parametros] <quando NL>

    Tipos: transito_casa, transito_trabalho, congresso, clima, chat
    Ex:
      /agendar_comando transito_casa amanhã 15h
      /agendar_comando congresso segunda 9h
      /agendar_comando chat resumo das notícias da semana sexta 18h
    """
    from bot.config import settings
    from bot.services.reminders import ReminderParseError, parse_reminder
    from bot.services.scheduled_actions import OWNER_KINDS, VALID_KINDS

    is_owner = bool(
        settings.owner_telegram_id
        and message.from_user
        and message.from_user.id == settings.owner_telegram_id
    )
    # Não-owner nem fica sabendo dos tipos restritos (agente/shell).
    visible_kinds = VALID_KINDS if is_owner else (VALID_KINDS - OWNER_KINDS)

    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            "Uso: /agendar_comando <tipo> [parametros] <quando NL>\n"
            f"Tipos: {', '.join(sorted(visible_kinds))}\n"
            "Ex: /agendar_comando transito_casa amanhã 15h\n"
            "Ex: /agendar_comando chat resumo da semana sexta 18h\n"
            "Recorrente (todo dia/semana/cron) → peça em linguagem natural "
            "no chat (ex: 'todo dia útil 7h me manda o trânsito')."
        )
        return

    parts = raw.split(None, 1)
    tipo = parts[0]
    if tipo not in visible_kinds:
        await message.answer(f"⚠️ Tipo inválido. Use um de: {', '.join(sorted(visible_kinds))}")
        return
    resto = parts[1] if len(parts) > 1 else ""
    if not resto:
        await message.answer("⚠️ Faltou data/hora.")
        return

    try:
        # parse_reminder espera 'texto + tempo'. Aqui o 'texto' é o restante
        # (que pode ser vazio pra transito/congresso). Inserimos placeholder
        # se necessário.
        target = resto if any(c.isalpha() for c in resto.split()[0]) else f"x {resto}"
        clean_text, due_utc = parse_reminder(target, user.timezone)
        # clean_text vira o parametros (se houver), ou vazio.
        parametros = "" if clean_text == "x" else clean_text
    except ReminderParseError as e:
        await message.answer(f"⚠️ {e}")
        return

    if tipo in ("chat", "agente", "shell") and not parametros:
        await message.answer(
            f"⚠️ Para tipo='{tipo}', forneça o conteúdo antes da data/hora."
        )
        return

    _trunc = (parametros[:60] + ("…" if len(parametros) > 60 else "")) if parametros else ""
    descricao_map = {
        "transito_casa": "trânsito → casa",
        "transito_trabalho": "trânsito → trabalho",
        "congresso": "pauta do congresso",
        "clima": "clima",
        "chat": _trunc or "chat",
        "agente": f"🤖 {_trunc}",
        "shell": f"$ {_trunc}",
    }
    texto = f"[agendado] {descricao_map[tipo]}"
    from bot.services.reminders import create_reminder

    rem = await create_reminder(
        session, user.id, texto, due_utc,
        command_kind=tipo, command_args=parametros or None,
    )
    local = due_utc.astimezone(ZoneInfo(user.timezone))
    await message.answer(
        f"⏰ Agendado #{rem.id}: {descricao_map[tipo]} em "
        f"{local.strftime('%d/%m %H:%M')}",
    )


@router.message(Command("apagar_lembrete"))
async def cmd_apagar_lembrete(
    message: Message, command: CommandObject, user: User, session: AsyncSession
) -> None:
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            "Uso: /apagar_lembrete <id>\n"
            "Veja os ids com /lembretes."
        )
        return
    try:
        rid = int(raw.lstrip("#"))
    except ValueError:
        await message.answer("⚠️ id inválido. Use um número (veja /lembretes).")
        return

    rem = await delete_reminder(session, user.id, rid)
    if rem is None:
        await message.answer(f"🤷 Lembrete #{rid} não encontrado (ou já enviado).")
        return
    await message.answer(f"🗑️ Lembrete #{rid} apagado: _{rem.text}_", parse_mode="Markdown")
