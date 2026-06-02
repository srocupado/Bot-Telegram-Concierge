from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession


class ChatMessage(TypedDict):
    role: str  # "user" | "assistant" | "system"
    # content é str pra mensagens só-texto, ou list[ContentBlock] pra multimodal.
    # ContentBlock é {"type": "text", "text": "..."}
    # ou {"type": "image", "data": "<base64>", "media_type": "image/jpeg"}
    content: Any


def make_image_message(text: str, image_bytes: bytes, mime_type: str = "image/jpeg") -> ChatMessage:
    """Constrói uma mensagem multimodal (user) com texto opcional + imagem."""
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.append({"type": "image", "data": b64, "media_type": mime_type})
    return {"role": "user", "content": parts}


def make_document_message(
    text: str, doc_bytes: bytes, mime_type: str = "application/pdf",
) -> ChatMessage:
    """Constrói uma mensagem multimodal (user) com texto opcional + documento (PDF)."""
    b64 = base64.standard_b64encode(doc_bytes).decode("ascii")
    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.append({"type": "document", "data": b64, "media_type": mime_type})
    return {"role": "user", "content": parts}


@dataclass
class ToolContext:
    user: Any  # bot.db.models.User — Any para evitar import circular
    session: AsyncSession
    tz: str
    # Marcado True quando uma tool de LANÇAMENTO financeiro grava com sucesso
    # (lancar_movimento_banco / lancar_despesa_cartao / registrar_aporte_tesouro).
    # Usado pela blindagem anti-alucinação no handler de chat/voz.
    financial_logged_ok: bool = False
    # Setado por consultar_mp_dou quando acha MP(s) numa data: {"date_iso", "count"}.
    # O handler de chat/voz usa pra oferecer a nota técnica com botões Sim/Não.
    dou_mp_found: Any = None
    # Texto HTML já formatado que o handler de chat/voz envia verbatim
    # (parse_mode=HTML), ignorando a resposta do LLM — evita paráfrase. Usado
    # por consultar_congresso e consultar_transito (saída idêntica aos /comandos).
    direct_html: str | None = None
    # Quando uma tool seta isto True, o loop de tool use encerra logo após
    # executar a tool, sem mais uma chamada ao LLM (a resposta já está pronta
    # via ctx.direct_html/etc). Evita uma geração extra desperdiçada.
    short_circuit: bool = False
    # Setado por zerar_lista_compras: o handler anexa botões Sim/Cancelar em
    # vez de apagar na hora (evita o LLM zerar a lista sem confirmação).
    confirm_clear_shopping: bool = False
    # Setado por consultar_transito quando a origem não é casa/trabalho explícito:
    # em vez de assumir HOME_COORDS silenciosamente, o handler anexa o teclado
    # "📍 Enviar localização" (mesma UX do /rota) e registra o pending route.
    # A tool já preencheu pending_routes; aqui o handler só precisa montar o
    # teclado e atrelar à mensagem.
    request_location: bool = False


ToolHandler = Callable[[dict, ToolContext], Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON schema (type: object, properties: {...}, required: [...])
    handler: ToolHandler


class LLMProvider(ABC):
    name: str

    @abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        ...

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
        """Default fallback: ignora tools e chama chat() normal."""
        return await self.chat(messages, system=system, max_tokens=max_tokens)

    async def ping(self) -> str:
        return await self.chat(
            [{"role": "user", "content": "Responda apenas com 'pong' (sem aspas)."}],
            max_tokens=8,
        )
