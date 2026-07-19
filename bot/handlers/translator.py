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
        "Mande um áudio; devolvo a tradução em texto e em voz.\n"
        "Pra sair: /tradutor off",
        parse_mode=None,
    )


@router.message(Command("tradutor_provider"))
async def cmd_tradutor_provider(
    message: Message, command: CommandObject, user: User, session: AsyncSession,
) -> None:
    if not user.is_authorized:
        return
    arg = (command.args or "").strip().lower()

    if not arg:
        cur = user.translator_tts_provider or settings.translator_tts_provider
        await message.answer(
            f"Motor do tradutor: <b>{cur}</b>\n\n"
            "<code>/tradutor_provider openai</code> · Whisper + GPT + TTS "
            "(não treina com seu dado)\n"
            "<code>/tradutor_provider gemini</code> · multimodal + TTS "
            "(tier grátis pode treinar)\n"
            "<code>/tradutor_provider padrao</code> · volta ao .env",
            parse_mode="HTML",
        )
        return

    if arg in ("padrao", "padrão", "default", "reset"):
        user.translator_tts_provider = None
        await session.commit()
        await message.answer(
            f"Motor do tradutor: padrão do .env ({settings.translator_tts_provider}).",
            parse_mode=None,
        )
        return

    if arg not in _PROVIDERS:
        await message.answer(
            "Use: /tradutor_provider openai | gemini | padrao", parse_mode=None,
        )
        return

    if arg == "openai" and not settings.openai_api_key:
        await message.answer(
            "⚠️ OPENAI_API_KEY não configurada — não dá pra usar openai.", parse_mode=None,
        )
        return
    if arg == "gemini" and not settings.gemini_api_key:
        await message.answer(
            "⚠️ GEMINI_API_KEY não configurada — não dá pra usar gemini.", parse_mode=None,
        )
        return

    user.translator_tts_provider = arg
    await session.commit()
    await message.answer(f"Motor do tradutor: <b>{arg}</b>.", parse_mode="HTML")
