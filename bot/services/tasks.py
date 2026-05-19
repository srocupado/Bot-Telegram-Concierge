from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Task


async def create_task(session: AsyncSession, user_id: int, text: str) -> Task:
    task = Task(user_id=user_id, text=text, done=False)
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def list_open_tasks(session: AsyncSession, user_id: int) -> list[Task]:
    result = await session.execute(
        select(Task).where(Task.user_id == user_id, Task.done.is_(False)).order_by(Task.created_at)
    )
    return list(result.scalars().all())


async def mark_done(session: AsyncSession, user_id: int, task_id: int) -> Task | None:
    task = await session.get(Task, task_id)
    if task is None or task.user_id != user_id:
        return None
    if task.done:
        return task
    task.done = True
    task.done_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(task)
    return task
