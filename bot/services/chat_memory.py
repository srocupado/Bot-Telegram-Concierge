from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from bot.services.llm.base import ChatMessage

# Hooks de persistência (ligados por services/memoria.py no startup):
#   on_append(chat_id, role, content)  → grava write-through no SQL;
#   on_evict(chat_id, mensagens)       → compacta no resumo rolante.
# Sem hooks ligados o comportamento é o antigo (RAM pura) — testes e
# import isolado continuam funcionando.
AppendHook = Callable[[int, str, str], None]
EvictHook = Callable[[int, list[ChatMessage]], None]


@dataclass
class _Entry:
    last_seen: datetime
    history: deque[ChatMessage]


class ChatMemory:
    """Memória de contexto in-memory por chat_id, com TTL e limite de turnos.

    Camada quente (RAM). A durabilidade fica nos hooks: cada append é
    persistido em chat_log e, quando mensagens saem do contexto (overflow
    em lote ou TTL), viram resumo rolante em chat_summaries.
    """

    def __init__(self, ttl: timedelta = timedelta(minutes=30), max_turns: int = 10) -> None:
        self._store: dict[int, _Entry] = {}
        self.ttl = ttl
        self._max_msgs = max_turns * 2
        self._on_append: AppendHook | None = None
        self._on_evict: EvictHook | None = None

    def set_hooks(self, on_append: AppendHook | None, on_evict: EvictHook | None) -> None:
        self._on_append = on_append
        self._on_evict = on_evict

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _emit_evict(self, chat_id: int, msgs: list[ChatMessage]) -> None:
        if self._on_evict is not None and msgs:
            self._on_evict(chat_id, msgs)

    def _purge_if_expired(self, chat_id: int) -> None:
        entry = self._store.get(chat_id)
        if entry is None:
            return
        if self._now() - entry.last_seen > self.ttl:
            # Conversa morreu de velha: compacta o que sobrou antes de soltar.
            self._emit_evict(chat_id, list(entry.history))
            del self._store[chat_id]

    def get(self, chat_id: int) -> list[ChatMessage]:
        self._purge_if_expired(chat_id)
        entry = self._store.get(chat_id)
        return list(entry.history) if entry else []

    def append(self, chat_id: int, role: str, content: str) -> None:
        self._purge_if_expired(chat_id)
        entry = self._store.get(chat_id)
        if entry is None:
            entry = _Entry(last_seen=self._now(), history=deque())
            self._store[chat_id] = entry
        entry.history.append({"role": role, "content": content})
        entry.last_seen = self._now()
        if self._on_append is not None and isinstance(content, str):
            self._on_append(chat_id, role, content)
        # Overflow: tira a metade mais antiga EM LOTE (1 compactação por
        # ~max_turns/2 turnos, em vez de 1 chamada de LLM por mensagem).
        if len(entry.history) > self._max_msgs:
            evicted = [entry.history.popleft() for _ in range(self._max_msgs // 2)]
            self._emit_evict(chat_id, evicted)

    def seed(self, chat_id: int, msgs: list[ChatMessage], last_seen: datetime) -> None:
        """Re-hidratação pós-restart (não dispara hooks — já está no SQL)."""
        if not msgs:
            return
        self._store[chat_id] = _Entry(
            last_seen=last_seen, history=deque(msgs[-self._max_msgs:]),
        )

    def reset(self, chat_id: int) -> None:
        # Descarte explícito (/reset): sem compactar — o usuário pediu
        # pra esquecer o contexto atual.
        self._store.pop(chat_id, None)


memory = ChatMemory()
