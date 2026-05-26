from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from bot.config import settings
from bot.db.base import Base

logger = logging.getLogger(__name__)


def _ensure_sqlite_dir(url: str) -> None:
    """Cria o diretório do arquivo SQLite se necessário.

    Suporta URLs como:
      sqlite+aiosqlite:////app/data/concierge.db   (absoluto)
      sqlite+aiosqlite:///./data/concierge.db      (relativo)
    """
    if not url.startswith("sqlite"):
        return
    _, _, raw = url.partition("://")
    # raw = "/app/data/concierge.db" (absoluto, vinha de 4 slashes)
    # raw = "./data/concierge.db" (relativo, vinha de 3 slashes)
    db_path = Path(raw)
    parent = db_path.parent
    if parent and str(parent) not in ("", ".") and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
        logger.info("created sqlite data dir: %s", parent)


_ensure_sqlite_dir(settings.database_url)

engine: AsyncEngine = create_async_engine(settings.database_url, echo=False, future=True)
SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def _ensure_columns(conn) -> None:
    """ALTER TABLE para colunas novas adicionadas após o create_all inicial.

    SQLite não tem `ADD COLUMN IF NOT EXISTS`, então checamos via PRAGMA.
    """
    user_cols = await conn.exec_driver_sql("PRAGMA table_info(users)")
    cols = {row[1] for row in user_cols.fetchall()}
    if "traffic_alert_enabled" not in cols:
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN traffic_alert_enabled BOOLEAN NOT NULL DEFAULT 1"
        )
        logger.info("migrated: added users.traffic_alert_enabled")
    if "last_traffic_alert_at" not in cols:
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN last_traffic_alert_at DATETIME"
        )
        logger.info("migrated: added users.last_traffic_alert_at")
    if "vision_provider" not in cols:
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN vision_provider VARCHAR(32)"
        )
        logger.info("migrated: added users.vision_provider")
    if "firebase_uid" not in cols:
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN firebase_uid VARCHAR(64)"
        )
        logger.info("migrated: added users.firebase_uid")
    if "awaiting_firebase_json_until" not in cols:
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN awaiting_firebase_json_until DATETIME"
        )
        logger.info("migrated: added users.awaiting_firebase_json_until")
    if "dou_mp_subscribed" not in cols:
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN dou_mp_subscribed BOOLEAN NOT NULL DEFAULT 0"
        )
        logger.info("migrated: added users.dou_mp_subscribed")
    if "proactive_enabled" not in cols:
        await conn.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN proactive_enabled BOOLEAN NOT NULL DEFAULT 0"
        )
        logger.info("migrated: added users.proactive_enabled")

    rem_cols = await conn.exec_driver_sql("PRAGMA table_info(reminders)")
    cols = {row[1] for row in rem_cols.fetchall()}
    if "command_kind" not in cols:
        await conn.exec_driver_sql(
            "ALTER TABLE reminders ADD COLUMN command_kind VARCHAR(32)"
        )
        logger.info("migrated: added reminders.command_kind")
    if "command_args" not in cols:
        await conn.exec_driver_sql(
            "ALTER TABLE reminders ADD COLUMN command_args VARCHAR(2048)"
        )
        logger.info("migrated: added reminders.command_args")
    if "recurrence" not in cols:
        await conn.exec_driver_sql(
            "ALTER TABLE reminders ADD COLUMN recurrence VARCHAR(64)"
        )
        logger.info("migrated: added reminders.recurrence")


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_columns(conn)
    logger.info("database initialized")
