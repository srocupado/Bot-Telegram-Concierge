from __future__ import annotations

import asyncio
import logging

import anthropic

from bot.services.llm.base import ChatMessage, LLMProvider, Tool, ToolContext

logger = logging.getLogger(__name__)


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

    async def chat_with_tools(
        self,
        messages: list[ChatMessage],
        tools: list[Tool],
        ctx: ToolContext,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        max_iterations: int = 5,
    ) -> str:
        anth_messages: list[dict] = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m["role"] in ("user", "assistant")
        ]
        tools_spec = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]
        tool_by_name = {t.name: t for t in tools}

        for _ in range(max_iterations):
            def _call() -> anthropic.types.Message:
                kwargs: dict = {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "messages": anth_messages,
                    "tools": tools_spec,
                }
                if system:
                    kwargs["system"] = system
                return self.client.messages.create(**kwargs)

            resp = await asyncio.to_thread(_call)

            if resp.stop_reason != "tool_use":
                parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
                return "".join(parts).strip()

            # Anexa a resposta do assistant (com blocos tool_use) e executa cada tool.
            anth_messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
            tool_results: list[dict] = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                tool = tool_by_name.get(block.name)
                if tool is None:
                    result = f"erro: tool '{block.name}' não existe"
                else:
                    try:
                        result = await tool.handler(block.input or {}, ctx)
                    except Exception as e:
                        logger.exception("tool %s failed", block.name)
                        result = f"erro: {e}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })
            anth_messages.append({"role": "user", "content": tool_results})

        return "(limite de iterações de tool use atingido)"
