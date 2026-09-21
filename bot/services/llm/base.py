from __future__ import annotations

import base64
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession


def resumo_tool_call(name: str, args: dict | None) -> str:
    """Linha compacta 'tool(args)' pro log de cada tool que o modelo chama.

    Motivo (04/08/2026): áudio pedindo previsão do tempo voltou com a fatura
    INTEIRA do cartão na frente — alguma tool financeira rodou num turno de
    clima e o log não tinha COMO dizer qual/por quê: nenhum provider logava
    as tool calls. Sem isso, todo bug de roteamento do modelo é indiagnosticável
    contra a fonte real (o log do Orange Pi).
    """
    try:
        s = json.dumps(args or {}, ensure_ascii=False, default=str)
    except Exception:
        s = repr(args)
    if len(s) > 300:
        s = s[:300] + "…"
    return f"{name}({s})"


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
    # Texto cru do pedido do usuário (mensagem/transcrição). Usado por travas
    # determinísticas nos handlers — ex.: redirecionar banco→cartão quando o
    # texto cita 'crédito/cartão' mas o LLM lançou no banco.
    user_text: str = ""
    # Marcado True quando uma tool de LANÇAMENTO financeiro grava com sucesso
    # (lancar_movimento_banco / lancar_despesa_cartao / registrar_aporte_tesouro).
    # Usado pela blindagem anti-alucinação no handler de chat/voz.
    financial_logged_ok: bool = False
    # Nomes das tools que rodaram NESTE turno, na ordem. Serve pra decidir se
    # o estouro de rodadas deve inventariar ações (turno que gravou) ou só
    # responder o que apurou (turno que só leu). Ver houve_escrita().
    tools_chamadas: list = field(default_factory=list)
    # Setado por consultar_mp_dou quando acha MP(s) numa data: {"date_iso", "count"}.
    # O handler de chat/voz usa pra oferecer a nota técnica com botões Sim/Não.
    dou_mp_found: Any = None
    # Texto já formatado que o handler usa SÓ quando o LLM volta vazio (a
    # geração que segue uma tool call às vezes vem sem texto no Gemini). Não
    # short-circuita — preserva teclados (ex.: botões Sim/Não da nota técnica).
    fallback_text: str | None = None
    # Partes do texto verbatim (ver property direct_html abaixo). O campo com
    # underscore existe só pro dataclass; ninguém o usa direto.
    _direct_parts: list = field(default_factory=list)
    # Quando uma tool seta isto True, o loop de tool use encerra logo após
    # executar a tool, sem mais uma chamada ao LLM (a resposta já está pronta
    # via ctx.direct_html/etc). Evita uma geração extra desperdiçada.
    short_circuit: bool = False
    # Setado por zerar_lista_compras: o handler anexa botões Sim/Cancelar em
    # vez de apagar na hora (evita o LLM zerar a lista sem confirmação).
    confirm_clear_shopping: bool = False
    # Teclado inline GENÉRICO pra resposta verbatim (direct_html): a tool que
    # precisa de botão próprio (ex.: propor_agente com "Resolver com o agente")
    # seta aqui em vez de ganhar mais um flag dedicado no deliver.
    direct_markup: Any = None
    # Setado por consultar_transito quando a origem não é casa/trabalho explícito:
    # em vez de assumir HOME_COORDS silenciosamente, o handler anexa o teclado
    # "📍 Enviar localização" (mesma UX do /rota) e registra o pending route.
    # A tool já preencheu pending_routes; aqui o handler só precisa montar o
    # teclado e atrelar à mensagem.
    request_location: bool = False

    # Texto HTML já formatado que o handler de chat/voz envia verbatim
    # (parse_mode=HTML), ignorando a resposta do LLM — evita paráfrase.
    #
    # ACUMULA em vez de sobrescrever (auditoria 03/08/2026): com DUAS tools
    # verbatim no mesmo turno ("comprei pão por 10 e gasolina por 200" → dois
    # lançamentos legítimos), o slot único guardava só a ÚLTIMA confirmação —
    # a primeira sumia, o dono achava que não gravou e re-lançava (duplicata).
    # O setter anexa (idempotente pra re-set do mesmo texto); None limpa.
    @property
    def direct_html(self) -> str | None:
        return "\n\n".join(self._direct_parts) if self._direct_parts else None

    @direct_html.setter
    def direct_html(self, valor: str | None) -> None:
        if valor is None:
            self._direct_parts.clear()
        elif valor not in self._direct_parts[-1:]:
            self._direct_parts.append(valor)


ToolHandler = Callable[[dict, ToolContext], Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON schema (type: object, properties: {...}, required: [...])
    handler: ToolHandler


# Tools que só LEEM. Tudo que não estiver aqui conta como escrita — o default
# seguro é o inverso do confortável: classificar errado pra "leitura" esconderia
# do dono que algo foi gravado (e ele repetiria o pedido, duplicando);
# classificar errado pra "escrita" só produz um aviso a mais.
#
# Origem (dono, 21/09/2026): perguntou onde comprar um drone no Japão e a
# resposta veio com "Nesta conversa, as seguintes ações já foram executadas e
# registradas no sistema: • Lançamento de R$ 236,00 no cartão…". Eram
# lançamentos REAIS e ANTIGOS da conversa, recitados porque o turno bateu no
# limite de rodadas e a instrução de estouro manda inventariar o que rodou.
# Numa busca web não há nada a duplicar — o inventário era só ruído.
TOOLS_SOMENTE_LEITURA = frozenset({
    # consulta externa
    "buscar_web", "ler_pagina", "buscar_local", "buscar_preco",
    "consultar_clima", "consultar_cotacao", "consultar_transito",
    "melhor_horario_sair", "buscar_voo", "buscar_hotel",
    # consulta ao estado do usuário
    "consultar_saldo", "consultar_lancamentos", "buscar_lancamento",
    "consultar_treinos", "listar_tarefas", "listar_compras",
    "listar_lembretes", "listar_watches_viagem", "listar_arquivos",
    "listar_fatos", "recuperar_fato", "buscar_historico",
    # legislativo
    "consultar_mp_dou", "consultar_congresso", "consultar_pauta_camara",
    "varrer_comissoes_partido", "listar_comissoes_reuniao",
    "consultar_sessoes_cinema",
    # ajuda e cálculo
    "ajuda", "propor_agente",
    # Processo isolado, env limpo, tmpdir efêmero: não toca estado do usuário.
    "executar_python",
})


# Rodadas de ferramenta por turno. Eram 5 fixas — e uma busca web que abre
# duas páginas já estourava (dono, 21/09/2026: pesquisa de loja no Japão).
# Turno que só LÊ pode ir mais longe: nenhuma rodada extra grava nada, e o
# custo é uma chamada de LLM a mais. Turno que ESCREVE mantém o teto curto:
# lá cada rodada tem efeito permanente e passear é arriscado.
MAX_ITER_PADRAO = 8
MAX_ITER_SOMENTE_LEITURA = 16


def teto_de_rodadas(ctx, max_iterations: int) -> int:
    """Teto efetivo do turno. Só afrouxa enquanto NADA foi gravado."""
    if houve_escrita(ctx):
        return max_iterations
    return max(max_iterations, MAX_ITER_SOMENTE_LEITURA)


def houve_escrita(ctx) -> bool:
    """Alguma tool com efeito PERMANENTE rodou neste turno?

    `executar_python` conta como leitura: roda em processo isolado com env
    limpo e tmpdir efêmero — não toca estado nenhum do usuário."""
    chamadas = getattr(ctx, "tools_chamadas", None) or ()
    return any(n not in TOOLS_SOMENTE_LEITURA for n in chamadas)


def instrucao_limite(ctx) -> str:
    """Instrução da última rodada, conforme o turno gravou algo ou não."""
    if houve_escrita(ctx):
        return ITER_LIMIT_INSTRUCTION
    return ITER_LIMIT_INSTRUCTION_LEITURA


def fallback_limite(ctx) -> str:
    if houve_escrita(ctx):
        return ITER_LIMIT_FALLBACK
    return ITER_LIMIT_FALLBACK_LEITURA


# Estouro de `max_iterations`: as tools JÁ rodaram (lançamento no Firestore,
# lembrete criado, item na lista). Devolver "(limite de iterações...)" fazia o
# usuário achar que nada aconteceu e REPETIR o pedido — duplicando o que já
# estava gravado. Em vez disso pedimos ao modelo uma ÚLTIMA rodada SEM tools,
# só pra contar o que já fez; e se nem isso sair, vai o aviso explícito abaixo.
ITER_LIMIT_INSTRUCTION = (
    "PARE de usar ferramentas — o limite de rodadas foi atingido. "
    "Responda AGORA ao usuário, em português, contando o que você JÁ executou "
    "nesta conversa (as ferramentas que rodaram tiveram efeito real e "
    "permanente) e o que ficou faltando. Não invente resultado que não veio "
    "de uma ferramenta."
)
ITER_LIMIT_FALLBACK = (
    "⚠️ Precisei parar no meio: bati o limite de rodadas de ferramenta. "
    "ATENÇÃO: parte do que você pediu PODE já ter sido executada (lançamento, "
    "lembrete, item de lista). Confira antes de repetir o pedido, pra não "
    "duplicar."
)

# Mesmas situações, turno que SÓ LEU: não há nada gravado, logo não há o que
# inventariar nem risco de duplicar. Pedir o relato de ações aqui produzia
# aquele preâmbulo burocrático em cima de uma resposta que estava boa.
ITER_LIMIT_INSTRUCTION_LEITURA = (
    "PARE de usar ferramentas — o limite de rodadas foi atingido. Responda "
    "AGORA ao usuário, em português, com o que você APUROU até aqui. Nada foi "
    "gravado neste turno: NÃO liste ações executadas nem fale em registros no "
    "sistema. Se algo da pergunta ficou sem resposta, diga em UMA linha no "
    "fim. Não invente resultado que não veio de uma ferramenta."
)
ITER_LIMIT_FALLBACK_LEITURA = (
    "⚠️ Precisei parar no meio: bati o limite de rodadas de ferramenta e não "
    "consegui fechar a pesquisa. Nada foi gravado — pode pedir de novo, "
    "de preferência estreitando a pergunta."
)


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
