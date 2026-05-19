from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TypedDict


class ChatMessage(TypedDict):
    role: str  # "user" | "assistant" | "system"
    content: str


class LLMProvider(ABC):
    name: str

    @abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        ...

    async def ping(self) -> str:
        return await self.chat(
            [{"role": "user", "content": "Responda apenas com 'pong' (sem aspas)."}],
            max_tokens=8,
        )
