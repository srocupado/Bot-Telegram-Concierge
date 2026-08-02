"""CRUD de TravelWatch + verificação diária + envio de alertas.

Equivalente ao `bot/services/scheduler.py::check_watch` do Telegram-Travels,
adaptado pra reusar `SessionLocal`/`settings` do Concierge e rodar no
`tick()` do scheduler existente — sem loop próprio.
"""
from __future__ import annotations

import logging
from html import escape as _html_escape
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import settings
from bot.db.models import TravelAlert, TravelPriceSnapshot, TravelWatch, User
from bot.utils import as_utc
from bot.services.travels.serpapi_client import (
    SerpAPIClient,
    SerpAPIError,
    attach_return_leg,
    extract_best_flight,
    extract_best_hotel,
    extract_price_insights,
    find_best_flight_in_window,
    find_best_hotel_in_window,
    format_flight,
    format_hotel,
    hotel_name_matches,
)

logger = logging.getLogger(__name__)

BRT = ZoneInfo("America/Sao_Paulo")


def _is_due(watch: TravelWatch, now_utc: datetime) -> bool:
    now_brt = now_utc.astimezone(BRT)
    if now_brt.hour < settings.travels_alert_hour:
        return False
    last_checked = as_utc(watch.last_checked_at)
    if last_checked is None:
        return True
    last_brt = last_checked.astimezone(BRT)
    return last_brt.date() < now_brt.date()


def _should_alert(
    watch: TravelWatch, new_price: float
) -> tuple[bool, str]:
    now = datetime.now(timezone.utc)
    snooze_until = as_utc(watch.snooze_until)
    if snooze_until and snooze_until > now:
        return False, "snoozed"
    if watch.max_price is not None:
        if new_price > watch.max_price:
            return False, "above_max"
        # Abaixo do teto: avisa na PRIMEIRA vez e depois só se MELHORAR
        # (novo mínimo). Antes repetia o mesmo alerta todo santo dia e a única
        # saída do usuário era cancelar o watch que ele quis criar — o campo
        # snooze_until existe no modelo mas nada no bot o escreve.
        if watch.last_alert_at is None:
            return True, "below_max"
        if watch.min_price_seen is None or new_price < watch.min_price_seen:
            return True, "new_min"
        return False, "ja_avisado"
    # Sem teto: avisa só quando bate mínimo histórico (ou é a primeira leitura).
    if watch.min_price_seen is None or new_price < watch.min_price_seen:
        return True, "new_min"
    return False, "no_change"


def _headline(kind: str, summary: str, price: float, reason: str) -> str:
    emoji = "✈️" if kind == "flight" else "🏨"
    motivo = {
        "below_max": " (atingiu seu teto)",
        "new_min": " (mínimo histórico)",
        "daily": "",
    }.get(reason, "")
    return f"{emoji} <b>Alerta de preço:</b> {summary}\nAgora: R$ {price:.2f}{motivo}"


async def _send_with_fallback(bot: Bot, chat_id: int, text: str) -> bool:
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception:
        logger.exception("travels: HTML send failed; retrying plain for chat %d", chat_id)
        try:
            await bot.send_message(chat_id, text, parse_mode=None, disable_web_page_preview=True)
            return True
        except Exception:
            logger.exception("travels: plain send failed for chat %d", chat_id)
            return False


# Falhas consecutivas até avisar o dono, e periodicidade dos lembretes
# seguintes. 3 dias tolera pane passageira do SerpAPI sem incomodar; depois
# disso a vigia não está vigiando nada e o dono precisa saber — silêncio aqui
# é indistinguível de "o preço não caiu", que é o falso negativo que este
# projeto não aceita (ver CLAUDE.md).
_FALHAS_PRA_AVISAR = 3
_REPETE_AVISO_A_CADA = 7


async def _registrar_falha(
    session: AsyncSession, bot: Bot, watch: TravelWatch, motivo: str,
) -> None:
    """Conta a falha, marca o dia como checado e avisa o dono quando a vigia
    passa a ser fachada.

    Marcar `last_checked_at` mesmo na falha é deliberado (evita re-executar a
    cada tick e queimar cota); o que faltava era o dono ficar sabendo.
    """
    watch.last_checked_at = datetime.now(timezone.utc)
    watch.consecutive_failures = (watch.consecutive_failures or 0) + 1
    watch.last_error = motivo[:300]
    n = watch.consecutive_failures
    await session.commit()

    # Avisa por ESTADO, não por contagem exata: no marco (>=3 e nunca avisado
    # neste streak) ou 7 falhas após o último aviso BEM-SUCEDIDO. Se o envio
    # falhar, `alerted_at_failures` não avança e o próximo tick re-tenta — o
    # marco perdido não vira silêncio de uma semana.
    ja_avisado = watch.alerted_at_failures or 0
    deve_avisar = n >= _FALHAS_PRA_AVISAR and (
        ja_avisado == 0 or n - ja_avisado >= _REPETE_AVISO_A_CADA
    )
    if not deve_avisar:
        return
    user = await session.get(User, watch.user_id)
    if user is None:
        return
    alvo = watch.summary or watch.kind
    enviado = await _send_with_fallback(bot, user.id, (
        f"⚠️ Não estou conseguindo checar a vigia <b>{_html_escape(alvo)}</b> "
        f"há {n} dia(s) seguidos.\n"
        f"Motivo da última tentativa: <code>{_html_escape(motivo[:200])}</code>\n\n"
        "Ela continua tentando todo dia — mas <b>não conte com alerta de preço "
        "dela até isso voltar</b>. Se o parâmetro mudou (aeroporto, data que já "
        "passou), refaça a vigia."
    ))
    if enviado:
        # Só avança o marco quando o aviso REALMENTE saiu. Falha de envio →
        # re-tenta no próximo tick (contador já continua subindo).
        watch.alerted_at_failures = n
        await session.commit()


async def check_watch(
    session: AsyncSession,
    serpapi: SerpAPIClient,
    bot: Bot,
    watch: TravelWatch,
) -> None:
    chosen_ci: str | None = None
    chosen_co: str | None = None
    chosen_dep: str | None = None
    chosen_ret: str | None = None
    insights: dict | None = None
    best: tuple[float, dict[str, Any]] | None = None
    try:
        if watch.kind == "flight":
            if watch.params.get("window_start") and watch.params.get("nights"):
                dests = watch.params.get("destination_iatas") or (
                    [watch.params["destination_iata"]]
                    if watch.params.get("destination_iata") else []
                )
                flex = await find_best_flight_in_window(
                    serpapi,
                    watch.params["origin_iata"],
                    dests,
                    watch.params["window_start"],
                    watch.params["window_end"],
                    int(watch.params["nights"]),
                    adults=watch.params.get("adults", 1),
                    currency=watch.currency,
                    travel_class=int(watch.params.get("travel_class", 1)),
                )
                if flex is not None:
                    price, payload, chosen_dep, chosen_ret, chosen_dest, insights = flex
                    await attach_return_leg(
                        serpapi, payload,
                        origin_iata=watch.params["origin_iata"],
                        destination_iata=chosen_dest,
                        depart_date=chosen_dep, return_date=chosen_ret,
                        adults=watch.params.get("adults", 1),
                        currency=watch.currency,
                        travel_class=int(watch.params.get("travel_class", 1)),
                    )
                    best = (price, payload)
            else:
                single_dest = watch.params.get("destination_iata") or (
                    (watch.params.get("destination_iatas") or [""])[0]
                )
                chosen_dep = watch.params["depart_date"]
                chosen_ret = watch.params.get("return_date")
                raw = await serpapi.search_flights(
                    origin_iata=watch.params["origin_iata"],
                    destination_iata=single_dest,
                    depart_date=watch.params["depart_date"],
                    return_date=watch.params.get("return_date"),
                    adults=watch.params.get("adults", 1),
                    currency=watch.currency,
                    travel_class=int(watch.params.get("travel_class", 1)),
                )
                best = extract_best_flight(raw)
                insights = extract_price_insights(raw)
                if best is not None and watch.params.get("return_date"):
                    await attach_return_leg(
                        serpapi, best[1],
                        origin_iata=watch.params["origin_iata"],
                        destination_iata=single_dest,
                        depart_date=watch.params["depart_date"],
                        return_date=watch.params["return_date"],
                        adults=watch.params.get("adults", 1),
                        currency=watch.currency,
                        travel_class=int(watch.params.get("travel_class", 1)),
                    )
        elif watch.kind == "hotel":
            if watch.params.get("nights") and watch.params.get("window_start"):
                flex = await find_best_hotel_in_window(
                    serpapi,
                    watch.params["location"],
                    watch.params["window_start"],
                    watch.params["window_end"],
                    int(watch.params["nights"]),
                    adults=watch.params.get("adults", 2),
                    currency=watch.currency,
                )
                if flex is not None:
                    price, payload, chosen_ci, chosen_co = flex
                    best = (price, payload)
            else:
                chosen_ci = watch.params["check_in"]
                chosen_co = watch.params["check_out"]
                raw = await serpapi.search_hotels(
                    location=watch.params["location"],
                    check_in=watch.params["check_in"],
                    check_out=watch.params["check_out"],
                    adults=watch.params.get("adults", 2),
                    currency=watch.currency,
                )
                best = extract_best_hotel(raw, prefer_name=watch.params.get("hotel") or None)
        else:
            logger.warning("travels: unknown watch kind: %s", watch.kind)
            return
    except SerpAPIError as e:
        logger.warning("serpapi error for watch %d: %s", watch.id, e)
        await _registrar_falha(session, bot, watch, f"SerpAPI: {e}")
        return
    except Exception as e:
        # QUALQUER outra falha (params malformados, payload inesperado) também
        # marca o dia como checado. Sem isto, _is_due seguia verdadeiro e o
        # watch quebrado era re-executado a cada tick (60s) até meia-noite,
        # queimando cota do SerpAPI todos os dias, em silêncio.
        logger.exception("watch %d: falha inesperada na checagem", watch.id)
        await _registrar_falha(session, bot, watch, f"{type(e).__name__}: {e}")
        return

    if watch.kind == "hotel" and best is not None:
        hotel_alvo = (watch.params.get("hotel") or "").strip()
        if hotel_alvo and not hotel_name_matches(hotel_alvo, best[1].get("name")):
            # Vigia de hotel NOMEADO cujo "melhor" é OUTRO hotel (o pedido está
            # esgotado/sem preço nessa data): pra esta vigia isso é "sem
            # preço", não um preço. Alertar "atingiu seu teto" e gravar
            # min_price_seen com a diária de outro hotel era o bug — a busca
            # interativa já trata isso (rótulo "Referência, o mais barato na
            # cidade"); a vigia não tinha o guard.
            logger.info(
                "travels: watch %d (hotel %r): melhor resultado é %r — "
                "sem preço do hotel pedido nesta checagem",
                watch.id, hotel_alvo, best[1].get("name") or "?",
            )
            best = None

    if best is None:
        # "Sem preço" NÃO é sucesso: uma vigia cujos params expiraram
        # (aeroporto, data que passou) devolve vazio TODO dia sem exceção. Se
        # isto zerasse o contador, "não consigo checar" viraria indistinguível
        # de "o preço não caiu" — o silêncio que a feature existe pra eliminar.
        # Conta como falha; 3 dias vazios seguidos avisam, e o 1º preço reseta.
        logger.info("travels: no price for watch %d", watch.id)
        await _registrar_falha(session, bot, watch,
                               "sem preço/resultado (parâmetros podem ter expirado)")
        return

    now = datetime.now(timezone.utc)
    watch.last_checked_at = now
    # Preço de fato = sucesso: zera contador E o marco de aviso, pra que uma
    # nova pane volte a avisar do zero.
    if watch.consecutive_failures:
        logger.info("travels: watch %d voltou a checar após %d falha(s)",
                    watch.id, watch.consecutive_failures)
        watch.consecutive_failures = 0
        watch.alerted_at_failures = 0
        watch.last_error = None

    price, payload = best
    snapshot = TravelPriceSnapshot(
        watch_id=watch.id, price=price, currency=watch.currency, raw=payload
    )
    session.add(snapshot)
    await session.flush()

    fire, reason = _should_alert(watch, price)
    watch.last_price = price
    if watch.min_price_seen is None or price < watch.min_price_seen:
        watch.min_price_seen = price

    if fire:
        headline = _headline(watch.kind, watch.summary or watch.kind, price, reason)
        details = (
            format_flight(price, payload, chosen_dep, chosen_ret, insights)
            if watch.kind == "flight"
            else format_hotel(price, payload, chosen_ci, chosen_co)
        )
        message = f"{headline}\n\n{details}"
        user = await session.get(User, watch.user_id)
        if user is not None:
            sent = await _send_with_fallback(bot, user.id, message)
            if sent:
                watch.last_alert_at = now
                session.add(
                    TravelAlert(
                        watch_id=watch.id, snapshot_id=snapshot.id,
                        price=price, reason=reason,
                    )
                )
    await session.commit()


async def run_travel_alerts(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
) -> None:
    """Roda 1x/dia (a partir de TRAVELS_ALERT_HOUR BRT) por watch ativo."""
    if settings.serpapi_key is None:
        return
    now_utc = datetime.now(timezone.utc)
    async with sessionmaker() as session:
        stmt = select(TravelWatch).where(TravelWatch.status == "active")
        all_active = list((await session.scalars(stmt)).all())

    due = [w for w in all_active if _is_due(w, now_utc)]
    if not due:
        return

    logger.info("travels tick: %d watch(es) due (of %d active)", len(due), len(all_active))
    api_key = settings.serpapi_key.get_secret_value()
    async with SerpAPIClient(api_key) as serpapi:
        for w in due:
            async with sessionmaker() as session:
                fresh = await session.get(TravelWatch, w.id)
                if fresh is None or fresh.status != "active":
                    continue
                try:
                    await check_watch(session, serpapi, bot, fresh)
                except Exception:
                    logger.exception("check_watch crashed for watch %d", w.id)


async def purge_old_travel_data(session: AsyncSession, days: int = 90) -> int:
    """Remove snapshots e alertas de viagem com mais de `days` dias.

    Cada checagem diária grava um TravelPriceSnapshot com o payload bruto
    (JSON gordo); sem limpeza, a tabela cresce indefinidamente no SQLite.
    Alertas saem antes dos snapshots por causa da FK alert→snapshot.
    Retorna o total de linhas removidas."""
    cut = datetime.now(timezone.utc) - timedelta(days=days)
    n_alerts = (await session.execute(
        delete(TravelAlert).where(TravelAlert.sent_at < cut)
    )).rowcount or 0
    n_snaps = (await session.execute(
        delete(TravelPriceSnapshot).where(TravelPriceSnapshot.captured_at < cut)
    )).rowcount or 0
    await session.commit()
    return n_alerts + n_snaps
