from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bot.services.llm.base import ChatMessage


@dataclass
class _Entry:
    last_seen: datetime
    history: deque[ChatMessage]


class ChatMemory:
    """Memória de contexto in-memory por chat_id, com TTL e limite de turnos."""

    def __init__(self, ttl: timedelta = timedelta(minutes=30), max_turns: int = 10) -> None:
        self._store: dict[int, _Entry] = {}
        self._ttl = ttl
        self._max_msgs = max_turns * 2

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _purge_if_expired(self, chat_id: int) -> None:
        entry = self._store.get(chat_id)
        if entry is None:
            return
        if self._now() - entry.last_seen > self._ttl:
            del self._store[chat_id]

    def get(self, chat_id: int) -> list[ChatMessage]:
        self._purge_if_expired(chat_id)
        entry = self._store.get(chat_id)
        return list(entry.history) if entry else []

    def append(self, chat_id: int, role: str, content: str) -> None:
        self._purge_if_expired(chat_id)
        entry = self._store.get(chat_id)
        if entry is None:
            entry = _Entry(last_seen=self._now(), history=deque(maxlen=self._max_msgs))
            self._store[chat_id] = entry
        entry.history.append({"role": role, "content": content})
        entry.last_seen = self._now()

    def reset(self, chat_id: int) -> None:
        self._store.pop(chat_id, None)


memory = ChatMemory()
