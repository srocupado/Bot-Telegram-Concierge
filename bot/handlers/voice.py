"""Handler de mensagens de voz/audio.

Fluxo: baixa o blob, transcreve via Gemini multimodal, e:
- se a transcrição começa com `/`, dispatcha para o handler do comando;
- caso contrário, trata como mensagem de chat livre (igual handlers/chat.py).
"""
from __future__ import annotations

import asyncio
import html
import io
import logging

from aiogram import F, Router
from aiogram.filters import CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User
from bot.handlers.chat import _build_system_prompt
from bot.handlers.congress import (
    cmd_congress_agora,
    cmd_congress_at,
    cmd_congress_off,
    cmd_congress_on,
    cmd_congress_reset,
)
from bot.handlers.agent import (
    cmd_agente,
    cmd_agente_config,
    cmd_agente_fim,
    cmd_agente_parar,
    cmd_agente_status,
)
from bot.handlers.ping import cmd_ping
from bot.handlers.provider import cmd_provider
from bot.handlers.dou_mp import cmd_dou_provider
from bot.handlers.reminders import cmd_lembrar, cmd_lembretes
from bot.handlers.reset import cmd_reset
from bot.handlers.route import cmd_rota
from bot.handlers.search import cmd_buscar
from bot.handlers.start import cmd_help, cmd_start
from bot.handlers.tasks import cmd_feito, cmd_nova, cmd_tarefas
from bot.handlers.traffic import (
    cmd_transito_agora,
    cmd_transito_at,
    cmd_transito_off,
    cmd_transito_on,
    cmd_transito_reset,
)
from bot.services.chat_memory import memory
from bot.services.llm.factory import get_provider
from bot.services.voice import VoiceTranscribeError, transcribe

logger = logging.getLogger(__name__)

router = Router(name="voice")

_TOO_LONG = "⚠️ Áudio muito longo (máx {max}s). Tenta um trecho menor."
_EMPTY = "🤷 Não entendi nada no áudio. Tenta gravar de novo, mais perto do microfone."
_STT_ERROR = "⚠️ Não consegui transcrever o áudio agora. Tenta de novo em alguns segundos."


def _cmd(name: str, args: str | None) -> CommandObject:
    return CommandObject(prefix="/", command=name, args=args or None)


# Wrappers que adaptam as assinaturas heterogêneas dos handlers existentes
# a um contrato único (message, args, user, session).
async def _w_transito_agora(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_transito_agora(message, _cmd("transito_agora", args))


async def _w_transito_on(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_transito_on(message, user, session)


async def _w_transito_off(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_transito_off(message, user, session)


async def _w_transito_at(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_transito_at(message, _cmd("transito_at", args), user, session)


async def _w_transito_reset(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_transito_reset(message, user, session)


async def _w_congresso_agora(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_congress_agora(message)


async def _w_congresso_on(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_congress_on(message, user, session)


async def _w_congresso_off(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_congress_off(message, user, session)


async def _w_congresso_at(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_congress_at(message, _cmd("congresso_at", args), user, session)


async def _w_congresso_reset(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_congress_reset(message, user, session)


async def _w_nova(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_nova(message, _cmd("nova", args), user, session)


async def _w_tarefas(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_tarefas(message, user, session)


async def _w_feito(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_feito(message, _cmd("feito", args), user, session)


async def _w_lembrar(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_lembrar(message, _cmd("lembrar", args), user, session)


async def _w_lembretes(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_lembretes(message, user, session)


async def _w_rota(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_rota(message, _cmd("rota", args), user, session)


async def _w_buscar(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_buscar(message, _cmd("buscar", args), user)


async def _w_ping(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_ping(message, user)


async def _w_provider(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_provider(message, _cmd("provider", args), user, session)


async def _w_dou_provider(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_dou_provider(message, _cmd("dou_provider", args), user, session)


async def _w_reset(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_reset(message)


async def _w_start(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_start(message, user)


async def _w_help(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_help(message)


async def _w_agente(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_agente(message, _cmd("agente", args), user)


async def _w_agente_parar(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_agente_parar(message, user)


async def _w_agente_status(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_agente_status(message, user)


async def _w_agente_fim(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_agente_fim(message, user)


async def _w_agente_config(message: Message, args: str, user: User, session: AsyncSession) -> None:
    await cmd_agente_config(message, _cmd("agente_config", args), user)


_DISPATCH: dict[str, callable] = {
    "transito_agora": _w_transito_agora,
    "transito_on": _w_transito_on,
    "transito_off": _w_transito_off,
    "transito_at": _w_transito_at,
    "transito_reset": _w_transito_reset,
    "congresso_agora": _w_congresso_agora,
    "congresso_on": _w_congresso_on,
    "congresso_off": _w_congresso_off,
    "congresso_at": _w_congresso_at,
    "congresso_reset": _w_congresso_reset,
    "nova": _w_nova,
    "tarefas": _w_tarefas,
    "feito": _w_feito,
    "lembrar": _w_lembrar,
    "lembretes": _w_lembretes,
    "rota": _w_rota,
    "buscar": _w_buscar,
    "ping": _w_ping,
    "provider": _w_provider,
    "dou_provider": _w_dou_provider,
    "reset": _w_reset,
    "start": _w_start,
    "help": _w_help,
    "agente": _w_agente,
    "agente_parar": _w_agente_parar,
    "agente_status": _w_agente_status,
    "agente_fim": _w_agente_fim,
    "agente_config": _w_agente_config,
}


@router.message(F.voice | F.audio)
async def cmd_voice(message: Message, user: User, session: AsyncSession) -> None:
    logger.info(
        "voice received",
        extra={
            "user_id": getattr(user, "id", None),
            "authorized": getattr(user, "is_authorized", None),
            "voice_enabled": settings.voice_enabled,
            "has_voice": message.voice is not None,
            "has_audio": message.audio is not None,
        },
    )
    if not user.is_authorized:
        return
    if not settings.voice_enabled:
        await message.answer("🔇 Mensagens de voz estão desativadas (VOICE_ENABLED=false).")
        return

    voice_obj = message.voice or message.audio
    if voice_obj is None:
        return
    duration = getattr(voice_obj, "duration", 0) or 0
    if duration > settings.voice_max_seconds:
        await message.answer(_TOO_LONG.format(max=settings.voice_max_seconds))
        return

    file_id = voice_obj.file_id
    mime_type = getattr(voice_obj, "mime_type", None) or "audio/ogg"

    try:
        buf = io.BytesIO()
        await asyncio.wait_for(
            message.bot.download(file_id, destination=buf), timeout=30,
        )
        audio_bytes = buf.getvalue()
        logger.info("voice downloaded", extra={"bytes": len(audio_bytes)})
    except asyncio.TimeoutError:
        logger.warning("voice download timed out")
        await message.answer(_STT_ERROR)
        return
    except Exception:
        logger.exception("voice download failed")
        await message.answer(_STT_ERROR)
        return

    stt_provider = user.voice_stt_provider or settings.voice_stt_provider
    stt_model = user.voice_stt_model or settings.voice_stt_model
    try:
        logger.info(
            "voice transcribing",
            extra={"provider": stt_provider, "model": stt_model},
        )
        transcribed = await asyncio.wait_for(
            transcribe(
                audio_bytes, mime_type=mime_type,
                provider=stt_provider, model=stt_model,
            ),
            timeout=90,
        )
        logger.info("voice transcribe done", extra={"len": len(transcribed)})
    except asyncio.TimeoutError:
        logger.warning("voice transcribe timed out (model=%s)", stt_model)
        await message.answer(_STT_ERROR)
        return
    except VoiceTranscribeError:
        logger.exception("voice transcribe failed")
        await message.answer(_STT_ERROR)
        return

    transcribed = transcribed.strip()
    if not transcribed:
        await message.answer(_EMPTY)
        return

    is_command = transcribed.startswith("/")
    logger.info(
        "voice transcribed",
        extra={
            "duration_s": duration,
            "transcribed_len": len(transcribed),
            "is_command": is_command,
        },
    )

    # Echo do que foi transcrito (transparência)
    await message.answer(
        f"🎤 <i>{html.escape(transcribed)}</i>",
        parse_mode="HTML",
    )

    if is_command:
        await _dispatch_command(message, user, session, transcribed)
    else:
        await _dispatch_chat(message, user, session, transcribed)


async def _dispatch_command(
    message: Message, user: User, session: AsyncSession, text: str
) -> None:
    parts = text.lstrip("/").split(None, 1)
    if not parts:
        return
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    handler = _DISPATCH.get(cmd)
    if handler is None:
        await message.answer(f"❌ Comando /{cmd} não reconhecido.")
        return

    try:
        await handler(message, args, user, session)
    except Exception:
        logger.exception("voice command dispatch failed: /%s", cmd)
        await message.answer(f"❌ Erro ao executar /{cmd}.")


async def _dispatch_chat(
    message: Message, user: User, session: AsyncSession, text: str
) -> None:
    """Mesma lógica do handlers/chat.py::free_chat, adaptada para texto vindo de voz."""
    from bot.services.llm.base import ToolContext
    from bot.services.tools import TOOLS

    # Sessão de continuação do agente ativa? Voz livre continua a tarefa
    # (mesmo comportamento do texto livre, que é capturado pelo router do
    # agente antes do catch-all).
    from bot.handlers.agent import try_continuation
    if try_continuation(message, user, text):
        return

    chat_id = message.chat.id
    history = memory.get(chat_id)  # já retorna cópia
    history.append({"role": "user", "content": text})

    try:
        provider = get_provider(user.provider, gemini_model=user.gemini_model)
        ctx = ToolContext(user=user, session=session, tz=user.timezone)
        reply = await provider.chat_with_tools(
            history, tools=TOOLS, ctx=ctx,
            system=_build_system_prompt(user.timezone),
            max_tokens=600,
        )
    except Exception as e:
        logger.exception("voice→chat failed")
        await message.answer(
            f"❌ erro no LLM ({user.provider}): {e}", parse_mode=None,
        )
        return

    from bot.services.finance_guard import guard_financial_reply
    reply = guard_financial_reply(text, ctx.financial_logged_ok, reply)

    if ctx.direct_html:
        memory.append(chat_id, "user", text)
        memory.append(chat_id, "assistant", ctx.direct_html)
        loc_kb = None
        if ctx.request_location:
            from bot.handlers.route import _build_keyboard
            loc_kb = _build_keyboard()
        try:
            await message.answer(
                ctx.direct_html, parse_mode="HTML", disable_web_page_preview=True,
                reply_markup=loc_kb,
            )
        except Exception:
            await message.answer(
                ctx.direct_html, parse_mode=None, disable_web_page_preview=True,
                reply_markup=loc_kb,
            )
        return

    # O Gemini às vezes volta texto vazio após uma tool call; se a tool deixou
    # um texto pronto, usa ele em vez de "(sem resposta)".
    if not (reply or "").strip() and ctx.fallback_text:
        reply = ctx.fallback_text

    memory.append(chat_id, "user", text)
    memory.append(chat_id, "assistant", reply)
    kb = None
    if ctx.dou_mp_found:
        from bot.handlers.dou_mp import nota_keyboard
        kb = nota_keyboard(ctx.dou_mp_found["date_iso"])
    elif ctx.confirm_clear_shopping:
        from bot.handlers.shopping import clear_keyboard
        kb = clear_keyboard()
    from bot.handlers.chat import answer_llm
    await answer_llm(message, reply, reply_markup=kb)
