"""Normalização de datetime do banco.

O SQLite devolve campos DateTime NAIVE mesmo quando declarados como
DateTime(timezone=True) nos models — sem normalizar, comparações ou
aritmética com outro datetime aware (ex.: `datetime.now(timezone.utc)`)
lançam TypeError em runtime.
"""
from __future__ import annotations

from datetime import datetime, timezone


def as_utc(dt: datetime | None) -> datetime | None:
    """Garante que `dt` seja aware-UTC. Naive vira UTC, None passa direto.

    Use em TODA leitura de campo datetime do ORM antes de comparar ou
    subtrair com outro datetime aware.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
