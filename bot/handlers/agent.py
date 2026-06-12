"""Agente de execução (/agente) — owner-only.

Comandos: /agente <tarefa>, /agente_parar, /agente_status, /agente_fim,
/agente_config. Texto livre do owner com sessão ativa (TTL) continua a
mesma sessão (resume). Não-owner: silêncio total (nem vê o recurso).

A tarefa roda em background (asyncio.create_task) — o handler retorna logo
e o progresso vai numa ÚNICA mensagem de status editada (máx 1 edição/3s).
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from collections import deque

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import FSInputFile, Message

from bot.config import settings
from bot.db.models import User
from bot.services.agent_config import agent_config
from bot.services.agent_runner import (
    AgentBusyError,
    AgentEvent,
    AgentResult,
    runner,
)

logger = logging.getLogger(__name__)

router = Router(name=__name__)

# Bot global pro caminho da tool executar_agente (tools não recebem Bot).
_bot: Bot | None = None

# Sessão de continuação (owner-only → 1 sessão global): após uma tarefa,
# texto livre do owner dentro do TTL continua a MESMA sessão via resume.
_session: dict | None = None  # {"id": str, "expires": float}

_BUSY_MSG = (
    "⏳ O agente já está executando uma tarefa. "
    "Acompanhe na mensagem de status, ou use /agente_parar."
)


def set_bot(bot: Bot) -> None:
    global _bot
    _bot = bot


def _is_owner(message: Message) -> bool:
    return bool(
        settings.owner_telegram_id
        and message.from_user
        and message.from_user.id == settings.owner_telegram_id
    )


def _active_session_id() -> str | None:
    global _session
    if _session and _session["expires"] > time.time():
        return _session["id"]
    _session = None
    return None


def _store_session(session_id: str | None) -> None:
    global _session
    if session_id:
        _session = {
            "id": session_id,
            "expires": time.time() + agent_config.session_ttl_minutes * 60,
        }


def _clear_session() -> None:
    global _session
    _session = None


class ProgressReporter:
    """Edita UMA mensagem de status, coalescendo eventos (máx 1 edição/3s)."""

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_id: int | None = None
        self._lines: deque[str] = deque(maxlen=8)
        self._last_edit = 0.0
        self._header = "🤖 <b>Agente trabalhando…</b>"

    async def start(self, prompt: str) -> None:
        preview = prompt if len(prompt) <= 120 else prompt[:117] + "…"
        self._header = f"🤖 <b>Agente trabalhando…</b>\n<i>{html.escape(preview)}</i>"
        msg = await self._bot.send_message(
            self._chat_id, self._header, parse_mode="HTML",
        )
        self._message_id = msg.message_id

    async def on_event(self, event: AgentEvent) -> None:
        self._lines.append(html.escape(event.line))
        now = time.monotonic()
        if now - self._last_edit < 3.0:
            return
        self._last_edit = now
        await self._edit(self._render())

    async def finish(self, summary_html: str) -> None:
        await self._edit(summary_html)

    def _render(self) -> str:
        return self._header + "\n\n" + "\n".join(self._lines)

    async def _edit(self, text: str) -> None:
        if self._message_id is None:
            return
        try:
            await self._bot.edit_message_text(
                text, chat_id=self._chat_id, message_id=self._message_id,
                parse_mode="HTML",
            )
        except TelegramRetryAfter as e:
            # Flood control: pula esta edição; a próxima (>retry_after) passa.
            self._last_edit = time.monotonic() + e.retry_after
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                logger.warning("agent progress edit falhou: %s", e)
        except Exception:
            logger.warning("agent progress edit falhou", exc_info=True)


def _chunk_text(text: str, limit: int = 4000) -> list[str]:
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # Parágrafo sozinho maior que o limite: corta na marra.
            while len(para) > limit:
                chunks.append(para[:limit])
                para = para[limit:]
            current = para
    if current:
        chunks.append(current)
    return chunks


def _summary_html(result: AgentResult) -> str:
    mins, secs = divmod(int(result.duration_s), 60)
    parts = [f"{mins}min {secs}s" if mins else f"{secs}s"]
    if result.num_turns:
        parts.append(f"{result.num_turns} turnos")
    if result.total_cost_usd is not None:
        parts.append(f"US$ {result.total_cost_usd:.3f}")
    meta = " · ".join(parts)
    if result.ok:
        head = "✅ <b>Tarefa concluída</b>"
    else:
        head = f"⚠️ <b>Tarefa não concluída</b> — {html.escape(result.error or 'erro')}"
    extra = ""
    if result.ok:
        extra = (
            f"\n💬 Responda em até {agent_config.session_ttl_minutes} min pra "
            "continuar a mesma tarefa (/agente_fim encerra)."
        )
    return f"{head}\n<i>{meta}</i>{extra}"


async def _deliver(bot: Bot, chat_id: int, result: AgentResult) -> None:
    """Texto final fatiado + artefatos do workspace via sendDocument."""
    text = (result.final_text or "").strip()
    if text:
        for chunk in _chunk_text(text):
            try:
                await bot.send_message(chat_id, chunk, parse_mode=None)
            except Exception:
                logger.exception("agent: falha ao enviar texto final")
                break
    for path in result.artifacts:
        try:
            await bot.send_document(chat_id, FSInputFile(path))
        except Exception:
            logger.exception("agent: falha ao enviar artefato %s", path)
            try:
                await bot.send_message(
                    chat_id,
                    f"⚠️ Não consegui enviar {path.name} (ele segue em ./workspace).",
                    parse_mode=None,
                )
            except Exception:
                pass


async def _run_and_report(
    bot: Bot, chat_id: int, prompt: str, resume_session_id: str | None,
) -> None:
    reporter = ProgressReporter(bot, chat_id)
    try:
        await reporter.start(prompt)
        result = await runner.run(
            prompt, chat_id=chat_id,
            resume_session_id=resume_session_id,
            on_event=reporter.on_event,
        )
    except AgentBusyError:
        await bot.send_message(chat_id, _BUSY_MSG, parse_mode=None)
        return
    except Exception as e:
        logger.exception("agent: tarefa falhou")
        await reporter.finish(f"❌ <b>Erro no agente</b>: {html.escape(str(e))}")
        return
    _store_session(result.session_id if result.ok else None)
    await reporter.finish(_summary_html(result))
    await _deliver(bot, chat_id, result)


def try_continuation(message: Message, user: User, text: str) -> bool:
    """Se o owner tem sessão ativa em TTL, continua a tarefa com `text`.

    Usado pelo handler de voz (texto transcrito não passa pelo filtro de
    mensagem deste router). True = consumiu a mensagem.
    """
    if not (_is_owner(message) and user.is_authorized):
        return False
    sid = _active_session_id()
    if sid is None or runner.busy:
        return False
    asyncio.create_task(_run_and_report(message.bot, message.chat.id, text, sid))
    return True


def start_background_task(prompt: str, chat_id: int) -> str:
    """Entrada usada pela tool executar_agente (chat livre / voz).

    Retorna: 'started' | 'busy' | 'disabled'.
    """
    if not runner.enabled or _bot is None:
        return "disabled"
    if runner.busy:
        return "busy"
    asyncio.create_task(_run_and_report(_bot, chat_id, prompt, None))
    return "started"


async def notify_stale_task(bot: Bot) -> None:
    """No startup: se havia tarefa em andamento quando o bot caiu, avisa o owner."""
    stale = runner.pop_stale_state()
    if not stale or not settings.owner_telegram_id:
        return
    try:
        await bot.send_message(
            settings.owner_telegram_id,
            "⚠️ O bot reiniciou no meio de uma tarefa do agente e ela foi "
            f"perdida:\n<i>{html.escape(stale.get('prompt', '?'))}</i>\n"
            "Mande /agente de novo se quiser refazer.",
            parse_mode="HTML",
        )
    except Exception:
        logger.warning("agent: falha ao avisar tarefa perdida", exc_info=True)


# --- comandos ---


@router.message(Command("agente"))
async def cmd_agente(message: Message, command: CommandObject, user: User) -> None:
    if not _is_owner(message) or not user.is_authorized:
        return  # não-owner nem fica sabendo que o comando existe
    prompt = (command.args or "").strip()
    if not prompt:
        await message.answer(
            "🤖 <b>Agente de execução</b>\n"
            "Escreve, EXECUTA e itera código no workspace, e te entrega os "
            "arquivos prontos.\n\n"
            "<code>/agente &lt;tarefa&gt;</code> — ex.: <i>/agente crie um script "
            "python que gera um CSV com fibonacci e execute</i>\n"
            "<code>/agente_status</code> · <code>/agente_parar</code> · "
            "<code>/agente_fim</code> · <code>/agente_config</code>\n\n"
            "Também funciona por voz (<i>\"barra agente …\"</i>) e em linguagem "
            "natural no chat (<i>\"constrói um app que…\"</i>).",
            parse_mode="HTML",
        )
        return
    if not runner.enabled:
        await message.answer(
            "⚠️ Agente desabilitado: confira OWNER_TELEGRAM_ID e "
            "ANTHROPIC_API_KEY no .env.", parse_mode=None,
        )
        return
    if runner.busy:
        await message.answer(_BUSY_MSG, parse_mode=None)
        return
    asyncio.create_task(_run_and_report(message.bot, message.chat.id, prompt, None))


@router.message(Command("agente_parar"))
async def cmd_agente_parar(message: Message, user: User) -> None:
    if not _is_owner(message) or not user.is_authorized:
        return
    if await runner.abort():
        await message.answer("🛑 Pedido de parada enviado ao agente.", parse_mode=None)
    else:
        await message.answer("Nenhuma tarefa em andamento.", parse_mode=None)


@router.message(Command("agente_status"))
async def cmd_agente_status(message: Message, user: User) -> None:
    if not _is_owner(message) or not user.is_authorized:
        return
    if not runner.enabled:
        await message.answer(
            "⚠️ Agente desabilitado (OWNER_TELEGRAM_ID/ANTHROPIC_API_KEY).",
            parse_mode=None,
        )
        return
    if runner.busy and runner.started_at is not None:
        elapsed = int(time.monotonic() - runner.started_at)
        prompt = runner.current_prompt or "?"
        if len(prompt) > 120:
            prompt = prompt[:117] + "…"
        await message.answer(
            f"⏳ <b>Rodando há {elapsed}s</b>\n<i>{html.escape(prompt)}</i>",
            parse_mode="HTML",
        )
        return
    sid = _active_session_id()
    if sid and _session:
        left = int((_session["expires"] - time.time()) / 60)
        await message.answer(
            f"💤 Ocioso. Sessão de continuação ativa por mais ~{left} min "
            "(texto livre continua a última tarefa; /agente_fim encerra).",
            parse_mode=None,
        )
    else:
        await message.answer("💤 Ocioso, sem sessão ativa.", parse_mode=None)


@router.message(Command("agente_fim"))
async def cmd_agente_fim(message: Message, user: User) -> None:
    if not _is_owner(message) or not user.is_authorized:
        return
    had = _active_session_id() is not None
    _clear_session()
    await message.answer(
        "✅ Sessão encerrada — texto livre volta ao chat normal."
        if had else "Não havia sessão de continuação ativa.",
        parse_mode=None,
    )


@router.message(Command("agente_config"))
async def cmd_agente_config(message: Message, command: CommandObject, user: User) -> None:
    if not _is_owner(message) or not user.is_authorized:
        return
    tokens = (command.args or "").strip().split()
    if not tokens:
        await message.answer(agent_config.summary_html(), parse_mode="HTML")
        return
    if tokens[0].lower() in ("padrao", "padrão", "reset", "limpar"):
        agent_config.reset()
        await message.answer("✅ Overrides limpos — valores do .env valem de novo.", parse_mode=None)
        return
    if len(tokens) < 2:
        await message.answer(
            "Use: /agente_config <opção> <valor> (ou /agente_config pra ver tudo).",
            parse_mode=None,
        )
        return
    try:
        confirm = agent_config.set(tokens[0], tokens[1])
    except ValueError as e:
        await message.answer(f"❌ {e}", parse_mode=None)
        return
    await message.answer(f"✅ {confirm} (vale já pra próxima tarefa).", parse_mode=None)


# --- continuação conversacional (registrar ANTES do catch-all de chat) ---


def _continuation_gate(message: Message) -> bool:
    return _is_owner(message) and not runner.busy and _active_session_id() is not None


@router.message(F.text & ~F.text.startswith("/"), _continuation_gate)
async def agent_continuation(message: Message, user: User) -> None:
    if not user.is_authorized:
        return
    sid = _active_session_id()
    if sid is None:  # TTL expirou entre o filtro e o handler
        return
    if runner.busy:
        await message.answer(_BUSY_MSG, parse_mode=None)
        return
    asyncio.create_task(
        _run_and_report(message.bot, message.chat.id, message.text or "", sid)
    )
