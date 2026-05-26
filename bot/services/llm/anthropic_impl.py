from __future__ import annotations

import asyncio
import logging
from typing import Any

import anthropic

from bot.services.llm.base import (
    SYSTEM_VOLATILE_MARKER,
    ChatMessage,
    LLMProvider,
    Tool,
    ToolContext,
)

logger = logging.getLogger(__name__)


def _log_usage(where: str, resp: Any) -> None:
    """Loga uso de tokens incl. métricas de prompt caching, pra avaliar o
    efeito do cache. cache_read > 0 = cache funcionando."""
    u = getattr(resp, "usage", None)
    if u is None:
        return
    logger.info(
        "anthropic[%s] usage: input=%s cache_write=%s cache_read=%s output=%s",
        where,
        getattr(u, "input_tokens", "?"),
        getattr(u, "cache_creation_input_tokens", 0),
        getattr(u, "cache_read_input_tokens", 0),
        getattr(u, "output_tokens", "?"),
    )


def _system_blocks(system: str | None) -> list[dict] | None:
    """Monta o system pra Anthropic com cache no prefixo ESTÁVEL. Se houver
    o marcador de contexto volátil, cacheia só a parte antes dele (o relógio
    no fim fica fora do cache, não invalidando system+tools a cada minuto)."""
    if not system:
        return None
    head, sep, tail = system.partition(SYSTEM_VOLATILE_MARKER)
    if sep:
        blocks = [{"type": "text", "text": head, "cache_control": {"type": "ephemeral"}}]
        if tail.strip():
            blocks.append({"type": "text", "text": tail})
        return blocks
    # sem marcador: cacheia o system inteiro
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def _to_anth_content(content: Any) -> Any:
    """Converte content (str ou list[block]) pro formato esperado pela API Anthropic."""
    if isinstance(content, str):
        return content
    out: list[dict] = []
    for b in content:
        bt = b.get("type")
        if bt == "text":
            out.append({"type": "text", "text": b.get("text", "")})
        elif bt == "image":
            out.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": b.get("media_type", "image/jpeg"),
                    "data": b.get("data", ""),
                },
            })
        elif bt == "document":
            out.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": b.get("media_type", "application/pdf"),
                    "data": b.get("data", ""),
                },
            })
        else:
            # bloco já em formato Anthropic nativo (tool_use, tool_result, etc).
            out.append(b)
    return out


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
            {"role": m["role"], "content": _to_anth_content(m["content"])}
            for m in messages
            if m["role"] in ("user", "assistant")
        ]

        def _call() -> str:
            kwargs: dict = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": anth_messages,
            }
            blocks = _system_blocks(system)
            if blocks:
                kwargs["system"] = blocks
            resp = self.client.messages.create(**kwargs)
            _log_usage("chat", resp)
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
            {"role": m["role"], "content": _to_anth_content(m["content"])}
            for m in messages
            if m["role"] in ("user", "assistant")
        ]
        tools_spec: list[dict] = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools
        ]
        # Server-side web search da Anthropic — sem handler local.
        tools_spec.append({
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,
        })
        # Prompt caching: marca o último tool como breakpoint → cacheia o
        # bloco inteiro de tools (estável). Reaproveitado entre as rodadas de
        # tool use da MESMA mensagem (que reenviam tudo) e entre mensagens na
        # janela de 5 min — corta ~90% dos tokens de input repetidos.
        tools_spec[-1] = {**tools_spec[-1], "cache_control": {"type": "ephemeral"}}
        tool_by_name = {t.name: t for t in tools}

        # System como bloco(s) cacheável(is): cacheia só o prefixo estável,
        # deixando o contexto volátil (data/hora) fora do cache.
        system_blocks = _system_blocks(system)

        for _ in range(max_iterations):
            def _call() -> anthropic.types.Message:
                kwargs: dict = {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "messages": anth_messages,
                    "tools": tools_spec,
                }
                if system_blocks:
                    kwargs["system"] = system_blocks
                return self.client.messages.create(**kwargs)

            resp = await asyncio.to_thread(_call)
            _log_usage("chat_with_tools", resp)

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
