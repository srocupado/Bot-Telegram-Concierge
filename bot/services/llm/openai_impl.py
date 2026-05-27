from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from openai import OpenAI

from bot.services.llm.base import ChatMessage, LLMProvider, Tool, ToolContext

logger = logging.getLogger(__name__)


def _to_openai_content(content: Any) -> Any:
    """Converte content (str ou list[block]) pro formato OpenAI chat.completions."""
    if isinstance(content, str):
        return content
    out: list[dict] = []
    for b in content:
        bt = b.get("type")
        if bt == "text":
            out.append({"type": "text", "text": b.get("text", "")})
        elif bt == "image":
            data_url = f"data:{b.get('media_type', 'image/jpeg')};base64,{b.get('data', '')}"
            out.append({"type": "image_url", "image_url": {"url": data_url}})
        elif bt == "document":
            # chat.completions não aceita PDFs direto. Sinaliza pro LLM.
            out.append({
                "type": "text",
                "text": "[PDF anexado — OpenAI não suporta leitura de PDF nessa versão; use /provider_visao anthropic ou gemini]",
            })
        else:
            out.append(b)
    return out


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
        oa_messages.extend(
            {"role": m["role"], "content": _to_openai_content(m["content"])} for m in messages
        )

        def _call() -> str:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=oa_messages,
                max_tokens=max_tokens,
            )
            return (resp.choices[0].message.content or "").strip()

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
        oa_messages: list[dict] = []
        if system:
            oa_messages.append({"role": "system", "content": system})
        oa_messages.extend(
            {"role": m["role"], "content": _to_openai_content(m["content"])} for m in messages
        )

        tools_spec = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]
        tool_by_name = {t.name: t for t in tools}

        for _ in range(max_iterations):
            def _call():
                return self.client.chat.completions.create(
                    model=self.model,
                    messages=oa_messages,
                    max_tokens=max_tokens,
                    tools=tools_spec,
                )

            resp = await asyncio.to_thread(_call)
            choice = resp.choices[0]
            msg = choice.message

            if not msg.tool_calls:
                return (msg.content or "").strip()

            # Anexa a mensagem do assistant (com tool_calls) e cada resultado.
            oa_messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
            })
            for tc in msg.tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    fn_args = {}
                tool = tool_by_name.get(fn_name)
                if tool is None:
                    result = f"erro: tool '{fn_name}' não existe"
                else:
                    try:
                        result = await tool.handler(fn_args, ctx)
                    except Exception as e:
                        logger.exception("tool %s failed", fn_name)
                        result = f"erro: {e}"
                oa_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

            if ctx.short_circuit:
                return ""

        return "(limite de iterações de tool use atingido)"
