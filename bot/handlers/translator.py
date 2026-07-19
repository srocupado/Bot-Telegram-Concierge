"""Modo tradutor: /tradutor (toggle por idioma) + /tradutor_provider (motor).

Ligado o modo, qualquer áudio enviado é traduzido — resposta em TEXTO e em VOZ —
em vez de transcrito/roteado como comando. O provider governa entendimento E voz
juntos (openai não treina; gemini tier grátis pode). Ver bot/handlers/voice.py.
"""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User
from bot.services.translator import normalize_lang

logger = logging.getLogger(__name__)
router = Router(name="translator")

_PROVIDERS = ("gemini", "openai")
_OFF_WORDS = ("off", "desligar", "desliga", "sair", "parar", "para", "stop", "fim")


@router.message(Command("tradutor"))
async def cmd_tradutor(
    message: Message, command: CommandObject, user: User, session: AsyncSession,
) -> None:
    if not user.is_authorized:
        return
    arg = (command.args or "").strip()

    if not arg:  # status
        if user.translator_lang:
            prov = user.translator_tts_provider or settings.translator_tts_provider
            await message.answer(
                f"🎙️ Modo tradutor LIGADO → {user.translator_lang} (voz: {prov}).\n"
                "Mande um áudio; devolvo texto + voz.\nPra sair: /tradutor off",
                parse_mode=None,
            )
        else:
            await message.answer(
                "Modo tradutor desligado.\n"
                "Ligue com /tradutor <idioma> (ex.: /tradutor inglês).",
                parse_mode=None,
            )
        return

    if arg.lower() in _OFF_WORDS:
        user.translator_lang = None
        await session.commit()
        await message.answer("🎙️ Modo tradutor desligado.", parse_mode=None)
        return

    lang = normalize_lang(arg)
    user.translator_lang = lang
    await session.commit()
    prov = user.translator_tts_provider or settings.translator_tts_provider
    await message.answer(
        f"🎙️ Modo tradutor LIGADO → {lang} (voz: {prov}).\n"
        f"Mande um áudio; devolvo a tradução em texto e em voz.\n"
        f"↔️ Via dupla: áudio já em {lang} (ex.: a resposta do atendente) "
        "volta traduzido pro português.\n"
        "Pra sair: /tradutor off",
        parse_mode=None,
    )


def _cur_model_label(user: User) -> str:
    prov = user.translator_tts_provider or settings.translator_tts_provider
    if user.translator_model:
        return f"{prov} · <code>{user.translator_model}</code>"
    default_id = (
        settings.translator_stt_gemini_model if prov == "gemini"
        else settings.voice_stt_openai_model
    )
    return f"{prov} · {default_id} <i>(default)</i>"


@router.message(Command("tradutor_provider"))
async def cmd_tradutor_provider(
    message: Message, command: CommandObject, user: User, session: AsyncSession,
) -> None:
    if not user.is_authorized:
        return
    # Reuso da maquinaria do /provider (lista viva da Models API + aliases).
    from bot.handlers.provider import _GEMINI_VARIANTS, _format_model_lists

    tokens = (command.args or "").strip().split()

    if not tokens:  # status + ajuda
        await message.answer(
            f"Motor do tradutor: <b>{_cur_model_label(user)}</b>\n\n"
            "<code>/tradutor_provider openai [id]</code> · Whisper+GPT+TTS "
            "(não treina)\n"
            "<code>/tradutor_provider gemini [id]</code> · multimodal+TTS "
            "(tier grátis pode treinar) — ex.: "
            "<code>/tradutor_provider gemini gemini-3.1-flash-lite</code>\n"
            "<code>/tradutor_provider modelos [gemini|openai]</code> · lista "
            "modelos que aceitam áudio (da API)\n"
            "<code>/tradutor_provider padrao</code> · volta ao .env",
            parse_mode="HTML",
        )
        return

    head = tokens[0].lower()

    # /tradutor_provider modelos [provider] — só modelos que ACEITAM ÁUDIO.
    if head in ("modelos", "models", "lista"):
        alvo = tokens[1].lower() if len(tokens) > 1 else None
        provs = [alvo] if alvo in _PROVIDERS else list(_PROVIDERS)
        texto = await _format_model_lists(provs, modality="audio")
        await message.answer(
            texto + "\n\nPra usar: <code>/tradutor_provider &lt;provider&gt; &lt;id&gt;</code>",
            parse_mode="HTML",
        )
        return

    if head in ("padrao", "padrão", "default", "reset"):
        user.translator_tts_provider = None
        user.translator_model = None
        await session.commit()
        await message.answer(
            f"Motor do tradutor: padrão do .env ({settings.translator_tts_provider}).",
            parse_mode=None,
        )
        return

    if head not in _PROVIDERS:
        await message.answer(
            "Use: /tradutor_provider openai|gemini [id] | modelos | padrao",
            parse_mode=None,
        )
        return

    # valida a chave do provider
    if head == "openai" and not settings.openai_api_key:
        await message.answer("⚠️ OPENAI_API_KEY não configurada.", parse_mode=None)
        return
    if head == "gemini" and not settings.gemini_api_key:
        await message.answer("⚠️ GEMINI_API_KEY não configurada.", parse_mode=None)
        return

    # id do modelo (opcional). Sem id → limpa o override (volta ao default).
    model_id: str | None = None
    if len(tokens) > 1:
        raw = tokens[1]
        if head == "gemini":
            model_id = _GEMINI_VARIANTS.get(raw.lower(), raw)
        else:
            model_id = raw
        # valida contra a lista viva de modelos de ÁUDIO (aceita se a API falhar)
        if not await _is_valid_id_audio(head, model_id):
            await message.answer(
                f"⚠️ <code>{model_id}</code> não parece aceitar áudio em "
                f"{head}. Veja <code>/tradutor_provider modelos {head}</code>.",
                parse_mode="HTML",
            )
            return

    user.translator_tts_provider = head
    user.translator_model = model_id
    await session.commit()
    await message.answer(
        f"Motor do tradutor: <b>{_cur_model_label(user)}</b>.", parse_mode="HTML",
    )


async def _is_valid_id_audio(provider: str, model_id: str) -> bool:
    """Confere o id contra a lista viva de modelos que aceitam ÁUDIO. Se a API
    falhar (lista vazia), aceita pra não travar o usuário."""
    from bot.services.llm import catalog
    modelos = await catalog.list_models(provider, "audio")
    if not modelos:
        return True
    return any(mid == model_id for mid, _ in modelos)
