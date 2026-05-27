"""Gemini provider via SDK `google-genai` 1.x.

Diferente do `google-generativeai` (0.x), o `google-genai` (1.x) suporta
combinar `function_declarations` (tool use customizado) com `google_search`
(busca web nativa) na mesma chamada — o que destrava web search no Gemini.

Voice STT continua usando `google-generativeai` 0.x (ver bot/services/voice.py)
porque a interface multimodal pra áudio ainda é mais simples lá.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from google import genai
from google.genai import types

from bot.services.llm.base import ChatMessage, LLMProvider, Tool, ToolContext

logger = logging.getLogger(__name__)

# Modelos Gemini 2.5 "pensam" por padrão e o thinking consome max_output_tokens.
# Garantimos um teto alto o bastante pra sobrar tokens pro texto final (senão a
# resposta vem vazia — "(sem resposta)"). É só um teto; respostas curtas não gastam tudo.
_MIN_OUTPUT_TOKENS = 8192


def _thinking_config(model: str):
    """ThinkingConfig conforme settings.gemini_thinking_budget. None = automático
    (sem alteração); -1 idem; 0 desliga; N fixa. O pro não permite desligar
    (mín ~128), então clampa pra 128 quando o budget global for 0 — evita 400."""
    from bot.config import settings as _s
    budget = getattr(_s, "gemini_thinking_budget", -1)
    if budget is None or budget == -1:
        return None
    budget = int(budget)
    if "pro" in (model or "") and 0 <= budget < 128:
        budget = 128
    try:
        return types.ThinkingConfig(thinking_budget=budget)
    except Exception:
        return None


def _to_genai_parts(content: Any) -> list[types.Part]:
    """Converte content (str ou list[block]) pra lista de Part do google-genai."""
    if isinstance(content, str):
        return [types.Part.from_text(text=content)]
    parts: list[types.Part] = []
    for b in content:
        bt = b.get("type")
        if bt == "text":
            parts.append(types.Part.from_text(text=b.get("text", "")))
        elif bt in ("image", "document"):
            import base64 as _b64
            data_bytes = _b64.b64decode(b.get("data", ""))
            mime = b.get("media_type", "image/jpeg" if bt == "image" else "application/pdf")
            parts.append(types.Part.from_bytes(data=data_bytes, mime_type=mime))
    return parts


def _messages_to_contents(messages: list[ChatMessage]) -> list[types.Content]:
    """Converte messages do nosso formato pra list[Content] do google-genai."""
    contents: list[types.Content] = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        parts = _to_genai_parts(m["content"])
        contents.append(types.Content(role=role, parts=parts))
    return contents


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY ausente")
        self.client = genai.Client(api_key=api_key)
        self.model_name = model
        self.model = model  # alias p/ interface comum (ex.: /ping)

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        contents = _messages_to_contents(messages)

        def _call() -> str:
            config = types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max(max_tokens, _MIN_OUTPUT_TOKENS),
                thinking_config=_thinking_config(self.model_name),
            )
            resp = self.client.models.generate_content(
                model=self.model_name, contents=contents, config=config,
            )
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
        contents = _messages_to_contents(messages)
        function_declarations = [
            types.FunctionDeclaration(
                name=t.name,
                description=t.description,
                parameters=t.parameters,
            )
            for t in tools
        ]
        # IMPORTANTE: a API do Gemini recusa combinar google_search com
        # function_declarations no mesmo request ('Built-in tools and
        # Function Calling cannot be combined'). Como tool use é o uso
        # primário, mantemos só function_declarations aqui. Web search
        # nativa via Gemini fica indisponível — use /provider anthropic
        # quando precisar de busca.
        genai_tools = [
            types.Tool(function_declarations=function_declarations),
        ]
        tool_by_name = {t.name: t for t in tools}

        for _ in range(max_iterations):
            def _call() -> Any:
                config = types.GenerateContentConfig(
                    system_instruction=system,
                    tools=genai_tools,
                    max_output_tokens=max(max_tokens, _MIN_OUTPUT_TOKENS),
                    thinking_config=_thinking_config(self.model_name),
                )
                return self.client.models.generate_content(
                    model=self.model_name, contents=contents, config=config,
                )

            resp = await asyncio.to_thread(_call)

            # Extrai function_calls de qualquer parte da resposta.
            fcs: list[Any] = []
            model_parts: list[types.Part] = []
            for cand in resp.candidates or []:
                for part in (cand.content.parts if cand.content else []) or []:
                    model_parts.append(part)
                    fc = getattr(part, "function_call", None)
                    if fc and fc.name:
                        fcs.append(fc)

            if not fcs:
                # Tenta texto da resposta.
                try:
                    text = (resp.text or "").strip()
                    if text:
                        return text
                except Exception as e:
                    logger.warning("Gemini resp.text raised: %s", e)
                for cand in resp.candidates or []:
                    fr = getattr(cand, "finish_reason", None)
                    sr = getattr(cand, "safety_ratings", None)
                    logger.warning(
                        "Gemini candidate empty: finish_reason=%s safety_ratings=%s",
                        fr, sr,
                    )
                return ""

            # Adiciona resposta do model (com function_calls) e executa cada tool.
            contents.append(types.Content(role="model", parts=model_parts))

            response_parts: list[types.Part] = []
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
                response_parts.append(
                    types.Part.from_function_response(
                        name=fc.name, response={"result": result},
                    )
                )
            contents.append(types.Content(role="user", parts=response_parts))

            if ctx.short_circuit:
                return ""

        return "(limite de iterações de tool use atingido)"
