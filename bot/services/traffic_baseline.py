from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import TrafficSample

logger = logging.getLogger(__name__)

BASELINE_WINDOW_DAYS = 7
MIN_SAMPLES = 5
ALERT_RATIO = 1.30
ALERT_FLOOR_MIN = 30
RETENTION_DAYS = 30


async def record_sample(
    session: AsyncSession,
    user_id: int,
    weekday: int,
    hour: int,
    duration_seconds: int,
) -> None:
    sample = TrafficSample(
        user_id=user_id,
        weekday=weekday,
        hour=hour,
        duration_seconds=duration_seconds,
    )
    session.add(sample)
    await session.commit()


async def baseline_p50(
    session: AsyncSession, user_id: int, weekday: int, hour: int
) -> int | None:
    """p50 (mediana) de duration_seconds nos últimos BASELINE_WINDOW_DAYS
    para o mesmo (weekday, hour). None se tiver menos de MIN_SAMPLES."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=BASELINE_WINDOW_DAYS)
    stmt = (
        select(TrafficSample.duration_seconds)
        .where(
            TrafficSample.user_id == user_id,
            TrafficSample.weekday == weekday,
            TrafficSample.hour == hour,
            TrafficSample.sampled_at >= cutoff,
        )
        .order_by(TrafficSample.duration_seconds)
    )
    rows = list((await session.scalars(stmt)).all())
    if len(rows) < MIN_SAMPLES:
        return None
    return rows[len(rows) // 2]


def should_alert(current_seconds: int, baseline_seconds: int) -> bool:
    if current_seconds < ALERT_FLOOR_MIN * 60:
        return False
    return current_seconds >= baseline_seconds * ALERT_RATIO


async def purge_old_samples(session: AsyncSession) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    result = await session.execute(
        delete(TrafficSample).where(TrafficSample.sampled_at < cutoff)
    )
    await session.commit()
    return result.rowcount or 0
