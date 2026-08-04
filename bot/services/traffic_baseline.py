from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import TrafficSample

logger = logging.getLogger(__name__)

# 28 dias = 4 ocorrências do MESMO dia-da-semana (a retenção guarda 30).
# Era 7: combinado com o filtro por weekday, a janela só enxergava as
# amostras de HOJE — a "mediana habitual" era a mediana da hora corrente, e
# um engarrafamento em curso virava o próprio normal (nunca +30%). O alerta
# nunca disparava, em silêncio.
BASELINE_WINDOW_DAYS = 28
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
    session: AsyncSession, user_id: int, weekday: int, hour: int,
    *, antes_de: datetime | None = None,
) -> int | None:
    """p50 (mediana) de duration_seconds nos últimos BASELINE_WINDOW_DAYS
    para o mesmo (weekday, hour). None se tiver menos de MIN_SAMPLES.

    `antes_de` (UTC): exclui amostras a partir desse instante — o caller
    passa a MEIA-NOITE de hoje pra tirar o DIA CORRENTE da mediana. Sem
    isso, as amostras do próprio engarrafamento em curso entravam no
    "habitual" e o puxavam pra cima justamente durante o evento que deveria
    alertar (30, 32, 48, 52, 55 min → mediana 48 → 55 nunca é +30%)."""
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
    if antes_de is not None:
        stmt = stmt.where(TrafficSample.sampled_at < antes_de)
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
