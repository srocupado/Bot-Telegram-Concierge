"""Lista de compras persistente por usuário.

Modelo simples: cada item tem texto + quantidade opcional + flag checked.
Itens marcados como checked permanecem na lista até o usuário pedir pra
limpar. Sem categorias/listas múltiplas nessa v1 — uma lista por usuário.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import ShoppingItem

logger = logging.getLogger(__name__)


async def add_item(
    session: AsyncSession, user_id: int, text: str, quantity: str | None = None,
) -> ShoppingItem:
    item = ShoppingItem(
        user_id=user_id,
        text=text.strip(),
        quantity=(quantity or None) and quantity.strip(),
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def list_items(
    session: AsyncSession, user_id: int, only_pending: bool = True,
) -> list[ShoppingItem]:
    stmt = select(ShoppingItem).where(ShoppingItem.user_id == user_id)
    if only_pending:
        stmt = stmt.where(ShoppingItem.checked.is_(False))
    stmt = stmt.order_by(ShoppingItem.checked, ShoppingItem.id)
    return list((await session.scalars(stmt)).all())


def _normalize(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


async def find_by_text(
    session: AsyncSession, user_id: int, query: str, include_checked: bool = False,
) -> list[ShoppingItem]:
    """Busca por substring case-insensitive no campo text. Usado pra
    marcar/remover por nome em vez de id."""
    q = _normalize(query)
    if not q:
        return []
    items = await list_items(session, user_id, only_pending=not include_checked)
    return [it for it in items if q in _normalize(it.text)]


async def mark_checked(
    session: AsyncSession, user_id: int, item_id: int, checked: bool = True,
) -> ShoppingItem | None:
    item = await session.get(ShoppingItem, item_id)
    if item is None or item.user_id != user_id:
        return None
    item.checked = checked
    item.checked_at = datetime.now(timezone.utc) if checked else None
    await session.commit()
    await session.refresh(item)
    return item


async def remove_item(
    session: AsyncSession, user_id: int, item_id: int,
) -> ShoppingItem | None:
    item = await session.get(ShoppingItem, item_id)
    if item is None or item.user_id != user_id:
        return None
    await session.delete(item)
    await session.commit()
    return item


async def clear_checked(session: AsyncSession, user_id: int) -> int:
    """Remove todos os itens já marcados como comprados. Retorna a
    quantidade removida."""
    result = await session.execute(
        delete(ShoppingItem).where(
            ShoppingItem.user_id == user_id,
            ShoppingItem.checked.is_(True),
        )
    )
    await session.commit()
    return result.rowcount or 0


async def clear_all(session: AsyncSession, user_id: int) -> int:
    """Remove TODOS os itens da lista (zera). Retorna quantidade removida."""
    result = await session.execute(
        delete(ShoppingItem).where(ShoppingItem.user_id == user_id)
    )
    await session.commit()
    return result.rowcount or 0


def format_item(item: ShoppingItem) -> str:
    box = "☑️" if item.checked else "⬜"
    qty = f" {item.quantity}" if item.quantity else ""
    return f"{box} #{item.id} {item.text}{qty}"
