"""Agente proativo (opt-in): avisa o usuário por conta própria, sem ser
perguntado. Gatilhos 100% determinísticos (queries); o LLM entra só como
redator opcional (PROACTIVE_USE_LLM) dos fatos já coletados — nunca decide
o que vigiar nem inventa dados.

Categorias:
- vencimentos: lembretes chegando (não recorrentes) + vencimento da fatura.
- tarefas: tarefas abertas (/tarefas) no briefing matinal e no resumo do fim
  do dia — lembrete até concluir (sem dedup).
- mp: Medidas Provisórias novas no DOU (substitui o digest fixo das 18h).
- nudges: inatividade (treino, lançamentos financeiros, lista de compras).

Janelas: PROACTIVE_HOURS (BRT). Na hora do briefing (PROACTIVE_BRIEFING_HOUR)
consolida e cobre também as MPs do dia anterior (pega edições tardias).
Anti-ruído: 1 mensagem por janela, dedup (kind,key) em ProactiveNotice,
cooldown por kind nos nudges, silêncio total quando não há nada.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import ProactiveNotice, Reminder, User, WorkoutLog
from bot.services import shopping
from bot.services import tasks as tasks_svc
from bot.services.reminders import as_utc, format_reminder_line

logger = logging.getLogger(__name__)

BRT = ZoneInfo("America/Sao_Paulo")

_PROACTIVE_SYSTEM = (
    "Você é um assistente pessoal sendo PROATIVO. Reescreva os AVISOS abaixo "
    "numa ÚNICA mensagem curta e amigável em português (HTML do Telegram: "
    "<b>, emojis simples). REGRAS: use SOMENTE os fatos fornecidos; NÃO invente "
    "datas, valores ou itens; NÃO dê conselhos não pedidos; seja conciso."
)


@dataclass
class ProactiveFact:
    category: str       # 'venc' | 'mp' | 'nudge'
    kind: str           # = ProactiveNotice.kind
    key: str            # = ProactiveNotice.key
    text: str           # linha já formatada (determinística)
    date_iso: str | None = None  # MP: data de publicação no DOU (pro botão "gerar nota")


# ──────────────────────── dedup ────────────────────────

async def already_notified(session: AsyncSession, user_id: int, kind: str, key: str) -> bool:
    row = await session.scalar(
        select(ProactiveNotice.id).where(
            ProactiveNotice.user_id == user_id,
            ProactiveNotice.kind == kind,
            ProactiveNotice.key == key,
        ).limit(1)
    )
    return row is not None


async def mark_notified(session: AsyncSession, user_id: int, kind: str, key: str) -> None:
    session.add(ProactiveNotice(user_id=user_id, kind=kind, key=key))
    await session.commit()


async def _nudge_recent(session: AsyncSession, user_id: int, kind: str, cooldown_days: int) -> bool:
    """True se já houve um nudge desse kind há menos de cooldown_days
    (evita repetir o mesmo nudge todo dia)."""
    last = await session.scalar(
        select(func.max(ProactiveNotice.sent_at)).where(
            ProactiveNotice.user_id == user_id, ProactiveNotice.kind == kind,
        )
    )
    if last is None:
        return False
    return (datetime.now(timezone.utc) - as_utc(last)) < timedelta(days=cooldown_days)


def parse_proactive_hours(csv: str) -> set[int]:
    """CSV de horas BRT → set[int]; inclui sempre o briefing_hour."""
    hours: set[int] = set()
    for part in (csv or "").split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 23:
            hours.add(int(part))
    hours.add(settings.proactive_briefing_hour)
    return hours


# ──────────────────────── coletores ────────────────────────

async def collect_vencimentos(
    session: AsyncSession, user: User, now_brt: datetime, *, force: bool = False,
) -> list[ProactiveFact]:
    facts: list[ProactiveFact] = []
    tz = ZoneInfo(user.timezone)
    now_utc = now_brt.astimezone(timezone.utc)
    horizon = now_utc + timedelta(hours=settings.proactive_lookahead_hours)

    # Lembretes chegando (não recorrentes — recorrentes já disparam no horário).
    rems = (await session.scalars(
        select(Reminder).where(
            Reminder.user_id == user.id,
            Reminder.sent.is_(False),
            Reminder.recurrence.is_(None),
            Reminder.due_at > now_utc,
            Reminder.due_at <= horizon,
        ).order_by(Reminder.due_at)
    )).all()
    # Vencimentos NÃO são deduplicados: o aviso deve repetir em TODA janela até
    # o pagamento (sent=True) ou o vencimento passar. A trava run_key evita
    # repetir dentro da mesma janela.
    for r in rems:
        key = f"{r.id}:{as_utc(r.due_at).astimezone(tz).date().isoformat()}"
        facts.append(ProactiveFact("venc", "venc_rem", key,
                                    "⏳ " + format_reminder_line(r, user.timezone)))

    # Vencimento da fatura do cartão (financeiro/Firestore).
    try:
        from bot.services.financeiro import card_due_soon
        lookahead_days = max(3, settings.proactive_lookahead_hours // 24)
        res = await card_due_soon(session, user, now_brt.date(), lookahead_days)
    except Exception:
        res = None
    if res:
        facts.append(ProactiveFact(
            "venc", "card_due", res["month_key"],
            f"💳 Fatura do cartão vence em <b>{res['due_date'].strftime('%d/%m')}</b>.",
        ))
    return facts


async def collect_mp(
    session: AsyncSession, user: User, dates: list[date], *, force: bool = False,
) -> list[ProactiveFact]:
    if not user.dou_mp_subscribed:
        return []
    from bot.services.dou_monitor import fetch_mps
    facts: list[ProactiveFact] = []
    seen: set[str] = set()
    for d in dates:
        try:
            mps = await fetch_mps(d)
        except Exception as exc:
            logger.warning("proactive: fetch_mps(%s) falhou: %s", d, exc)
            continue
        for mp in mps:
            key = f"{mp['numero']}/{mp['ano']}"
            if key in seen:
                continue
            seen.add(key)
            if not force and await already_notified(session, user.id, "mp", key):
                continue
            ementa = _clean_ementa(mp.get("ementa") or "")
            facts.append(ProactiveFact(
                "mp", "mp", key,
                f"📜 MP {mp['numero']}/{mp['ano']}: {ementa}",
                date_iso=d.isoformat(),
            ))
    return facts


def _clean_ementa(ementa: str, limit: int = 220) -> str:
    """Limpa a ementa pro aviso leve: remove o TÍTULO do próprio ato que às
    vezes vem anexado no fim ('... MEDIDA PROVISÓRIA Nº 1.371, DE 22 DE JUNHO
    DE 2026 ...') e trunca em limite com '…'.

    O título anexado é MAIÚSCULO e datado. Uma menção a OUTRA MP DENTRO da
    ementa ('Altera a Medida Provisória nº 1.354, de 30 de abril...') vem em
    caixa-título/minúscula e NÃO pode cortar — antes, com IGNORECASE, cortava
    nela e a ementa virava só 'Altera a'."""
    e = re.sub(r"\s+", " ", ementa).strip()
    # casa só o título anexado: MAIÚSCULO + número + ', DE <dia>' (case-sensitive)
    cut = re.search(r"MEDIDA\s+PROVIS[ÓO]RIA\s+N\S*\s*[\d.]+,?\s+DE\s+\d", e)
    if cut and cut.start() > 0:
        e = e[:cut.start()].strip()
    if len(e) > limit:
        e = e[:limit].rsplit(" ", 1)[0].rstrip(" .,;") + "…"
    return e


async def collect_nudges(
    session: AsyncSession, user: User, now_brt: datetime, *, force: bool = False,
) -> list[ProactiveFact]:
    facts: list[ProactiveFact] = []
    today = now_brt.date()
    cooldown = settings.proactive_nudge_cooldown_days

    async def _ok(kind: str) -> bool:
        if force:
            return True
        key = today.isoformat()
        if await already_notified(session, user.id, kind, key):
            return False
        return not await _nudge_recent(session, user.id, kind, cooldown)

    # Treino parado.
    last_w = await session.scalar(select(func.max(WorkoutLog.date)).where(WorkoutLog.user_id == user.id))
    if last_w is not None:
        dias = (today - last_w).days
        if dias >= settings.proactive_workout_idle_days and await _ok("nudge_workout"):
            facts.append(ProactiveFact("nudge", "nudge_workout", today.isoformat(),
                                       f"🏋️ Você não registra treino há {dias} dias."))

    # Lançamentos financeiros parados.
    try:
        from bot.services.financeiro import last_finance_activity
        last_f = await last_finance_activity(session, user)
    except Exception:
        last_f = None
    if last_f is not None:
        dias = (today - last_f).days
        if dias >= settings.proactive_finance_idle_days and await _ok("nudge_finance"):
            facts.append(ProactiveFact("nudge", "nudge_finance", today.isoformat(),
                                       f"💸 Faz {dias} dias que você não lança nada no financeiro."))

    # Lista de compras parada.
    items = await shopping.list_items(session, user.id, only_pending=True)
    if items:
        oldest = as_utc(min(i.created_at for i in items))
        dias = (today - oldest.astimezone(ZoneInfo(user.timezone)).date()).days
        if dias >= settings.proactive_shopping_idle_days and await _ok("nudge_shopping"):
            n = len(items)
            facts.append(ProactiveFact("nudge", "nudge_shopping", today.isoformat(),
                                       f"🛒 Sua lista de compras tem {n} item(ns) parado(s) há {dias} dias."))
    return facts


_TASKS_LIMIT = 12  # teto de tarefas na mensagem (evita briefing gigante)


async def collect_tarefas(
    session: AsyncSession, user: User, now_brt: datetime,
) -> list[ProactiveFact]:
    """Tarefas abertas (/tarefas) pro briefing matinal e o resumo do fim do
    dia — lembrete pra não esquecer. Sem dedup: repete até o usuário concluir.
    Mostra idade em dias pra dar relevo às que estão paradas; corta no teto."""
    tarefas = await tasks_svc.list_open_tasks(session, user.id)
    if not tarefas:
        return []
    tz = ZoneInfo(user.timezone)
    today = now_brt.date()
    facts: list[ProactiveFact] = []
    for t in tarefas[:_TASKS_LIMIT]:
        dias = (today - as_utc(t.created_at).astimezone(tz).date()).days
        idade = f"  <i>(há {dias}d)</i>" if dias >= 1 else ""
        facts.append(ProactiveFact("tarefas", "tarefa", str(t.id), f"• {t.text}{idade}"))
    extra = len(tarefas) - _TASKS_LIMIT
    if extra > 0:
        facts.append(ProactiveFact("tarefas", "tarefa_more", "more",
                                   f"… e mais {extra} tarefa(s) — veja em /tarefas"))
    return facts


async def collect_clima(user: User, now_brt: datetime) -> list[ProactiveFact]:
    """Previsão do tempo do dia (Open-Meteo, HOME_COORDS) pro briefing
    matinal. Roda todo dia (clima interessa também no fim de semana). Sem
    dedup (leitura fresca a cada manhã); falha silenciosa não derruba o
    briefing."""
    if not settings.home_coords:
        return []
    import httpx
    from bot.services.weather import fetch_today_weather, format_weather_line
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            w = await fetch_today_weather(
                client, settings.home_coords, tz=settings.timezone,
            )
    except Exception:
        logger.warning("proactive: previsão do tempo falhou", exc_info=True)
        return []
    return [ProactiveFact("clima", "clima_hoje", "", format_weather_line(w))]


async def collect_transito(user: User, now_brt: datetime) -> list[ProactiveFact]:
    """Trânsito casa → trabalho pro briefing matinal (dias úteis). Reusa o
    fetch do digest de trânsito. Sem dedup (leitura fresca a cada manhã)."""
    if now_brt.weekday() > 4:  # fim de semana: sem trânsito pro trabalho
        return []
    if not (settings.home_coords and settings.work_coords and settings.google_maps_api_key):
        return []
    import httpx
    from bot.services.traffic import (
        USER_AGENT as TRAFFIC_USER_AGENT,
        fetch_traffic_with_alternative,
        format_traffic_briefing,
        parse_route_waypoints,
    )
    api_key = settings.google_maps_api_key.get_secret_value()
    try:
        async with httpx.AsyncClient(
            timeout=20.0, follow_redirects=True,
            headers={"User-Agent": TRAFFIC_USER_AGENT},
        ) as client:
            waypoints: list[str] = []
            if settings.route_google_maps_url:
                waypoints = await parse_route_waypoints(client, settings.route_google_maps_url)
            # Duas rotas comparadas (mesma leitura do /transito_agora), não só uma.
            pref, alt = await fetch_traffic_with_alternative(
                client, api_key, settings.home_coords, settings.work_coords,
                waypoints, maps_url=settings.route_google_maps_url or "",
            )
    except Exception:
        logger.warning("proactive: trânsito casa→trabalho falhou", exc_info=True)
        return []
    txt = format_traffic_briefing(pref, alt)
    return [ProactiveFact("transito", "transito_trabalho", "", txt)]


async def collect_carteira(
    session: AsyncSession, user: User, now_brt: datetime, *, force: bool = False,
) -> list[ProactiveFact]:
    """Revisão da carteira (ações/FIIs/ETFs) na ÚLTIMA janela do dia: busca a
    cotação de mercado atual (brapi), atualiza o currentPrice no Firestore e
    monta valor investido vs valor de mercado por ativo. Tesouro fica fora
    (não tem cotação de bolsa). 1×/dia (deduplicado por data)."""
    last_hour = max(parse_proactive_hours(settings.proactive_hours))
    if not force and now_brt.hour != last_hour:
        return []
    try:
        from bot.services.financeiro import (
            atualizar_cotacoes_carteira,
            format_carteira_review,
            get_carteira_tickers,
        )
        from bot.services.quotes import QuotesError, fetch_quotes

        tickers = await get_carteira_tickers(session, user)
        if not tickers:
            return []
        try:
            prices = await fetch_quotes(tickers)
        except QuotesError as e:
            logger.warning("proactive: cotação indisponível (%s)", e)
            return []
        if not prices:
            return []
        assets = await atualizar_cotacoes_carteira(session, user, prices)
        text = format_carteira_review(assets, prices)
        if not text:
            return []
    except Exception:
        logger.exception("proactive: revisão de carteira falhou p/ user %s", user.id)
        return []
    key = now_brt.date().isoformat()
    return [ProactiveFact("carteira", "carteira_review", key, text)]


# ──────────────────────── orquestrador ────────────────────────

_CAT_HEADER = {
    "clima": "🌦️ <b>Clima hoje</b>",
    "transito": "🚗 <b>Trânsito casa → trabalho</b>",
    "venc": "⏳ <b>Chegando</b>",
    "tarefas": "📋 <b>Tarefas abertas</b>",
    "mp": "📜 <b>Diário Oficial</b>",
    "nudge": "💡 <b>Hábitos</b>",
    "carteira": "📈 <b>Carteira hoje</b>",
}


def _compose(facts: list[ProactiveFact], *, briefing: bool) -> str:
    blocks: list[str] = []
    if briefing:
        blocks.append("☀️ <b>Bom dia! Resumo de hoje</b>")
    for cat in ("clima", "transito", "venc", "tarefas", "mp", "nudge", "carteira"):
        lines = [f.text for f in facts if f.category == cat]
        if not lines:
            continue
        blocks.append(_CAT_HEADER[cat] + "\n" + "\n".join(lines))
    return "\n\n".join(blocks)


async def _send(bot, chat_id: int, text: str, reply_markup=None) -> bool:
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML",
                               disable_web_page_preview=True, reply_markup=reply_markup)
        return True
    except Exception:
        logger.exception("proactive: HTML send failed; retrying plain for %d", chat_id)
        try:
            await bot.send_message(chat_id, text, parse_mode=None,
                                   disable_web_page_preview=True, reply_markup=reply_markup)
            return True
        except Exception:
            logger.exception("proactive: failed to send to %d", chat_id)
            return False


async def _redigir(user: User, deterministic: str) -> str:
    """Redação opcional via LLM (sem tools). Fallback ao texto determinístico."""
    if not settings.proactive_use_llm:
        return deterministic
    try:
        from bot.services.llm.factory import get_provider_for_user
        provider = get_provider_for_user(user)
        out = await provider.chat(
            [{"role": "user", "content": deterministic}],
            system=_PROACTIVE_SYSTEM, max_tokens=400,
        )
        return (out or "").strip() or deterministic
    except Exception:
        logger.exception("proactive: LLM redação falhou; usando texto determinístico")
        return deterministic


async def run_for_user(
    bot, session: AsyncSession, user: User, now_brt: datetime, *,
    window: str, force: bool = False,
) -> bool:
    """Coleta fatos da janela, monta UMA mensagem e envia. Marca dedup só
    após envio OK. Retorna True se enviou."""
    briefing = window == "briefing"
    today = now_brt.date()
    mp_dates = [today - timedelta(days=1), today] if briefing else [today]

    # Trava de nível-janela: roda 1x por (janela, dia, hora). Sem isso, como o
    # tick é de ~20s e a janela é minute<=1, rodaria ~5x — refazendo fetch de
    # DOU/coletas à toa. Marca já na entrada (mesmo que dê "sem fatos") pra os
    # ticks seguintes pularem. force (/proativo_agora) ignora a trava.
    if not force:
        run_key = f"{window}:{today.isoformat()}:{now_brt.hour}"
        if await already_notified(session, user.id, "proactive_run", run_key):
            return False
        await mark_notified(session, user.id, "proactive_run", run_key)

    # Resumo do fim do dia = última janela proativa (mesma régua da carteira).
    last_hour = max(parse_proactive_hours(settings.proactive_hours))
    end_of_day = (not briefing) and (force or now_brt.hour == last_hour)

    facts: list[ProactiveFact] = []
    if briefing:
        facts += await collect_clima(user, now_brt)
        facts += await collect_transito(user, now_brt)
    facts += await collect_vencimentos(session, user, now_brt, force=force)
    # Tarefas abertas no briefing matinal e no resumo do fim do dia.
    if briefing or end_of_day:
        facts += await collect_tarefas(session, user, now_brt)
    facts += await collect_mp(session, user, mp_dates, force=force)
    facts += await collect_nudges(session, user, now_brt, force=force)
    if not briefing:
        facts += await collect_carteira(session, user, now_brt, force=force)

    if not facts:
        logger.info("proactive: user %d window=%s sem fatos", user.id, window)
        return False

    text = await _redigir(user, _compose(facts, briefing=briefing))

    # Botão de nota técnica quando houver MP nos facts. Usa a data da MP
    # (não o `today` da execução), pra cobrir briefing que junta ontem+hoje.
    # Se houver MPs de mais de uma data, usa a mais recente — o usuário ainda
    # pode chamar /mp_dou_agora <data> pras outras. Passa os NÚMEROS detectados
    # nesta notificação (key = "numero/ano") pra nota cobrir só essas MPs — sem
    # isso o botão regerava todas as MPs do dia (ex.: 19h refazia as das 13h).
    reply_markup = None
    mp_facts = [f for f in facts if f.category == "mp" and f.date_iso]
    if mp_facts:
        from bot.handlers.dou_mp import nota_keyboard
        latest_date = max(f.date_iso for f in mp_facts)
        numeros = [f.key.split("/")[0] for f in mp_facts if f.date_iso == latest_date]
        reply_markup = nota_keyboard(latest_date, numeros)

    sent = await _send(bot, user.id, text, reply_markup=reply_markup)
    logger.info("proactive: user %d window=%s %d fatos enviado=%s", user.id, window, len(facts), sent)
    if sent and not force:
        for f in facts:
            # clima, trânsito e vencimentos não têm dedup: repetem a cada
            # janela (clima/trânsito = leitura fresca; vencimento = lembrar
            # até pagar).
            if f.category in ("clima", "transito", "venc", "tarefas"):
                continue
            await mark_notified(session, user.id, f.kind, f.key)
    return sent


async def purge_old_notices(session: AsyncSession, days: int = 90) -> int:
    cut = datetime.now(timezone.utc) - timedelta(days=days)
    res = await session.execute(delete(ProactiveNotice).where(ProactiveNotice.sent_at < cut))
    await session.commit()
    return res.rowcount or 0
