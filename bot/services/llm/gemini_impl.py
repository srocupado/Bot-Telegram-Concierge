from __future__ import annotations

import asyncio
import logging
from typing import Any

import google.generativeai as genai

from bot.services.llm.base import ChatMessage, LLMProvider, Tool, ToolContext

logger = logging.getLogger(__name__)


def _to_gemini_parts(content: Any) -> list[Any]:
    """Converte content (str ou list[block]) pra lista de parts Gemini.

    Cada part é um dict {"text": ...} ou {"inline_data": {"mime_type", "data"}}.
    """
    if isinstance(content, str):
        return [{"text": content}]
    parts: list[Any] = []
    for b in content:
        bt = b.get("type")
        if bt == "text":
            parts.append({"text": b.get("text", "")})
        elif bt == "image":
            parts.append({
                "inline_data": {
                    "mime_type": b.get("media_type", "image/jpeg"),
                    "data": b.get("data", ""),
                }
            })
        else:
            parts.append(b)
    return parts


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY ausente")
        genai.configure(api_key=api_key)
        self.model_name = model

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        # Gemini: histórico estilo {role: "user"|"model", parts: [...]}.
        history: list[dict] = []
        last_user_parts: list[Any] | None = None
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            parts = _to_gemini_parts(m["content"])
            if m is messages[-1] and m["role"] == "user":
                last_user_parts = parts
                break
            history.append({"role": role, "parts": parts})
        if last_user_parts is None and messages:
            last_user_parts = _to_gemini_parts(messages[-1]["content"])

        def _call() -> str:
            model = genai.GenerativeModel(
                self.model_name,
                system_instruction=system,
                generation_config={"max_output_tokens": max_tokens},
            )
            chat = model.start_chat(history=history)
            resp = chat.send_message(last_user_parts or [{"text": ""}])
            return (resp.text or "").strip()

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
        history: list[dict] = []
        last_user_parts: list[Any] | None = None
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            parts = _to_gemini_parts(m["content"])
            if m is messages[-1] and m["role"] == "user":
                last_user_parts = parts
                break
            history.append({"role": role, "parts": parts})
        if last_user_parts is None and messages:
            last_user_parts = _to_gemini_parts(messages[-1]["content"])

        # Gemini FunctionDeclaration espera schema "OpenAPI-like" — JSON schema funciona.
        function_declarations = [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in tools
        ]
        # google-generativeai 0.8.x não aceita combinar function_declarations
        # com google_search no formato dict — quebra com "Unknown field for
        # FunctionDeclaration: google_search". Web search via Gemini fica
        # indisponível por ora; quem precisa de busca usa /provider anthropic.
        gemini_tools = [{"function_declarations": function_declarations}]
        tool_by_name = {t.name: t for t in tools}

        model = genai.GenerativeModel(
            self.model_name,
            system_instruction=system,
            tools=gemini_tools,
            generation_config={"max_output_tokens": max_tokens},
        )
        chat = model.start_chat(history=history)

        def _send(payload):
            return chat.send_message(payload)

        pending: Any = last_user_parts or [{"text": ""}]
        for _ in range(max_iterations):
            resp = await asyncio.to_thread(_send, pending)
            fcs: list = []
            for cand in resp.candidates or []:
                for part in (cand.content.parts if cand.content else []):
                    fc = getattr(part, "function_call", None)
                    if fc and fc.name:
                        fcs.append(fc)

            if not fcs:
                try:
                    return (resp.text or "").strip()
                except Exception:
                    return ""

            # Executa cada function_call e monta function_response parts.
            response_parts = []
            for fc in fcs:
                args = dict(fc.args) if fc.args else {}
                tool = tool_by_name.get(fc.name)
                if tool is None:
                    result = f"erro: tool '{fc.name}' não existe"
                else:
                    try:
                        result = await tool.handler(args, ctx)
                    except Exception as e:
                        logger.exception("tool %s failed", fc.name)
                        result = f"erro: {e}"
                response_parts.append({
                    "function_response": {
                        "name": fc.name,
                        "response": {"result": result},
                    }
                })
            pending = response_parts

        return "(limite de iterações de tool use atingido)"
