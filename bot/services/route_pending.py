from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class PendingRoute:
    label: str                     # "casa", "trabalho", ou texto cru
    raw_query: str                 # texto original do usuário
    resolved_coords: str | None    # "lat,lng" se atalho conhecido; None → geocodar depois
    expires_at: datetime
    kind: str = "rota"             # "rota" | "melhor_horario"
    arrive_by_raw: str | None = None  # só p/ melhor_horario: horário-alvo cru ("9h")


class PendingRoutes:
    """Estado in-memory de /rota aguardando localização do usuário.

    Não persiste. TTL curto (default 2min). Operações são atômicas em
    Python (dict assignment), suficiente pra um único event loop.
    """

    def __init__(self, ttl: timedelta = timedelta(minutes=2)) -> None:
        self._store: dict[int, PendingRoute] = {}
        self._ttl = ttl

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def put(self, user_id: int, label: str, raw_query: str,
            resolved_coords: str | None, *, kind: str = "rota",
            arrive_by_raw: str | None = None) -> PendingRoute:
        pending = PendingRoute(
            label=label,
            raw_query=raw_query,
            resolved_coords=resolved_coords,
            expires_at=self._now() + self._ttl,
            kind=kind,
            arrive_by_raw=arrive_by_raw,
        )
        self._store[user_id] = pending
        return pending

    def pop(self, user_id: int) -> PendingRoute | None:
        pending = self._store.pop(user_id, None)
        if pending is None:
            return None
        if pending.expires_at < self._now():
            return None
        return pending

    def discard(self, user_id: int) -> None:
        self._store.pop(user_id, None)


pending_routes = PendingRoutes()
