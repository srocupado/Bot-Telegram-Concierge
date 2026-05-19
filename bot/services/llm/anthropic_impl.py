from __future__ import annotations

import asyncio

import anthropic

from bot.services.llm.base import ChatMessage, LLMProvider


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY ausente")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        anth_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m["role"] in ("user", "assistant")
        ]

        def _call() -> str:
            kwargs: dict = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": anth_messages,
            }
            if system:
                kwargs["system"] = system
            resp = self.client.messages.create(**kwargs)
            parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
            return "".join(parts).strip()

        return await asyncio.to_thread(_call)
