from __future__ import annotations

import asyncio

from openai import OpenAI

from bot.services.llm.base import ChatMessage, LLMProvider


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY ausente")
        self.client = OpenAI(api_key=api_key)
        self.model = model

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        oa_messages: list[dict] = []
        if system:
            oa_messages.append({"role": "system", "content": system})
        oa_messages.extend({"role": m["role"], "content": m["content"]} for m in messages)

        def _call() -> str:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=oa_messages,
                max_tokens=max_tokens,
            )
            return (resp.choices[0].message.content or "").strip()

        return await asyncio.to_thread(_call)
