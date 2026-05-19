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


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("database initialized")
