"""CRUD de TravelWatch + verificação diária + envio de alertas.

Equivalente ao `bot/services/scheduler.py::check_watch` do Telegram-Travels,
adaptado pra reusar `SessionLocal`/`settings` do Concierge e rodar no
`tick()` do scheduler existente — sem loop próprio.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import settings
from bot.db.models import TravelAlert, TravelPriceSnapshot, TravelWatch, User
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
)

logger = logging.getLogger(__name__)

BRT = ZoneInfo("America/Sao_Paulo")


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_due(watch: TravelWatch, now_utc: datetime) -> bool:
    now_brt = now_utc.astimezone(BRT)
    if now_brt.hour < settings.travels_alert_hour:
        return False
    last_checked = _as_utc(watch.last_checked_at)
    if last_checked is None:
        return True
    last_brt = last_checked.astimezone(BRT)
    return last_brt.date() < now_brt.date()


def _should_alert(
    watch: TravelWatch, new_price: float
) -> tuple[bool, str]:
    now = datetime.now(timezone.utc)
    snooze_until = _as_utc(watch.snooze_until)
    if snooze_until and snooze_until > now:
        return False, "snoozed"
    if watch.max_price is not None:
        if new_price <= watch.max_price:
            return True, "below_max"
        return False, "above_max"
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
                best = extract_best_hotel(raw)
        else:
            logger.warning("travels: unknown watch kind: %s", watch.kind)
            return
    except SerpAPIError as e:
        logger.warning("serpapi error for watch %d: %s", watch.id, e)
        watch.last_checked_at = datetime.now(timezone.utc)
        await session.commit()
        return

    now = datetime.now(timezone.utc)
    watch.last_checked_at = now

    if best is None:
        logger.info("travels: no price for watch %d", watch.id)
        await session.commit()
        return

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
