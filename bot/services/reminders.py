from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import dateparser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Reminder

logger = logging.getLogger(__name__)


class ReminderParseError(Exception):
    pass


def parse_reminder(text: str, user_tz: str) -> tuple[str, datetime]:
    """Tenta separar 'texto' e 'quando' de uma string como:
        'ligar pro João em 2h'
        'reunião amanhã 09:00'
        'comprar pão hoje 18h'
    Estratégia: começa cortando do fim e testando se forma uma data válida.
    Retorna (texto_limpo, due_at em UTC).
    """
    raw = (text or "").strip()
    if not raw:
        raise ReminderParseError("texto vazio")

    tz = ZoneInfo(user_tz)
    now_local = datetime.now(tz)
    settings = {
        "TIMEZONE": user_tz,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "RELATIVE_BASE": now_local,
        "DATE_ORDER": "DMY",
    }

    words = raw.split()
    # Tenta o sufixo de tamanho crescente (até 6 palavras) como expressão temporal.
    best_when: datetime | None = None
    best_split = -1
    for take in range(1, min(7, len(words)) + 1):
        candidate = " ".join(words[-take:])
        # ignora caudas que claramente não são tempo
        if not any(ch.isdigit() or ch.isalpha() for ch in candidate):
            continue
        parsed = dateparser.parse(candidate, languages=["pt"], settings=settings)
        if parsed and parsed > now_local:
            best_when = parsed
            best_split = len(words) - take

    if best_when is None or best_split <= 0:
        # Tenta a string inteira como fallback (caso o texto seja só uma data).
        parsed = dateparser.parse(raw, languages=["pt"], settings=settings)
        if parsed and parsed > now_local:
            raise ReminderParseError("informe um texto antes da data/hora")
        raise ReminderParseError(
            "não entendi a data/hora. Exemplos: 'em 2h', 'amanhã 09:00', 'sexta 18h'"
        )

    clean_text = " ".join(words[:best_split]).strip(" -—:")
    if not clean_text:
        raise ReminderParseError("informe um texto antes da data/hora")

    due_utc = best_when.astimezone(timezone.utc)
    return clean_text, due_utc


async def create_reminder(
    session: AsyncSession,
    user_id: int,
    text: str,
    due_utc: datetime,
    *,
    command_kind: str | None = None,
    command_args: str | None = None,
) -> Reminder:
    rem = Reminder(
        user_id=user_id,
        text=text,
        due_at=due_utc,
        sent=False,
        command_kind=command_kind,
        command_args=command_args,
    )
    session.add(rem)
    await session.commit()
    await session.refresh(rem)
    return rem


async def list_pending(session: AsyncSession, user_id: int) -> list[Reminder]:
    result = await session.execute(
        select(Reminder)
        .where(Reminder.user_id == user_id, Reminder.sent.is_(False))
        .order_by(Reminder.due_at)
    )
    return list(result.scalars().all())


async def due_reminders(session: AsyncSession, user_id: int, now_utc: datetime) -> list[Reminder]:
    result = await session.execute(
        select(Reminder)
        .where(
            Reminder.user_id == user_id,
            Reminder.sent.is_(False),
            Reminder.due_at <= now_utc,
        )
        .order_by(Reminder.due_at)
    )
    return list(result.scalars().all())


async def mark_sent(session: AsyncSession, rem: Reminder) -> None:
    rem.sent = True
    rem.sent_at = datetime.now(timezone.utc)
    await session.commit()


async def delete_reminder(session: AsyncSession, user_id: int, reminder_id: int) -> Reminder | None:
    result = await session.execute(
        select(Reminder).where(
            Reminder.id == reminder_id,
            Reminder.user_id == user_id,
            Reminder.sent.is_(False),
        )
    )
    rem = result.scalar_one_or_none()
    if rem is None:
        return None
    await session.delete(rem)
    await session.commit()
    return rem
