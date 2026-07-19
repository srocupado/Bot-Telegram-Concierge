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


async def unmark_notified(session: AsyncSession, user_id: int, kind: str, key: str) -> None:
    await session.execute(delete(ProactiveNotice).where(
        ProactiveNotice.user_id == user_id,
        ProactiveNotice.kind == kind,
        ProactiveNotice.key == key,
    ))
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


# Checagem RETROATIVA do DOU: dia que falhou vira pendência persistente e é
# re-checado nas janelas seguintes quando o Inlabs voltar. fetch_mps cobre
# DO1E+DO1, então edição EXTRA de dia perdido entra também. Teto por janela
# (cada dia retroativo re-baixa os ZIPs do dia, ~100-200MB no Orange Pi) e
# expiração pra não insistir num dia problemático pra sempre.
_MP_RETRO_MAX_POR_JANELA = 2
_MP_RETRO_EXPIRA_DIAS = 14

# Fila de NOTA TÉCNICA pendente: MP detectada, usuário pediu a nota (botão) e o
# Inlabs caiu na hora de gerar — o pedido fica na fila (kind nota_pendente,
# key "AAAA-MM-DD:num1,num2|all") e é re-tentado silenciosamente a cada janela
# até sair. Teto 1/janela (nota é cara: web search + LLM + DOCX).
_NOTA_MAX_POR_JANELA = 1
_NOTA_PENDENTE_EXPIRA_DIAS = 14


async def _processar_notas_pendentes(bot, session: AsyncSession, user: User) -> None:
    """Re-tenta gerar/entregar notas pendentes. Sucesso → sai da fila (a
    entrega do deliver_to_user É a notificação); falha do Inlabs → silêncio
    (o usuário já foi avisado da fila no momento do pedido)."""
    from bot.services.dou_monitor import DouError, deliver_to_user
    rows = list(await session.scalars(
        select(ProactiveNotice).where(
            ProactiveNotice.user_id == user.id,
            ProactiveNotice.kind == "nota_pendente",
        )
    ))
    if not rows:
        return
    hoje = datetime.now(BRT).date()
    fila: list[tuple[date, list[str] | None, str]] = []
    for r in rows:
        date_part, _, nums = r.key.partition(":")
        try:
            d = date.fromisoformat(date_part)
        except ValueError:
            await unmark_notified(session, user.id, "nota_pendente", r.key)
            continue
        if (hoje - d).days > _NOTA_PENDENTE_EXPIRA_DIAS:
            await unmark_notified(session, user.id, "nota_pendente", r.key)
            continue
        numeros = [n for n in nums.split(",") if n and n != "all"] or None
        fila.append((d, numeros, r.key))
    for d, numeros, key in sorted(fila, key=lambda t: t[0])[:_NOTA_MAX_POR_JANELA]:
        try:
            await deliver_to_user(bot, session, user, d, force=True, only_numeros=numeros)
        except DouError as e:
            logger.warning("nota pendente %s: Inlabs ainda fora (%s)", key, e)
            continue
        except Exception:
            logger.exception("nota pendente %s: falha inesperada", key)
            continue
        await unmark_notified(session, user.id, "nota_pendente", key)
        logger.info("nota pendente %s entregue", key)


async def _mp_dias_pendentes(session: AsyncSession, user_id: int, hoje: date) -> list[date]:
    """Dias de DOU pendentes de checagem (antigos primeiro). Limpa do banco
    pendências expiradas e chaves inválidas."""
    rows = list(await session.scalars(
        select(ProactiveNotice).where(
            ProactiveNotice.user_id == user_id,
            ProactiveNotice.kind == "mp_pendente",
        )
    ))
    out: list[date] = []
    for r in rows:
        try:
            d = date.fromisoformat(r.key)
        except ValueError:
            await unmark_notified(session, user_id, "mp_pendente", r.key)
            continue
        if (hoje - d).days > _MP_RETRO_EXPIRA_DIAS:
            await unmark_notified(session, user_id, "mp_pendente", r.key)
            continue
        out.append(d)
    return sorted(out)


async def collect_mp(
    session: AsyncSession, user: User, dates: list[date], *, force: bool = False,
) -> list[ProactiveFact]:
    if not user.dou_mp_subscribed:
        return []
    from bot.services.dou_monitor import fetch_mps
    facts: list[ProactiveFact] = []
    seen: set[str] = set()
    failed: list[date] = []

    async def _colher(d: date) -> list[ProactiveFact]:
        """Facts das MPs de um dia (dedup por número e por já-notificada).
        Levanta exceção quando o fetch falha — o caller decide a pendência."""
        mps = await fetch_mps(d)
        out: list[ProactiveFact] = []
        for mp in mps:
            key = f"{mp['numero']}/{mp['ano']}"
            if key in seen:
                continue
            seen.add(key)
            if not force and await already_notified(session, user.id, "mp", key):
                continue
            ementa = _clean_ementa(mp.get("ementa") or "")
            out.append(ProactiveFact(
                "mp", "mp", key,
                f"📜 MP {mp['numero']}/{mp['ano']}: {ementa}",
                date_iso=d.isoformat(),
            ))
        return out

    ok_dates: set[date] = set()
    for d in dates:
        try:
            facts += await _colher(d)
            ok_dates.add(d)
        except Exception as exc:
            logger.warning("proactive: fetch_mps(%s) falhou: %s", d, exc)
            failed.append(d)

    # Dia que falhou vira PENDÊNCIA persistente — gravada JÁ (não no pós-envio):
    # precisa sobreviver mesmo que o envio desta janela falhe.
    for d in failed:
        if not await already_notified(session, user.id, "mp_pendente", d.isoformat()):
            await mark_notified(session, user.id, "mp_pendente", d.isoformat())

    # CRÍTICO: se NÃO conseguiu checar o DOU, AVISA — senão o usuário vê o
    # briefing sem MP e conclui (errado) que não houve MP publicada. Dedup por
    # conjunto de datas falhas (kind 'mp_fail' é marcado após o envio), pra
    # avisar 1x e não repetir a cada janela na mesma pane. date_iso=None mantém
    # o aviso FORA do botão de nota técnica (não é uma MP de verdade).
    if failed:
        fkey = "fail:" + ",".join(sorted(d.isoformat() for d in failed))
        if force or not await already_notified(session, user.id, "mp_fail", fkey):
            datas = ", ".join(d.strftime("%d/%m") for d in failed)
            facts.append(ProactiveFact(
                "mp", "mp_fail", fkey,
                f"⚠️ <b>Não consegui checar o DOU</b> de {datas} (Inlabs "
                "instável). NÃO assuma que não houve MP — confira depois com "
                "<code>/mp_dou_agora</code>.",
                date_iso=None,
            ))

    # Checagem RETROATIVA dos dias pendentes de janelas anteriores. A pendência
    # só é limpa APÓS o envio (run() → mp_retro), então falha de envio não
    # perde o dia. Dia pendente que já entrou na varredura normal desta janela
    # (ex.: briefing re-checa ontem) conta como coberto, sem novo fetch.
    hoje = datetime.now(BRT).date()
    pendentes = await _mp_dias_pendentes(session, user.id, hoje)
    resolvidos: list[date] = [d for d in pendentes if d in ok_dates]
    restantes = [d for d in pendentes if d not in dates][:_MP_RETRO_MAX_POR_JANELA]
    for d in restantes:
        try:
            facts += await _colher(d)
        except Exception as exc:
            logger.warning("proactive: retroativa DOU %s ainda falhando: %s", d, exc)
            continue
        resolvidos.append(d)
    for d in sorted(resolvidos):
        novas = sum(1 for f in facts if f.kind == "mp" and f.date_iso == d.isoformat())
        detalhe = f"{novas} MP(s) nova(s) acima" if novas else "nenhuma MP nova"
        facts.append(ProactiveFact(
            "mp", "mp_retro", f"retro:{d.isoformat()}",
            f"✅ Checagem retroativa do DOU de {d.strftime('%d/%m')} concluída — {detalhe}.",
            date_iso=None,
        ))

    # NOTAS na fila (pedidas com o Inlabs fora): linha de status em TODA janela
    # (kind nota_fila não é dedupado no run) até a entrega dar baixa.
    rows = list(await session.scalars(
        select(ProactiveNotice).where(
            ProactiveNotice.user_id == user.id,
            ProactiveNotice.kind == "nota_pendente",
        )
    ))
    for r in rows:
        date_part, _, nums = r.key.partition(":")
        try:
            d = date.fromisoformat(date_part)
        except ValueError:
            continue
        alvo = "todas as MPs" if (not nums or nums == "all") else f"MP {nums.replace(',', ', ')}"
        facts.append(ProactiveFact(
            "mp", "nota_fila", r.key,
            f"📄 Nota técnica na fila ({alvo} de {d.strftime('%d/%m')}) — "
            "Inlabs instável; tento gerar a cada janela e envio assim que sair.",
            date_iso=None,
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
    """Previsão do tempo do dia (Open-Meteo) pro briefing matinal. Em MODO
    VIAGEM ativo, usa as coords/fuso do DESTINO (com rótulo); senão HOME_COORDS.
    Roda todo dia; sem dedup (leitura fresca); falha não derruba o briefing."""
    from bot.services.viagem import effective_coords, effective_tz
    coords = effective_coords(user)
    label = f" em {user.viagem_destino}" if coords else ""
    tz = effective_tz(user) if coords else settings.timezone
    if not coords:
        coords = settings.home_coords
    if not coords:
        return []
    import httpx
    from bot.services.weather import fetch_today_weather, format_weather_line
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            w = await fetch_today_weather(client, coords, tz=tz)
    except Exception:
        logger.warning("proactive: previsão do tempo falhou", exc_info=True)
        return []
    linha = format_weather_line(w)
    if label:
        linha = f"✈️ {label.strip()}: {linha}"
    return [ProactiveFact("clima", "clima_hoje", "", linha)]


async def collect_moeda_viagem(user: User) -> list[ProactiveFact]:
    """Cotação da moeda local no briefing DURANTE a viagem (se configurada
    com 'moeda X'). Sem dedup (leitura fresca por dia)."""
    from bot.services.viagem import viagem_ativa
    moeda = getattr(user, "viagem_moeda", None)
    if not moeda or not viagem_ativa(user):
        return []
    try:
        from bot.services.cotacao import consultar_cotacao
        linha = await consultar_cotacao(moeda)
    except Exception:
        logger.warning("proactive: cotação da moeda da viagem falhou", exc_info=True)
        return []
    return [ProactiveFact("clima", "moeda_viagem", "", f"💱 {linha}")]


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
        facts += await collect_moeda_viagem(user)
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
            # nota_fila é linha de STATUS: repete a cada janela até a entrega
            # dar baixa na pendência (sem dedup).
            if f.kind == "nota_fila":
                continue
            await mark_notified(session, user.id, f.kind, f.key)
            # Retroativa do DOU ENTREGUE → o dia sai da pendência. Se o envio
            # falhar, a pendência fica e a retro repete na próxima janela.
            if f.kind == "mp_retro":
                await unmark_notified(
                    session, user.id, "mp_pendente", f.key.removeprefix("retro:"),
                )

    # Fila de notas técnicas pendentes (pedidas com Inlabs fora): re-tenta por
    # último — a geração é lenta/cara e não pode atrasar a mensagem da janela.
    if user.dou_mp_subscribed:
        try:
            await _processar_notas_pendentes(bot, session, user)
        except Exception:
            logger.exception("proactive: fila de notas pendentes falhou p/ user %s", user.id)
    return sent


async def purge_old_notices(session: AsyncSession, days: int = 90) -> int:
    cut = datetime.now(timezone.utc) - timedelta(days=days)
    res = await session.execute(delete(ProactiveNotice).where(ProactiveNotice.sent_at < cut))
    await session.commit()
    return res.rowcount or 0
