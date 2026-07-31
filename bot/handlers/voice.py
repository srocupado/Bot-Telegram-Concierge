"""Handler de mensagens de voz/audio.

Fluxo: baixa o blob, transcreve via Gemini multimodal, e:
- se a transcrição começa com `/`, dispatcha para o handler do comando;
- caso contrário, trata como mensagem de chat livre (igual handlers/chat.py).
"""
from __future__ import annotations

import asyncio
import html
import inspect
import io
import logging
import re
from typing import Any, Callable

from aiogram import F, Router
from aiogram.filters import CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User
from bot.handlers.chat import _build_system_prompt, inject_context
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
from bot.handlers.financeiro import cmd_setup as cmd_financeiro_setup
from bot.handlers.ping import cmd_ping
from bot.handlers.proactive import (
    cmd_agora as cmd_proativo_agora,
    cmd_off as cmd_proativo_off,
    cmd_on as cmd_proativo_on,
    cmd_status as cmd_proativo_status,
)
from bot.handlers.provider import cmd_provider, cmd_provider_visao, cmd_voice_provider
from bot.handlers.dou_mp import (
    cmd_agora as cmd_mp_dou_agora,
    cmd_dou_provider,
    cmd_off as cmd_mp_dou_off,
    cmd_on as cmd_mp_dou_on,
)
from bot.handlers.translator import cmd_tradutor, cmd_tradutor_provider
from bot.handlers.reminders import (
    cmd_agendar_comando,
    cmd_apagar_lembrete,
    cmd_lembrar,
    cmd_lembretes,
)
from bot.handlers.reset import cmd_reset, cmd_reset_memoria
from bot.handlers.route import cmd_rota
from bot.handlers.search import cmd_buscar
from bot.handlers.start import cmd_help, cmd_start
from bot.handlers.tasks import cmd_feito, cmd_nova, cmd_tarefas
from bot.handlers.traffic import (
    cmd_transito_agora,
    cmd_transito_alerta_off,
    cmd_transito_alerta_on,
    cmd_transito_at,
    cmd_transito_off,
    cmd_transito_on,
    cmd_transito_reset,
)
from bot.handlers.upload import cmd_arquivos
from bot.handlers.viagem import cmd_viagem
from bot.services.chat_memory import memory
from bot.services.llm.factory import get_provider_for_user
from bot.services.voice import VoiceTranscribeError, transcribe
from bot.services.viagem import effective_tz

logger = logging.getLogger(__name__)

router = Router(name="voice")

_TOO_LONG = "⚠️ Áudio muito longo (máx {max}s). Tenta um trecho menor."
_EMPTY = "🤷 Não entendi nada no áudio. Tenta gravar de novo, mais perto do microfone."
_STT_ERROR = "⚠️ Não consegui transcrever o áudio agora. Tenta de novo em alguns segundos."

# Download do áudio no Telegram. O PRIMEIRO download após um restart do
# container é lento (warm-up de conexão/DNS/TLS no Orange Pi): já vi 29s, na
# beira do antigo teto de 30s. Teto folgado + 1 retry cobrem o cold start sem
# travar o caminho quente (que baixa em ~1-2s).
_DL_TIMEOUT = 60
_DL_ATTEMPTS = 2

# "cem" e "sem" são HOMÓFONOS em PT-BR (ambos /sẽj̃/) — nenhum STT distingue
# pelo som, é decisão de contexto. Num comando do bot "sem reais" é quase
# sempre "cem reais" (ninguém compra "sem reais"). Corrige só nesse contexto
# de moeda, preservando "sem" legítimo ("sem açúcar", "sem glúten").
_SEM_REAIS_RE = re.compile(r"\bsem(\s+mil)?\s+(reais|real)\b", re.IGNORECASE)


def _fix_currency_homophones(text: str) -> str:
    return _SEM_REAIS_RE.sub(lambda m: "cem" + (m.group(1) or "") + " reais", text)


def _cmd(name: str, args: str | None) -> CommandObject:
    return CommandObject(prefix="/", command=name, args=args or None)


# Comandos executáveis POR VOZ: nome falado → handler real.
#
# Antes cada entrada exigia um wrapper escrito à mão (as assinaturas dos
# handlers são heterogêneas: uns querem `command`, outros `user`/`session`) — e
# o custo disso apareceu: 17 comandos NUNCA foram adicionados (viagem,
# mp_dou_*, proativo*, reset_memoria, arquivos, apagar_lembrete,
# agendar_comando, provider_visao, voice, transito_alerta_*, financeiro_setup)
# e respondiam "❌ comando não reconhecido" por voz, contrariando o help; e o
# wrapper do /reset chamava o handler SEM user/session, então /reset por voz
# sempre estourava. Agora os argumentos são montados por inspeção da
# assinatura (`_invocar`) e adicionar comando é uma linha aqui.
# tests/test_voice_dispatch.py falha se algum Command() dos handlers ficar de
# fora — a lacuna não volta em silêncio.
_DISPATCH: dict[str, Callable[..., Any]] = {
    "transito_agora": cmd_transito_agora,
    "transito_on": cmd_transito_on,
    "transito_off": cmd_transito_off,
    "transito_at": cmd_transito_at,
    "transito_reset": cmd_transito_reset,
    "transito_alerta_on": cmd_transito_alerta_on,
    "transito_alerta_off": cmd_transito_alerta_off,
    "congresso_agora": cmd_congress_agora,
    "congresso_on": cmd_congress_on,
    "congresso_off": cmd_congress_off,
    "congresso_at": cmd_congress_at,
    "congresso_reset": cmd_congress_reset,
    "nova": cmd_nova,
    "tarefas": cmd_tarefas,
    "feito": cmd_feito,
    "lembrar": cmd_lembrar,
    "lembretes": cmd_lembretes,
    "apagar_lembrete": cmd_apagar_lembrete,
    "agendar_comando": cmd_agendar_comando,
    "rota": cmd_rota,
    "buscar": cmd_buscar,
    "ping": cmd_ping,
    "provider": cmd_provider,
    "provider_visao": cmd_provider_visao,
    "voice": cmd_voice_provider,
    "dou_provider": cmd_dou_provider,
    "mp_dou_agora": cmd_mp_dou_agora,
    "mp_dou_on": cmd_mp_dou_on,
    "mp_dou_off": cmd_mp_dou_off,
    "proativo": cmd_proativo_status,
    "proativo_on": cmd_proativo_on,
    "proativo_off": cmd_proativo_off,
    "proativo_agora": cmd_proativo_agora,
    "tradutor": cmd_tradutor,
    "tradutor_provider": cmd_tradutor_provider,
    "viagem": cmd_viagem,
    "reset": cmd_reset,
    "reset_memoria": cmd_reset_memoria,
    "arquivos": cmd_arquivos,
    "financeiro_setup": cmd_financeiro_setup,
    "start": cmd_start,
    "help": cmd_help,
    "agente": cmd_agente,
    "agente_parar": cmd_agente_parar,
    "agente_status": cmd_agente_status,
    "agente_fim": cmd_agente_fim,
    "agente_config": cmd_agente_config,
}


async def _invocar(
    nome: str, handler: Callable[..., Any], message: Message,
    args: str, user: User, session: AsyncSession,
) -> None:
    """Chama o handler passando só o que a assinatura dele pede.

    Os nomes dos parâmetros são estáveis no projeto inteiro (`message`,
    `command`, `user`, `session`), então a inspeção é confiável — e é ela que
    tira a necessidade de um wrapper por comando."""
    params = inspect.signature(handler).parameters
    kwargs: dict[str, Any] = {}
    if "command" in params:
        kwargs["command"] = _cmd(nome, args)
    if "user" in params:
        kwargs["user"] = user
    if "session" in params:
        kwargs["session"] = session
    await handler(message, **kwargs)


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

    audio_bytes = b""
    for attempt in range(1, _DL_ATTEMPTS + 1):
        try:
            buf = io.BytesIO()
            await asyncio.wait_for(
                message.bot.download(file_id, destination=buf), timeout=_DL_TIMEOUT,
            )
            audio_bytes = buf.getvalue()
            logger.info("voice downloaded", extra={"bytes": len(audio_bytes), "attempt": attempt})
            break
        except asyncio.TimeoutError:
            logger.warning("voice download timed out (tentativa %d/%d)", attempt, _DL_ATTEMPTS)
        except Exception:
            logger.exception("voice download failed (tentativa %d/%d)", attempt, _DL_ATTEMPTS)
        if attempt < _DL_ATTEMPTS:
            await asyncio.sleep(2)
    else:
        # todas as tentativas falharam
        await message.answer(_STT_ERROR)
        return

    # MODO TRADUTOR: se ligado, o áudio é traduzido (texto + voz) em vez de
    # transcrito/roteado. Bypassa o STT normal (que força PT e vira comando).
    if user.translator_lang:
        await _handle_translation(message, user, session, audio_bytes, mime_type)
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

    # Corrige o homófono cem→sem antes do eco e do dispatch (vale só fora de
    # comando — slash não tem "sem reais").
    if not transcribed.startswith("/"):
        transcribed = _fix_currency_homophones(transcribed)

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


# Desligar o tradutor POR VOZ. Com o modo ligado, todo áudio ia pra tradução —
# quem falava "desliga o tradutor" recebia isso traduzido pro inglês e continuava
# preso no modo (só /tradutor off, digitado, saía). Exige a palavra "tradutor"
# + um verbo de desligar numa frase curta, pra não capturar uma tradução legítima
# que por acaso fale de tradutor.
_TRAD_OFF_RE = re.compile(
    r"\b(?:desliga\w*|desativa\w*|encerra\w*|para(?:r)?|sai(?:r)?|fecha\w*|off|stop|fim|cancela\w*)\b",
    re.IGNORECASE,
)
_TRAD_WORD_RE = re.compile(r"\b(?:tradutor|tradu[cç][ãa]o|translator)\b", re.IGNORECASE)


def _pede_desligar_tradutor(texto: str) -> bool:
    t = (texto or "").strip()
    if not t or len(t.split()) > 8:
        return False
    return bool(_TRAD_WORD_RE.search(t) and _TRAD_OFF_RE.search(t))


async def _handle_translation(
    message: Message, user: User, session: AsyncSession,
    audio_bytes: bytes, mime_type: str,
) -> None:
    """Modo tradutor: áudio → {original, tradução} → responde texto + nota de voz."""
    from aiogram.types import BufferedInputFile

    from bot.services.translator import TranslateError, translate_audio
    from bot.services.tts import TTSError, synthesize

    prov = user.translator_tts_provider or settings.translator_tts_provider
    lang = user.translator_lang

    try:
        res = await asyncio.wait_for(
            translate_audio(
                audio_bytes, mime_type, lang,
                provider=prov, model=user.translator_model,
            ),
            timeout=90,
        )
    except asyncio.TimeoutError:
        logger.warning("tradutor: tradução estourou o tempo")
        await message.answer("⚠️ A tradução demorou demais. Tenta um trecho menor.", parse_mode=None)
        return
    except (TranslateError, Exception):
        logger.exception("tradutor: falha na tradução")
        await message.answer("⚠️ Não consegui traduzir o áudio agora. Tenta de novo.", parse_mode=None)
        return

    original = (res.get("original") or "").strip()
    translation = (res.get("translation") or "").strip()

    # "desliga o tradutor" falado vale tanto quanto /tradutor off digitado.
    if _pede_desligar_tradutor(original):
        user.translator_lang = None
        await session.commit()
        await message.answer(
            f"🎙️ Modo tradutor desligado (você disse: “{original}”).\n"
            "Pra ligar de novo: /tradutor <idioma>.",
            parse_mode=None,
        )
        return

    if not translation:
        await message.answer(_EMPTY)
        return

    await message.answer(
        f"🗣️ <i>{html.escape(original)}</i>\n\n🌐 <b>{html.escape(translation)}</b>",
        parse_mode="HTML",
    )

    try:
        ogg = await asyncio.wait_for(synthesize(translation, provider=prov), timeout=60)
        await message.answer_voice(BufferedInputFile(ogg, filename="traducao.ogg"))
    except asyncio.TimeoutError:
        logger.warning("tradutor: TTS estourou o tempo")
        await message.answer("(áudio demorou — segue a tradução em texto acima)", parse_mode=None)
    except (TTSError, Exception):
        logger.exception("tradutor: TTS falhou")
        await message.answer("(áudio indisponível agora — segue a tradução em texto acima)", parse_mode=None)


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
        await _invocar(cmd, handler, message, args, user, session)
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

    # Fast-path determinístico: "liste meus lembretes" vai direto ao banco
    # (mesma saída do /lembretes), sem passar pelo LLM — que às vezes inventava
    # horários ou repetia uma lista velha do contexto.
    from bot.services.reminders import (
        format_pending_list, is_list_reminders_request, list_pending,
    )
    if is_list_reminders_request(text):
        out = format_pending_list(await list_pending(session, user.id), effective_tz(user))
        memory.append(chat_id, "user", text)
        memory.append(chat_id, "assistant", out)
        await message.answer(out, parse_mode=None)
        return

    history = memory.get(chat_id)  # já retorna cópia
    history.append({"role": "user", "content": text})

    from bot.services.memoria import get_summary
    summary = await get_summary(session, user.id)

    try:
        provider = get_provider_for_user(user)
        ctx = ToolContext(user=user, session=session, tz=effective_tz(user), user_text=text)
        reply = await provider.chat_with_tools(
            inject_context(history, effective_tz(user), summary), tools=TOOLS, ctx=ctx,
            system=_build_system_prompt(),
            max_tokens=600,
        )
    except Exception as e:
        logger.exception("voice→chat failed")
        await message.answer(
            f"❌ erro no LLM ({user.provider}): {e}", parse_mode=None,
        )
        return

    from bot.handlers.chat import deliver_llm_reply
    await deliver_llm_reply(message, ctx, reply, user_text=text)
