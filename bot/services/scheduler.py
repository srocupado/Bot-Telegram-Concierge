from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import settings
from bot.db.models import Reminder, User
from bot.services.congress import (
    USER_AGENT as CONGRESS_USER_AGENT,
    CongressScrapeError,
    fetch_week_mps,
    format_week_message,
)
from bot.services.reminders import due_reminders, mark_sent
from bot.services.traffic import (
    USER_AGENT as TRAFFIC_USER_AGENT,
    TrafficError,
    fetch_traffic,
    format_traffic_message,
    parse_route_waypoints,
)
from bot.services.traffic_baseline import (
    baseline_p50,
    purge_old_samples,
    record_sample,
    should_alert,
)
from bot.services.weather import (
    WeatherError,
    fetch_today_weather,
    format_weather_line,
)

logger = logging.getLogger(__name__)

BRT = ZoneInfo("America/Sao_Paulo")
CONGRESS_HOUR = 7


async def _send_html_with_fallback(bot: Bot, chat_id: int, text: str) -> bool:
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception:
        logger.exception("HTML send failed; retrying as plain text for chat %d", chat_id)
        try:
            await bot.send_message(
                chat_id, text, parse_mode=None, disable_web_page_preview=True
            )
            return True
        except Exception:
            logger.exception("failed to send message to chat %d", chat_id)
            return False


async def run_congress_digest(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
) -> None:
    if not settings.congress_digest_enabled:
        return
    now_brt = datetime.now(BRT)
    if now_brt.weekday() != 0:
        return

    monday_brt = datetime.combine(now_brt.date(), time(0, 0), tzinfo=BRT)
    monday_start_utc = monday_brt.astimezone(timezone.utc)

    async with sessionmaker() as session:
        stmt = select(User).where(
            User.congress_subscribed.is_(True),
            (User.last_congress_digest_at.is_(None))
            | (User.last_congress_digest_at < monday_start_utc),
        )
        candidates = list((await session.scalars(stmt)).all())

    def _due(u: User) -> bool:
        h = u.congress_hour if u.congress_hour is not None else CONGRESS_HOUR
        m = u.congress_minute if u.congress_minute is not None else 0
        return (now_brt.hour, now_brt.minute) >= (h, m)

    users = [u for u in candidates if _due(u)]

    if not users:
        return

    try:
        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": CONGRESS_USER_AGENT},
        ) as client:
            items = await fetch_week_mps(client, now_brt.date())
    except CongressScrapeError:
        logger.exception("congress scrape failed")
        return

    message = format_week_message(items, now_brt.date())
    logger.info("congress digest: %d inscritos, %d MPs encontradas", len(users), len(items))

    for u in users:
        sent = await _send_html_with_fallback(bot, u.id, message)
        if sent:
            async with sessionmaker() as session:
                fresh = await session.get(User, u.id)
                if fresh is not None:
                    fresh.last_congress_digest_at = datetime.now(timezone.utc)
                    await session.commit()
            logger.info("congress digest enviado a %d", u.id)


async def run_traffic_digest(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
) -> None:
    if not settings.traffic_digest_enabled:
        return
    if not (settings.home_coords and settings.work_coords and settings.google_maps_api_key):
        logger.warning(
            "traffic digest skipped: missing config (home_coords/work_coords/google_maps_api_key)"
        )
        return

    now_brt = datetime.now(BRT)
    if now_brt.weekday() > 4:
        return

    day_start_brt = datetime.combine(now_brt.date(), time(0, 0), tzinfo=BRT)
    day_start_utc = day_start_brt.astimezone(timezone.utc)

    async with sessionmaker() as session:
        stmt = select(User).where(
            User.traffic_subscribed.is_(True),
            (User.last_traffic_digest_at.is_(None))
            | (User.last_traffic_digest_at < day_start_utc),
        )
        candidates = list((await session.scalars(stmt)).all())

    def _due(u: User) -> bool:
        h = u.traffic_hour if u.traffic_hour is not None else settings.traffic_hour
        m = u.traffic_minute if u.traffic_minute is not None else settings.traffic_minute
        return (now_brt.hour, now_brt.minute) >= (h, m)

    users = [u for u in candidates if _due(u)]

    if not users:
        return

    api_key = settings.google_maps_api_key.get_secret_value()
    weather_line: str | None = None
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": TRAFFIC_USER_AGENT},
        ) as client:
            waypoints: list[str] = []
            if settings.route_google_maps_url:
                waypoints = await parse_route_waypoints(
                    client, settings.route_google_maps_url
                )
            traffic_task = fetch_traffic(
                client,
                api_key,
                settings.home_coords,
                settings.work_coords,
                waypoints,
                maps_url=settings.route_google_maps_url or "",
            )
            weather_task = fetch_today_weather(client, settings.home_coords)
            results = await asyncio.gather(
                traffic_task, weather_task, return_exceptions=True
            )
            traffic_result, weather_result = results
            if isinstance(traffic_result, BaseException):
                raise traffic_result
            infos = traffic_result
            info = infos[0]
            if isinstance(weather_result, WeatherError):
                logger.warning("weather fetch failed: %s", weather_result)
            elif isinstance(weather_result, BaseException):
                logger.exception(
                    "weather fetch crashed", exc_info=weather_result
                )
            else:
                weather_line = format_weather_line(weather_result)
    except TrafficError:
        logger.exception("traffic digest fetch failed")
        return

    message = format_traffic_message(info, "casa → trabalho")
    if weather_line:
        link_marker = "\n\n<a href="
        idx = message.rfind(link_marker)
        if idx >= 0:
            message = message[:idx] + f"\n\n{weather_line}" + message[idx:]
        else:
            message = f"{message}\n\n{weather_line}"
    logger.info(
        "traffic digest: %d inscritos, %d min via %s%s",
        len(users),
        info.duration_minutes,
        info.summary or "rota direta",
        " (com clima)" if weather_line else "",
    )

    for u in users:
        sent = await _send_html_with_fallback(bot, u.id, message)
        if sent:
            async with sessionmaker() as session:
                fresh = await session.get(User, u.id)
                if fresh is not None:
                    fresh.last_traffic_digest_at = datetime.now(timezone.utc)
                    await session.commit()
            logger.info("traffic digest enviado a %d", u.id)


async def run_reminders(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
) -> None:
    now_utc = datetime.now(timezone.utc)
    async with sessionmaker() as session:
        # Tirar todos os lembretes vencidos para todos os usuários autorizados.
        stmt = select(User).where(User.is_authorized.is_(True))
        users = list((await session.scalars(stmt)).all())
        for user in users:
            items: list[Reminder] = await due_reminders(session, user.id, now_utc)
            for rem in items:
                try:
                    await bot.send_message(
                        user.id,
                        f"🔔 *Lembrete*: {rem.text}",
                        parse_mode="Markdown",
                    )
                    await mark_sent(session, rem)
                    logger.info("reminder sent", extra={"user_id": user.id, "reminder_id": rem.id})
                except Exception:
                    logger.exception("reminder send failed", extra={"reminder_id": rem.id})
                    await session.rollback()


TRAFFIC_WATCH_INTERVAL_MIN = 10
TRAFFIC_WATCH_LEAD_HOURS = 2
TRAFFIC_WATCH_TAIL_MIN = 30
TRAFFIC_ALERT_COOLDOWN_MIN = 30


def _user_traffic_time(u: User) -> tuple[int, int]:
    h = u.traffic_hour if u.traffic_hour is not None else settings.traffic_hour
    m = u.traffic_minute if u.traffic_minute is not None else settings.traffic_minute
    return h, m


def _in_watch_window(now_brt: datetime, u: User) -> bool:
    h, m = _user_traffic_time(u)
    digest_dt = now_brt.replace(hour=h, minute=m, second=0, microsecond=0)
    start = digest_dt - timedelta(hours=TRAFFIC_WATCH_LEAD_HOURS)
    end = digest_dt + timedelta(minutes=TRAFFIC_WATCH_TAIL_MIN)
    return start <= now_brt <= end


async def run_traffic_watch(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
) -> None:
    """Coleta sample da rota home→work, compara com baseline e alerta."""
    if not (settings.home_coords and settings.work_coords and settings.google_maps_api_key):
        return
    now_brt = datetime.now(BRT)
    if now_brt.weekday() > 4:
        return
    # rate-limit por intervalo: só roda em minutos múltiplos do intervalo (com folga de 1)
    if now_brt.minute % TRAFFIC_WATCH_INTERVAL_MIN > 1:
        return

    async with sessionmaker() as session:
        stmt = select(User).where(
            User.traffic_subscribed.is_(True),
            User.traffic_alert_enabled.is_(True),
            User.is_authorized.is_(True),
        )
        candidates = list((await session.scalars(stmt)).all())

    users = [u for u in candidates if _in_watch_window(now_brt, u)]
    if not users:
        return

    api_key = settings.google_maps_api_key.get_secret_value()
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": TRAFFIC_USER_AGENT},
        ) as client:
            waypoints: list[str] = []
            if settings.route_google_maps_url:
                waypoints = await parse_route_waypoints(
                    client, settings.route_google_maps_url
                )
            infos = await fetch_traffic(
                client, api_key,
                settings.home_coords, settings.work_coords,
                waypoints, maps_url=settings.route_google_maps_url or "",
            )
    except TrafficError:
        logger.exception("traffic watch fetch failed")
        return

    info = infos[0]
    current_s = info.duration_minutes * 60
    weekday, hour = now_brt.weekday(), now_brt.hour
    cooldown_cut = datetime.now(timezone.utc) - timedelta(minutes=TRAFFIC_ALERT_COOLDOWN_MIN)

    for u in users:
        async with sessionmaker() as session:
            await record_sample(session, u.id, weekday, hour, current_s)
            base = await baseline_p50(session, u.id, weekday, hour)
        if base is None:
            continue
        if not should_alert(current_s, base):
            continue
        if u.last_traffic_alert_at and u.last_traffic_alert_at >= cooldown_cut:
            continue

        delta_pct = round((current_s / base - 1) * 100)
        text = (
            f"🚨 <b>Trânsito anormal na sua rota</b>\n\n"
            f"⏱️ ~{info.duration_minutes} min agora (mediana de hoje: "
            f"~{round(base/60)} min, +{delta_pct}%).\n"
            f"📏 {info.distance_km} km via {info.summary or 'rota direta'}.\n"
        )
        if info.maps_url:
            text += f'\n<a href="{info.maps_url}">abrir no Google Maps</a>'

        sent = await _send_html_with_fallback(bot, u.id, text)
        if sent:
            async with sessionmaker() as session:
                fresh = await session.get(User, u.id)
                if fresh is not None:
                    fresh.last_traffic_alert_at = datetime.now(timezone.utc)
                    await session.commit()
            logger.info(
                "traffic alert sent to %d: %d min (baseline %d, +%d%%)",
                u.id, info.duration_minutes, round(base / 60), delta_pct,
            )


async def run_purge(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    now_brt = datetime.now(BRT)
    # Roda só uma vez por dia, às 03:00 BRT.
    if now_brt.hour != 3 or now_brt.minute > 1:
        return
    async with sessionmaker() as session:
        n = await purge_old_samples(session)
        if n:
            logger.info("purged %d old traffic_samples", n)


async def tick(
    sessionmaker: async_sessionmaker[AsyncSession],
    bot: Bot,
) -> None:
    try:
        await run_congress_digest(sessionmaker, bot)
    except Exception:
        logger.exception("congress digest crashed")

    try:
        await run_traffic_digest(sessionmaker, bot)
    except Exception:
        logger.exception("traffic digest crashed")

    try:
        await run_traffic_watch(sessionmaker, bot)
    except Exception:
        logger.exception("traffic watch crashed")

    try:
        await run_reminders(sessionmaker, bot)
    except Exception:
        logger.exception("reminders dispatch crashed")

    try:
        await run_purge(sessionmaker)
    except Exception:
        logger.exception("purge crashed")


async def scheduler_loop(
    bot: Bot,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    logger.info("scheduler started; tick=%ds", settings.scheduler_tick_seconds)
    while True:
        try:
            await tick(sessionmaker, bot)
        except Exception:
            logger.exception("scheduler tick crashed")
        await asyncio.sleep(settings.scheduler_tick_seconds)
