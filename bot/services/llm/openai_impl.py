from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from openai import OpenAI

from bot.services.llm.base import (
    ITER_LIMIT_FALLBACK,
    ITER_LIMIT_INSTRUCTION,
    ChatMessage,
    LLMProvider,
    Tool,
    ToolContext,
    resumo_tool_call,
)

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


# Modelos de RACIOCÍNIO da OpenAI (o1/o3/o4, gpt-5…) REJEITAM `max_tokens` —
# exigem `max_completion_tokens`. O /provider oferece esses ids (o catálogo
# lista o1/o3/o4 em catalog.py::_OPENAI_PREFIX), então escolher um deles
# quebrava TODO chat com "❌ erro no LLM", sem pista do motivo.
_MAX_COMPLETION_PREFIXOS = ("o1", "o3", "o4", "gpt-5", "gpt-6")
# Nesses modelos o teto cobre os tokens de RACIOCÍNIO também; com 1024 o
# pensamento consome tudo e a resposta volta VAZIA. Mesmo raciocínio do
# _MIN_OUTPUT_TOKENS do Gemini.
_MIN_REASONING_TOKENS = 4096


def _campo_teto(model: str) -> str:
    return (
        "max_completion_tokens"
        if (model or "").lower().startswith(_MAX_COMPLETION_PREFIXOS)
        else "max_tokens"
    )


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY ausente")
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def _create(self, *, max_tokens: int, **kwargs: Any):
        """chat.completions.create escolhendo o parâmetro de teto certo pro
        modelo — e, se a API reclamar DESSE parâmetro (modelo novo fora da
        lista de prefixos), trocando e refazendo UMA vez. Prefixo é palpite;
        a mensagem da API é fato."""
        campo = _campo_teto(self.model)
        teto = (
            max(max_tokens, _MIN_REASONING_TOKENS)
            if campo == "max_completion_tokens" else max_tokens
        )
        try:
            return self.client.chat.completions.create(**kwargs, **{campo: teto})
        except Exception as e:
            msg = str(e)
            if campo not in msg:
                raise
            outro = "max_tokens" if campo == "max_completion_tokens" else "max_completion_tokens"
            logger.warning(
                "openai: modelo %s recusou '%s' (%s) — refazendo com '%s'",
                self.model, campo, msg[:120], outro,
            )
            teto = max(teto, _MIN_REASONING_TOKENS) if outro == "max_completion_tokens" else teto
            return self.client.chat.completions.create(**kwargs, **{outro: teto})

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
            resp = self._create(
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
                return self._create(
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
                # Toda tool call fica no log — sem isto, "por que o bot me
                # respondeu ISSO?" não tem resposta na fonte real (ver
                # resumo_tool_call em llm/base.py).
                logger.info("openai tool call: %s", resumo_tool_call(fn_name, fn_args))
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

        # Limite estourado: última rodada SEM tools pro modelo contar o que já
        # executou (ver ITER_LIMIT_INSTRUCTION em llm/base.py).
        logger.warning("openai: max_iterations (%d) estourado", max_iterations)
        oa_messages.append({"role": "user", "content": ITER_LIMIT_INSTRUCTION})

        def _final():
            # tools declaradas (o histórico tem tool_calls), mas proibidas de
            # rodar de novo — só a resposta em texto.
            return self._create(
                model=self.model, messages=oa_messages, max_tokens=max_tokens,
                tools=tools_spec, tool_choice="none",
            )

        try:
            resp = await asyncio.to_thread(_final)
            texto = (resp.choices[0].message.content or "").strip()
        except Exception:
            logger.exception("openai: rodada final pós-limite falhou")
            texto = ""
        return texto or ITER_LIMIT_FALLBACK
