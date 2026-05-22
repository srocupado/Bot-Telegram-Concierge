"""CRUD de fatos persistentes do usuário (memória entre sessões)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import UserFact


async def upsert_fact(session: AsyncSession, user_id: int, key: str, value: str) -> UserFact:
    key = key.strip().lower()
    result = await session.execute(
        select(UserFact).where(UserFact.user_id == user_id, UserFact.key == key)
    )
    fact = result.scalar_one_or_none()
    if fact is None:
        fact = UserFact(user_id=user_id, key=key, value=value)
        session.add(fact)
    else:
        fact.value = value
    await session.commit()
    await session.refresh(fact)
    return fact


async def get_fact(session: AsyncSession, user_id: int, key: str) -> UserFact | None:
    key = key.strip().lower()
    result = await session.execute(
        select(UserFact).where(UserFact.user_id == user_id, UserFact.key == key)
    )
    return result.scalar_one_or_none()


async def list_facts(session: AsyncSession, user_id: int) -> list[UserFact]:
    result = await session.execute(
        select(UserFact).where(UserFact.user_id == user_id).order_by(UserFact.key)
    )
    return list(result.scalars().all())


async def delete_fact(session: AsyncSession, user_id: int, key: str) -> bool:
    fact = await get_fact(session, user_id, key)
    if fact is None:
        return False
    await session.delete(fact)
    await session.commit()
    return True
