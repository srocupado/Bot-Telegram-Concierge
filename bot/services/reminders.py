from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import dateparser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Reminder

logger = logging.getLogger(__name__)

# Re-export pra compatibilidade — implementação central em bot/utils.py.
from bot.utils import as_utc  # noqa: E402,F401


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
    recurrence: str | None = None,
) -> Reminder:
    rem = Reminder(
        user_id=user_id,
        text=text,
        due_at=due_utc,
        sent=False,
        command_kind=command_kind,
        command_args=command_args,
        recurrence=recurrence,
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


_DIAS_SEMANA = [
    "Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo",
]


def _dia_label(local: datetime, today) -> str:
    d = local.date()
    delta = (d - today).days
    if delta == 0:
        return "Hoje"
    if delta == 1:
        return "Amanhã"
    if delta == -1:
        return "Ontem"
    return f"{_DIAS_SEMANA[local.weekday()]} ({local.strftime('%d/%m')})"


def _hora_label(local: datetime) -> str:
    return local.strftime("%Hh") if local.minute == 0 else local.strftime("%H:%M")


# Emoji contextual por palavra-chave (ordem importa: específico antes de
# genérico — ex.: 'ipva'/'carro' antes de 'boleto').
_EMOJI_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("ipva", "carro", "veícul", "veicul", "bmw", "pneu", "combustív", "gasolina",
      "posto", "oficina", "licenciamento", "multa", "estaciona"), "🚗"),
    (("voo", "passagem", "viagem", "hotel", "embarque", "aeroporto"), "✈️"),
    (("médic", "medic", "remédio", "remedio", "consulta", "dentista", "exame",
      "vacina", "farmácia", "farmacia"), "💊"),
    (("aniversár", "niver", "parabéns", "parabens"), "🎂"),
    (("reunião", "reuniao", "meeting", "call", "compromisso"), "📅"),
    (("academia", "treino", "malhar", "musculação", "musculacao", "corrida"), "🏋️"),
    (("mercado", "supermercado", "compras", "feira"), "🛒"),
    (("luz", "energia", "elétric", "eletric"), "💡"),
    (("água", "agua", "saneamento"), "💧"),
    (("internet", "wifi", "telefone", "celular", "fatura do celular"), "📶"),
    (("aluguel", "condomínio", "condominio", "imóvel", "imovel", "casa"), "🏠"),
    (("banco", "empréstimo", "emprestimo", "financiamento", "cartão", "cartao"), "🏦"),
    (("escola", "prova", "aula", "faculdade", "trabalho de", "entrega"), "🎓"),
    (("boleto", "conta", "pagar", "pagamento", "pix", "fatura"), "🧾"),
]


def _emoji_for(r: Reminder) -> str:
    if r.recurrence:
        return "🔁"
    if r.command_kind:
        return "⏰"
    text = (r.text or "").lower()
    for keywords, emoji in _EMOJI_KEYWORDS:
        if any(k in text for k in keywords):
            return emoji
    return "📌"


def format_reminder_line(r: Reminder, tz_name: str) -> str:
    """Uma linha padronizada do lembrete: '{emoji} #id — Dia, hora → texto'."""
    tz = ZoneInfo(tz_name)
    local = as_utc(r.due_at).astimezone(tz)
    today = datetime.now(tz).date()
    rec = f" ({r.recurrence})" if r.recurrence else ""
    return (
        f"{_emoji_for(r)} #{r.id} — {_dia_label(local, today)}, "
        f"{_hora_label(local)} → {r.text}{rec}"
    )


def format_reminder_confirmation(r: Reminder, tz_name: str, *, verb: str = "criado") -> str:
    """Confirmação padronizada ao criar/agendar um lembrete, com o teor."""
    return f"✅ Lembrete {verb}:\n{format_reminder_line(r, tz_name)}"


def format_pending_list(items: list[Reminder], tz_name: str) -> str:
    """Formatação ÚNICA e padronizada da lista de lembretes (usada pelo
    comando /lembretes e pela tool listar_lembretes, pra a saída ficar igual
    em qualquer provider de LLM). Padrão: lista numerada + emoji contextual."""
    if not items:
        return "📭 Nenhum lembrete pendente."
    plural = "lembrete" if len(items) == 1 else "lembretes"
    suf = "s" if len(items) > 1 else ""
    lines = [f"Você tem {len(items)} {plural} pendente{suf}:\n"]
    for i, r in enumerate(items, 1):
        lines.append(f"{i}. {format_reminder_line(r, tz_name)}")
    return "\n".join(lines)


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


_WEEKDAY_MAP = {
    "mon": 0, "seg": 0,
    "tue": 1, "ter": 1,
    "wed": 2, "qua": 2,
    "thu": 3, "qui": 3,
    "fri": 4, "sex": 4,
    "sat": 5, "sab": 5, "sáb": 5,
    "sun": 6, "dom": 6,
}

VALID_RECURRENCES = {"daily", "weekday", "weekend", "monthly"}  # + "weekly:<dias>" + "cron:<expr>"

# Frequência mínima de uma expressão cron (proteção de custo/spam — um
# '* * * * *' dispararia a cada tick do scheduler).
CRON_MIN_INTERVAL_MINUTES = 10


def cron_expr(rrule: str | None) -> str | None:
    """Extrai a expressão de um rrule 'cron:<expr>' (None se não for cron)."""
    if rrule and rrule.startswith("cron:"):
        return rrule.split(":", 1)[1].strip()
    return None


def is_valid_recurrence(rrule: str) -> bool:
    if rrule in VALID_RECURRENCES:
        return True
    if rrule.startswith("weekly:"):
        days = rrule.split(":", 1)[1].split(",")
        return all(d.strip().lower() in _WEEKDAY_MAP for d in days if d.strip())
    expr = cron_expr(rrule)
    if expr is not None:
        from croniter import croniter
        return croniter.is_valid(expr)
    return False


def cron_interval_ok(expr: str, min_minutes: int = CRON_MIN_INTERVAL_MINUTES) -> bool:
    """True se nenhum intervalo entre os próximos disparos for menor que
    `min_minutes`. Amostra alguns disparos a partir de agora — suficiente
    pra pegar '*/5 * * * *' e afins."""
    from croniter import croniter

    it = croniter(expr, datetime.now(timezone.utc))
    fires = [it.get_next(datetime) for _ in range(6)]
    return all(
        (b - a).total_seconds() >= min_minutes * 60
        for a, b in zip(fires, fires[1:])
    )


def cron_next_fire(expr: str, tz_name: str, *, base: datetime | None = None) -> datetime:
    """Próximo disparo da expressão (avaliada no tz do usuário), em UTC."""
    from croniter import croniter

    tz = ZoneInfo(tz_name)
    base_local = (base or datetime.now(timezone.utc)).astimezone(tz)
    nxt: datetime = croniter(expr, base_local).get_next(datetime)
    return nxt.astimezone(timezone.utc)


def next_due_from(rrule: str, after: datetime, tz_name: str = "America/Sao_Paulo") -> datetime:
    """Calcula o próximo disparo a partir de `after` (timezone-aware), no mesmo HH:MM.

    Pra 'cron:<expr>' a expressão é avaliada no timezone do usuário e a base
    é max(after, agora) — se o bot ficou fora do ar, não dispara rajada de
    ocorrências perdidas (no máx. 1 catch-up, que é o próprio `after` vencido).
    """
    expr = cron_expr(rrule)
    if expr is not None:
        base = max(as_utc(after) or after, datetime.now(timezone.utc))
        return cron_next_fire(expr, tz_name, base=base)
    if rrule == "daily":
        return after + timedelta(days=1)
    if rrule == "weekday":
        nxt = after + timedelta(days=1)
        while nxt.weekday() > 4:  # 5=sat, 6=sun
            nxt += timedelta(days=1)
        return nxt
    if rrule == "weekend":
        nxt = after + timedelta(days=1)
        while nxt.weekday() < 5:
            nxt += timedelta(days=1)
        return nxt
    if rrule == "monthly":
        # Próximo mês, mesmo dia. Edge case: dia 31 em mês com 30 dias → cai pro último dia.
        from calendar import monthrange
        year, month = after.year, after.month + 1
        if month > 12:
            month, year = 1, year + 1
        day = min(after.day, monthrange(year, month)[1])
        return after.replace(year=year, month=month, day=day)
    if rrule.startswith("weekly:"):
        wanted = {_WEEKDAY_MAP[d.strip().lower()] for d in rrule.split(":", 1)[1].split(",") if d.strip()}
        nxt = after + timedelta(days=1)
        for _ in range(8):
            if nxt.weekday() in wanted:
                return nxt
            nxt += timedelta(days=1)
    # Fallback: 1 dia. Evita loop infinito caso rrule estranho.
    return after + timedelta(days=1)


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
