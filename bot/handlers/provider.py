from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User
from bot.services.llm.factory import SUPPORTED_PROVIDERS

router = Router(name=__name__)

# Variantes de modelo do Gemini via /provider gemini <variante>.
_GEMINI_VARIANTS = {
    "pro": "gemini-2.5-pro",
    "flash": "gemini-2.5-flash",
    "flash-lite": "gemini-2.5-flash-lite",
    "lite": "gemini-2.5-flash-lite",
}


@router.message(Command("provider"))
async def cmd_provider(
    message: Message, command: CommandObject, user: User, session: AsyncSession
) -> None:
    tokens = (command.args or "").strip().lower().split()
    if not tokens:
        opts = " | ".join(SUPPORTED_PROVIDERS)
        atual = user.provider
        if atual == "gemini":
            atual += f" ({user.gemini_model or settings.gemini_model})"
        await message.answer(
            f"Provider atual: *{atual}*\n\nUse: /provider {opts}\n"
            "No Gemini dá pra escolher o modelo: `/provider gemini pro` | `/provider gemini flash`",
            parse_mode="Markdown",
        )
        return

    prov = tokens[0]
    variant = tokens[1] if len(tokens) > 1 else None
    if prov not in SUPPORTED_PROVIDERS:
        opts = ", ".join(SUPPORTED_PROVIDERS)
        await message.answer(f"Provider inválido. Opções: {opts}")
        return

    if prov == "gemini":
        if variant is None:
            user.gemini_model = None  # volta ao GEMINI_MODEL do .env
        elif variant in _GEMINI_VARIANTS:
            user.gemini_model = _GEMINI_VARIANTS[variant]
        else:
            await message.answer(
                "Variante inválida. Use: /provider gemini pro | flash | flash-lite",
                parse_mode=None,
            )
            return
    user.provider = prov
    await session.commit()

    label = prov
    if prov == "gemini":
        label = f"gemini ({user.gemini_model or settings.gemini_model})"
    await message.answer(f"✅ Provider definido como *{label}*.", parse_mode="Markdown")


@router.message(Command("provider_visao"))
async def cmd_provider_visao(
    message: Message, command: CommandObject, user: User, session: AsyncSession
) -> None:
    """Override só pra entrada de imagens (foto). 'auto' / vazio = limpa override."""
    arg = (command.args or "").strip().lower()
    if not arg:
        current = user.vision_provider or "(seguindo /provider)"
        opts = " | ".join(SUPPORTED_PROVIDERS)
        await message.answer(
            f"Provider de visão: <b>{current}</b>\n\n"
            f"Use: /provider_visao {opts} | auto",
            parse_mode="HTML",
        )
        return
    if arg in ("auto", "none", "padrao", "padrão", "limpar"):
        user.vision_provider = None
        await session.commit()
        await message.answer("✅ Visão volta a seguir o /provider atual.", parse_mode=None)
        return
    if arg not in SUPPORTED_PROVIDERS:
        opts = ", ".join(SUPPORTED_PROVIDERS)
        await message.answer(
            f"Provider inválido. Opções: {opts} | auto", parse_mode=None,
        )
        return
    user.vision_provider = arg
    await session.commit()
    await message.answer(
        f"✅ Provider de visão definido como <b>{arg}</b>.", parse_mode="HTML",
    )
