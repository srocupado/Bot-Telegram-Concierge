from __future__ import annotations

import logging
import re
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


# Sem espaço dentro ("18h", "18h30") e sem engolir o "às": a troca preserva
# o NÚMERO DE PALAVRAS, e o texto do lembrete sai das palavras originais.
_HORA_H_RE = re.compile(r"(?<![\w:])(\d{1,2})h(\d{2})?(?=$|[\s,.;!?])", re.IGNORECASE)
_HORA_SOLTA_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
# Conectores que sobram no fim do texto depois de tirar a data ("… às",
# "… daqui a", "… pra").
_CONECTOR_FIM_RE = re.compile(
    r"(?:\s+(?:[àa]s|as|em|daqui\s+a|daqui|para|pra|no\s+dia|dia|na|no))+\s*$",
    re.IGNORECASE)
_DURACAO_ANTES_RE = re.compile(r"(?:\bem|\bdaqui\s+a|\bdaqui\s+h[áa]|\bdentro\s+de)\s*$",
                               re.IGNORECASE)


def _normalizar_hora(texto: str) -> str:
    def _troca(m: re.Match) -> str:
        if _DURACAO_ANTES_RE.search(texto[:m.start()]):
            return m.group(0)          # "em 2h": duração, não hora do dia
        h = int(m.group(1))
        if h > 23:
            return m.group(0)
        return f"{h}:{m.group(2) or '00'}"
    return _HORA_H_RE.sub(_troca, texto)


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

    # "18h" / "18h30" / "às 9h" → "18:00" / "18:30" / "9:00". O dateparser
    # lê "18h" como DURAÇÃO ("daqui a 18 horas"): "sexta 18h" virava a quarta
    # seguinte à meia-noite e "amanhã às 15h" virava amanhã 21h45 (revisão de
    # 11/11/2026). "em 2h" / "daqui a 2h" continuam sendo duração.
    originais = raw.split()
    raw = _normalizar_hora(raw)

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

    if best_when is None and len(words) >= 2:
        # Hora solta no fim ("remédio às 22h"): o dateparser não entende
        # "22:00" sozinho. Hoje nessa hora; se já passou, amanhã.
        m_h = _HORA_SOLTA_RE.match(words[-1])
        if m_h and int(m_h.group(1)) <= 23 and int(m_h.group(2)) <= 59:
            alvo = now_local.replace(hour=int(m_h.group(1)), minute=int(m_h.group(2)),
                                     second=0, microsecond=0)
            if alvo <= now_local:
                alvo += timedelta(days=1)
            best_when, best_split = alvo, len(words) - 1

    if best_when is None or best_split <= 0:
        # Tenta a string inteira como fallback (caso o texto seja só uma data).
        parsed = dateparser.parse(raw, languages=["pt"], settings=settings)
        if parsed and parsed > now_local:
            raise ReminderParseError("informe um texto antes da data/hora")
        raise ReminderParseError(
            "não entendi a data/hora. Exemplos: 'em 2h', 'amanhã 09:00', 'sexta 18h'"
        )

    base = originais if len(originais) == len(words) else words
    clean_text = _CONECTOR_FIM_RE.sub("", " ".join(base[:best_split])).strip(" -—:,")
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
    tz_name: str = "America/Sao_Paulo",
) -> Reminder:
    if recurrence == "monthly":
        # Grava a ÂNCORA do dia ("monthly:31"): o passo mensal parte do dia já
        # clampado, então sem âncora um lembrete do dia 31 virava 28 depois de
        # fevereiro e nunca mais voltava (drift permanente). O dia local do
        # primeiro disparo é o intento do usuário.
        recurrence = f"monthly:{due_utc.astimezone(ZoneInfo(tz_name)).day}"
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


# Fast-path determinístico de "liste meus lembretes": o LLM às vezes inventava
# horários ou repetia uma lista velha do contexto em vez de chamar a tool. Quando
# a mensagem é claramente um PEDIDO DE LISTAGEM (e não criar/apagar/agendar),
# respondemos direto do banco, sem passar pelo modelo.
import re as _re  # noqa: E402
import unicodedata as _ud  # noqa: E402


def _norm_txt(s: str) -> str:
    s = _ud.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not _ud.combining(c))
    return _re.sub(r"\s+", " ", s).strip()


# Verbos que indicam OUTRA ação (não listar) — se aparecerem, não é fast-path.
_REM_ACTION_BLOCK = (
    "apag", "delet", "remov", "exclu", "cria", "criar", "cadastr", "marca",
    "adicion", "agend", "edita", "altera", "atualiz", "conclu", "desfaz",
    "me lembr", "lembra de", "lembre de", "lembrar de", "lembra-me", "lembre-me",
    # Verbos de CRIAÇÃO que faltavam (revisão de 11/11/2026): "coloca um
    # lembrete que eu tenho médico amanhã às 10h" devolvia a lista e não
    # criava nada.
    "coloc", "bota", "bote", "botar", "poe ", "por um", "anota", "anote",
    "faz um", "faca um", "novo lembrete", "nova lembrete", "quero um",
    "preciso de um", "cancel", "tira ", "tirar", "muda", "troca", "adia",
)
# Hora marcada ("10h", "às 15h30", "09:00") = pedido de criação/alteração,
# nunca de listagem.
_REM_HORA_RE = _re.compile(r"\b\d{1,2}\s*(h\b|h\d{2}\b|:\d{2}\b|\s+horas?\b)")
# Pistas de que o usuário quer VER a lista.
_REM_LIST_CUES = (
    "lista", "liste", "listar", "listos", "quais", "quantos", "mostra",
    "meus lembrete", "os lembrete", "tem lembrete", "tem algum lembrete",
    "que lembrete", "ver lembrete", "ver os lembrete", "tenho",
)


def is_list_reminders_request(text: str) -> bool:
    """True só quando a mensagem é, sem ambiguidade, um pedido pra LISTAR os
    lembretes pendentes (e não criar/apagar/agendar/concluir)."""
    n = _norm_txt(text)
    if "lembrete" not in n:
        return False
    if any(b in n for b in _REM_ACTION_BLOCK):
        return False
    if _REM_HORA_RE.search(n):
        return False
    return any(c in n for c in _REM_LIST_CUES)


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
    if rrule.startswith("monthly:"):
        # Forma normalizada com âncora de dia (ver create_reminder).
        try:
            return 1 <= int(rrule.split(":", 1)[1]) <= 31
        except ValueError:
            return False
    if rrule.startswith("weekly:"):
        days = [d.strip() for d in rrule.split(":", 1)[1].split(",") if d.strip()]
        # `all()` sobre lista VAZIA é True: "weekly:" sem dias passava na
        # validação e caía no fallback diário do _passo_recorrencia — usuário
        # pedia semanal e recebia todo dia, em silêncio.
        return bool(days) and all(d.lower() in _WEEKDAY_MAP for d in days)
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

    # A aritmética roda no fuso do usuário e o resultado volta pra UTC — assim
    # o HH:MM LOCAL é preservado. Somando no instante UTC, um recorrente criado
    # em viagem (8h de Tóquio) continuaria no mesmo instante e viraria 20h BRT
    # pra sempre depois da volta. `tz_name` é o fuso EFETIVO de quem dispara.
    tz = ZoneInfo(tz_name)
    base_local = (as_utc(after) or after).astimezone(tz)
    agora_local = datetime.now(timezone.utc).astimezone(tz)

    prox = _passo_recorrencia(rrule, base_local)
    # GUARDA DE CATCH-UP — a mesma que o ramo `cron:` já tinha, e que faltava
    # aqui. O scheduler reagenda com `sent=False`, então devolver instante no
    # PASSADO fazia o lembrete vencer de novo no tick seguinte (60s): bot fora
    # 5 dias = 5 mensagens em 5 minutos. Avança até o primeiro disparo futuro;
    # a ocorrência vencida já foi entregue nesta rodada (1 catch-up, como no
    # cron), o resto é rajada de coisa que o dono não pode mais atender.
    saltos = 0
    while prox <= agora_local and saltos < _MAX_SALTOS_CATCHUP:
        prox = _passo_recorrencia(rrule, prox)
        saltos += 1
    if saltos >= _MAX_SALTOS_CATCHUP:
        # Não deveria acontecer (daily precisa de 500 dias parado). Se
        # acontecer, entregar no futuro é melhor que travar o tick num laço.
        logger.warning(
            "reminders: catch-up de '%s' esgotou %d saltos a partir de %s",
            rrule, _MAX_SALTOS_CATCHUP, base_local.isoformat(),
        )
        prox = max(prox, agora_local + timedelta(minutes=1))
    return prox.astimezone(timezone.utc)


# Teto de segurança do laço de catch-up: 500 saltos cobrem 500 dias de
# 'daily' ou ~9 anos de 'weekly' parado.
_MAX_SALTOS_CATCHUP = 500


def _passo_recorrencia(rrule: str, base_local: datetime) -> datetime:
    """UM avanço da recorrência a partir de `base_local` (no fuso do usuário).

    Separado de `next_due_from` porque o catch-up precisa aplicar o mesmo
    passo várias vezes; com a lógica inline, a guarda teria que duplicar as
    regras de cada rrule — e um dia divergiria de mansinho.
    """
    if rrule == "daily":
        return base_local + timedelta(days=1)
    if rrule == "weekday":
        nxt = base_local + timedelta(days=1)
        while nxt.weekday() > 4:  # 5=sat, 6=sun
            nxt += timedelta(days=1)
        return nxt
    if rrule == "weekend":
        nxt = base_local + timedelta(days=1)
        while nxt.weekday() < 5:
            nxt += timedelta(days=1)
        return nxt
    if rrule == "monthly" or rrule.startswith("monthly:"):
        # Próximo mês, no dia da ÂNCORA ("monthly:31" — ver create_reminder);
        # mês curto clampa pro último dia SEM perder a âncora (31/jan → 28/fev →
        # 31/mar). "monthly" legado (sem âncora) usa o dia da base — o
        # comportamento antigo, que drifta após um clamp.
        from calendar import monthrange
        anchor = base_local.day
        if ":" in rrule:
            try:
                anchor = min(max(int(rrule.split(":", 1)[1]), 1), 31)
            except ValueError:
                pass
        year, month = base_local.year, base_local.month + 1
        if month > 12:
            month, year = 1, year + 1
        day = min(anchor, monthrange(year, month)[1])
        return base_local.replace(year=year, month=month, day=day)
    if rrule.startswith("weekly:"):
        wanted = {_WEEKDAY_MAP[d.strip().lower()] for d in rrule.split(":", 1)[1].split(",") if d.strip()}
        nxt = base_local + timedelta(days=1)
        for _ in range(8):
            if nxt.weekday() in wanted:
                return nxt
            nxt += timedelta(days=1)
    # Fallback: 1 dia. Evita loop infinito caso rrule estranho.
    return base_local + timedelta(days=1)


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
