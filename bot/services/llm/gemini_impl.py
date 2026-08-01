"""Gemini provider via SDK `google-genai` 1.x.

Diferente do `google-generativeai` (0.x), o `google-genai` (1.x) suporta
combinar `function_declarations` (tool use customizado) com `google_search`
(busca web nativa) na mesma chamada — o que destrava web search no Gemini.

Voice STT também usa este SDK (ver bot/services/voice.py): o antigo
`google-generativeai` 0.x fala gRPC e pendura em alguns ambientes (ARM/docker).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from google import genai
from google.genai import types

from bot.services.llm.base import (
    ITER_LIMIT_FALLBACK,
    ITER_LIMIT_INSTRUCTION,
    ChatMessage,
    LLMProvider,
    Tool,
    ToolContext,
)

logger = logging.getLogger(__name__)


def _log_usage(where: str, resp: Any) -> None:
    """Loga uso de tokens incl. caching implícito do Gemini (ligado por padrão
    em 2.5/3.x). cached > 0 = o prefixo estável (system+tools) foi cacheado —
    é a métrica pra confirmar a economia. thoughts = tokens de 'thinking'."""
    u = getattr(resp, "usage_metadata", None)
    if u is None:
        return
    logger.info(
        "gemini[%s] usage: prompt=%s cached=%s thoughts=%s output=%s total=%s",
        where,
        getattr(u, "prompt_token_count", "?"),
        getattr(u, "cached_content_token_count", 0) or 0,
        getattr(u, "thoughts_token_count", 0) or 0,
        getattr(u, "candidates_token_count", "?"),
        getattr(u, "total_token_count", "?"),
    )


# Modelos Gemini 2.5 "pensam" por padrão e o thinking consome max_output_tokens.
# Garantimos um teto alto o bastante pra sobrar tokens pro texto final (senão a
# resposta vem vazia — "(sem resposta)"). É só um teto; respostas curtas não gastam tudo.
_MIN_OUTPUT_TOKENS = 8192


# Modelos que RECUSARAM o thinking_budget configurado (400 INVALID_ARGUMENT),
# aprendidos em runtime. Adivinhar por nome já falhou: o clamp abaixo cobria só
# "pro" e o gemini-3.6-flash passou batido, derrubando TODO chat com
# GEMINI_THINKING_BUDGET=0. A lista de modelos do Gemini muda toda semana, então
# quem decide é a API, não um heurístico de substring.
_SEM_THINKING_BUDGET: set[str] = set()


def _thinking_config(model: str):
    """ThinkingConfig conforme settings.gemini_thinking_budget. None = automático
    (sem alteração); -1 idem; 0 desliga; N fixa. O pro não permite desligar
    (mín ~128), então clampa pra 128 quando o budget global for 0 — evita 400."""
    if model in _SEM_THINKING_BUDGET:
        return None
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


def _e_argumento_invalido(exc: Exception) -> bool:
    return "INVALID_ARGUMENT" in str(exc)


def _gerar(client, model: str, contents, onde: str, **config_kwargs):
    """`generate_content` com queda automática do thinking_config.

    O budget que um modelo aceita, outro recusa com 400 INVALID_ARGUMENT — e a
    resposta não diz qual argumento é o inválido, então o sintoma é "todo chat
    quebrado" sem pista. Em vez de manter lista de quem aceita o quê, tenta;
    se levar 400 COM thinking_config, repete UMA vez sem ele e memoriza o
    modelo, pra não pagar a ida e volta dupla nas mensagens seguintes.
    """
    tc = _thinking_config(model)

    def _chamar(thinking):
        return client.models.generate_content(
            model=model, contents=contents,
            config=types.GenerateContentConfig(thinking_config=thinking, **config_kwargs),
        )

    try:
        return _chamar(tc)
    except Exception as exc:
        if tc is None or not _e_argumento_invalido(exc):
            _log_payload(onde, model, contents, config_kwargs, tc)
            raise
        _SEM_THINKING_BUDGET.add(model)
        logger.warning(
            "gemini[%s]: %s recusou thinking_budget=%s (400) — repetindo sem "
            "thinking_config e desligando esse ajuste para este modelo",
            onde, model, getattr(tc, "thinking_budget", "?"),
        )
        try:
            return _chamar(None)
        except Exception:
            _log_payload(onde, model, contents, config_kwargs, None)
            raise


def _log_payload(onde, model, contents, config_kwargs, thinking) -> None:
    """Formato do que foi enviado (NÃO o conteúdo: sem vazar conversa)."""
    try:
        budget = getattr(thinking, "thinking_budget", None) if thinking else None
        forma = [
            f"{getattr(c, 'role', '?')}:{len(getattr(c, 'parts', []) or [])}p"
            for c in contents
        ]
        system = config_kwargs.get("system_instruction")
        logger.error(
            "gemini[%s] FALHOU — model=%s contents=%d %s system=%s "
            "max_output_tokens=%s tools=%s thinking_budget=%s",
            onde, model, len(contents), forma,
            f"{len(system)}ch" if system else "ausente",
            config_kwargs.get("max_output_tokens"),
            len(config_kwargs.get("tools") or []), budget,
        )
    except Exception:
        logger.error("gemini[%s] FALHOU (e o log do payload também)", onde)


def _to_genai_parts(content: Any) -> list[types.Part]:
    """Converte content (str ou list[block]) pra lista de Part do google-genai.

    Texto VAZIO não vira part: `{"text": ""}` no corpo é recusado com
    400 INVALID_ARGUMENT ("Request contains an invalid argument"), sem dizer
    qual argumento. Basta uma mensagem vazia no histórico — resposta que veio
    em branco, turno só de tool call — pra derrubar toda conversa seguinte.
    """
    if isinstance(content, str):
        return [types.Part.from_text(text=content)] if content.strip() else []
    parts: list[types.Part] = []
    for b in content:
        bt = b.get("type")
        if bt == "text":
            texto = b.get("text", "")
            if not texto.strip():
                continue
            parts.append(types.Part.from_text(text=texto))
        elif bt in ("image", "document"):
            import base64 as _b64
            data_bytes = _b64.b64decode(b.get("data", ""))
            mime = b.get("media_type", "image/jpeg" if bt == "image" else "application/pdf")
            parts.append(types.Part.from_bytes(data=data_bytes, mime_type=mime))
    return parts


def _messages_to_contents(messages: list[ChatMessage]) -> list[types.Content]:
    """Converte messages do nosso formato pra list[Content] do google-genai.

    Mensagem que ficou SEM parts é descartada: `Content` com lista vazia é o
    mesmo 400 INVALID_ARGUMENT do part vazio.
    """
    contents: list[types.Content] = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        parts = _to_genai_parts(m["content"])
        if not parts:
            logger.debug("gemini: mensagem %s sem conteúdo utilizável — fora do payload", role)
            continue
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
            resp = _gerar(
                self.client, self.model_name, contents, "chat",
                system_instruction=system,
                max_output_tokens=max(max_tokens, _MIN_OUTPUT_TOKENS),
            )
            _log_usage("chat", resp)
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
                return _gerar(
                    self.client, self.model_name, contents, "chat_with_tools",
                    system_instruction=system,
                    tools=genai_tools,
                    max_output_tokens=max(max_tokens, _MIN_OUTPUT_TOKENS),
                )

            resp = await asyncio.to_thread(_call)
            _log_usage("chat_with_tools", resp)

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

        # Limite estourado: última rodada SEM tools pro modelo contar o que já
        # executou (ver ITER_LIMIT_INSTRUCTION em llm/base.py).
        logger.warning("gemini: max_iterations (%d) estourado", max_iterations)
        contents.append(
            types.Content(
                role="user", parts=[types.Part.from_text(text=ITER_LIMIT_INSTRUCTION)],
            )
        )

        def _final() -> Any:
            # Declarações continuam (o histórico tem function_call/response),
            # com function calling em modo NONE: só texto sai daqui.
            return _gerar(
                self.client, self.model_name, contents, "chat_with_tools[limite]",
                system_instruction=system,
                tools=genai_tools,
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode="NONE"),
                ),
                max_output_tokens=max(max_tokens, _MIN_OUTPUT_TOKENS),
            )

        try:
            resp = await asyncio.to_thread(_final)
            _log_usage("chat_with_tools[limite]", resp)
            texto = (resp.text or "").strip()
        except Exception:
            logger.exception("gemini: rodada final pós-limite falhou")
            texto = ""
        return texto or ITER_LIMIT_FALLBACK
