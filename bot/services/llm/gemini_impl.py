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
    MAX_ITER_PADRAO,
    fallback_limite,
    instrucao_limite,
    teto_de_rodadas,
    ChatMessage,
    LLMProvider,
    Tool,
    ToolContext,
    resumo_tool_call,
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


# Níveis de thinking da API. O thinking_budget numérico foi descontinuado pelo
# Google (e-mail de 07/10/2026: nos próximos modelos, "requests that set
# thinking_budget will no longer be remapped and will return 400").
NIVEIS = ("minimal", "low", "medium", "high")

# (modelo, nível pedido) → nível que FUNCIONOU no lugar (None = sem ajuste).
# Aprendido em runtime, provado pelo retry ter dado certo. Cada modelo aceita
# um conjunto diferente — medido na API real (07/10/2026): gemini-3.8-flash
# recusa "minimal" com 400 ("Thinking level MINIMAL is not supported for this
# model"); gemini-3.1-flash-lite aceita. Quem decide é a API, não uma lista.
_NIVEL_SUBSTITUTO: dict[tuple[str, str], str | None] = {}


def _nivel_do_budget(budget: int | None) -> str | None:
    """Converte o budget numérico antigo (GEMINI_THINKING_BUDGET no .env ou
    /provider thinking salvo no banco) pro nível mais próximo — quem já tinha
    configurado não precisa refazer nada. -1/None = automático; 0 = o mínimo
    ("desligado" não existe como nível)."""
    if budget is None or int(budget) < 0:
        return None
    b = int(budget)
    if b == 0:
        return "minimal"
    if b <= 2048:
        return "low"
    if b <= 8192:
        return "medium"
    return "high"


def nivel_efetivo(nivel: str | None = None, budget: int | None = None) -> str | None:
    """Nível que vale: o do usuário (/provider thinking), senão o budget antigo
    dele convertido, senão o do .env (GEMINI_THINKING_LEVEL, ou o antigo
    GEMINI_THINKING_BUDGET convertido). None = automático (não envia nada)."""
    if nivel:
        return None if nivel == "auto" else nivel
    if budget is not None:
        return _nivel_do_budget(budget)
    from bot.config import settings as _s
    env = (getattr(_s, "gemini_thinking_level", "") or "").strip().lower()
    if env:
        return None if env == "auto" else env
    return _nivel_do_budget(getattr(_s, "gemini_thinking_budget", -1))


def _candidatos(model: str, nivel: str | None) -> list[str | None]:
    """Ordem de tentativa: o pedido (ou o substituto já aprendido), depois
    "low" se o pedido era "minimal" (o menor nível que os modelos sem
    "minimal" aceitam), depois sem ajuste (padrão do modelo)."""
    if nivel is None:
        return [None]
    if (model, nivel) in _NIVEL_SUBSTITUTO:
        return [_NIVEL_SUBSTITUTO[(model, nivel)]]
    out: list[str | None] = [nivel]
    if nivel == "minimal":
        out.append("low")
    out.append(None)
    return out


def _e_argumento_invalido(exc: Exception) -> bool:
    return "INVALID_ARGUMENT" in str(exc)


def gerar(client, model: str, contents, onde: str, nivel: str | None = None,
          **config_kwargs):
    """`generate_content` com o nível de thinking e queda automática.

    PÚBLICO porque não é só o chat: nota técnica do DOU, STT de voz e tradutor
    também fixam nível e quebrariam igual num modelo que recuse.

    Cada modelo aceita um conjunto de níveis, e o 400 INVALID_ARGUMENT não diz
    qual argumento é o inválido. Em vez de manter lista, tenta: se levar 400
    COM thinking_config, desce pro próximo candidato (minimal → low → padrão)
    e memoriza o que funcionou pro par (modelo, nível). 400 sem
    thinking_config, ou que persiste até o padrão, é outro problema (imagem,
    schema): propaga.
    """
    candidatos = _candidatos(model, nivel)

    def _chamar(n):
        thinking = types.ThinkingConfig(thinking_level=n.upper()) if n else None
        return client.models.generate_content(
            model=model, contents=contents,
            config=types.GenerateContentConfig(thinking_config=thinking, **config_kwargs),
        )

    for i, n in enumerate(candidatos):
        try:
            resultado = _chamar(n)
        except Exception as exc:
            ultimo = i == len(candidatos) - 1
            if n is None or not _e_argumento_invalido(exc) or ultimo:
                _log_payload(onde, model, contents, config_kwargs, n)
                raise
            logger.warning("gemini[%s]: %s deu 400 com thinking_level=%s — "
                           "tentando %s", onde, model, n,
                           candidatos[i + 1] or "sem ajuste")
            continue
        if i > 0 and nivel is not None:
            _NIVEL_SUBSTITUTO[(model, nivel)] = n
            logger.warning("gemini[%s]: %s não aceita thinking_level=%s — "
                           "usando %s daqui pra frente", onde, model, nivel,
                           n or "o padrão do modelo")
        return resultado
    raise RuntimeError("gerar: nenhum candidato")  # inalcançável


def _log_payload(onde, model, contents, config_kwargs, nivel) -> None:
    """Formato do que foi enviado (NÃO o conteúdo: sem vazar conversa)."""
    try:
        forma = [
            f"{getattr(c, 'role', '?')}:{len(getattr(c, 'parts', []) or [])}p"
            for c in contents
        ]
        system = config_kwargs.get("system_instruction")
        logger.error(
            "gemini[%s] FALHOU — model=%s contents=%d %s system=%s "
            "max_output_tokens=%s tools=%s thinking_level=%s",
            onde, model, len(contents), forma,
            f"{len(system)}ch" if system else "ausente",
            config_kwargs.get("max_output_tokens"),
            len(config_kwargs.get("tools") or []), nivel,
        )
    except Exception:
        logger.error("gemini[%s] FALHOU (e o log do payload também)", onde)


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

    def __init__(self, api_key: str, model: str, thinking_budget: int | None = None,
                 thinking_level: str | None = None) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY ausente")
        self.client = genai.Client(api_key=api_key)
        self.model_name = model
        self.model = model  # alias p/ interface comum (ex.: /ping)
        # Nível já resolvido: /provider thinking do usuário (nível novo ou
        # budget antigo convertido), senão o .env. None = automático.
        self.thinking_level = nivel_efetivo(thinking_level, thinking_budget)

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        contents = _messages_to_contents(messages)

        def _call() -> str:
            resp = gerar(
                self.client, self.model_name, contents, "chat",
                nivel=self.thinking_level,
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
        max_iterations: int = MAX_ITER_PADRAO,
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

        rodada = 0
        # Teto RECALCULADO a cada volta: se uma escrita acontecer no
        # meio de um turno que começou só lendo, o teto encurta na hora.
        while rodada < teto_de_rodadas(ctx, max_iterations):
            rodada += 1
            def _call() -> Any:
                return gerar(
                    self.client, self.model_name, contents, "chat_with_tools",
                    nivel=self.thinking_level,
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
                # Toda tool call fica no log — sem isto, "por que o bot me
                # respondeu ISSO?" não tem resposta na fonte real (ver
                # resumo_tool_call em llm/base.py).
                logger.info("gemini tool call: %s", resumo_tool_call(fc.name, args))
                tool = tool_by_name.get(fc.name)
                if tool is None:
                    result = f"erro: tool '{fc.name}' não existe"
                else:
                    try:
                        ctx.tools_chamadas.append(fc.name)
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
        # executou (ver instrucao_limite em llm/base.py — turno que só LEU não
        # inventaria ação nenhuma).
        logger.warning("gemini: max_iterations (%d) estourado", max_iterations)
        contents.append(
            types.Content(
                role="user", parts=[types.Part.from_text(text=instrucao_limite(ctx))],
            )
        )

        def _final() -> Any:
            # Declarações continuam (o histórico tem function_call/response),
            # com function calling em modo NONE: só texto sai daqui.
            return gerar(
                self.client, self.model_name, contents, "chat_with_tools[limite]",
                nivel=self.thinking_level,
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
        return texto or fallback_limite(ctx)
