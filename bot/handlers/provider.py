from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User
from bot.services.llm import catalog
from bot.services.llm.factory import SUPPORTED_PROVIDERS

router = Router(name=__name__)


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _format_model_list(provider: str) -> str:
    """Lista dinâmica (Models API) dos modelos de chat do provider, em HTML."""
    modelos = await catalog.list_models(provider)
    if not modelos:
        return (
            f"Não consegui listar os modelos de <b>{_html_escape(provider)}</b> "
            "agora (chave ausente ou API indisponível)."
        )
    linhas = [f"<b>Modelos {_html_escape(provider)}:</b>"]
    for mid, nome in modelos:
        if nome and nome != mid:
            linhas.append(f"<code>{_html_escape(mid)}</code> · {_html_escape(nome)}")
        else:
            linhas.append(f"<code>{_html_escape(mid)}</code>")
    return "\n".join(linhas)


# Variantes de modelo do Gemini via /provider gemini <variante>.
_GEMINI_VARIANTS = {
    # Geração nova (3.x) — capacidade separada, sem os 503 de "high demand" do 2.5.
    "3.5": "gemini-3.5-flash",
    "3.5-flash": "gemini-3.5-flash",
    "flash-novo": "gemini-3.5-flash",
    "3.1-pro": "gemini-3.1-pro-preview",
    "pro-novo": "gemini-3.1-pro-preview",
    "3.1-lite": "gemini-3.1-flash-lite",
    # Geração 2.5.
    "pro": "gemini-2.5-pro",
    "flash": "gemini-2.5-flash",
    "flash-lite": "gemini-2.5-flash-lite",
    "lite": "gemini-2.5-flash-lite",
}


async def _is_valid_gemini_id(model_id: str) -> bool:
    """Confere o id contra a lista viva da Models API (aceita modelos novos sem
    mexer no código). Se a API falhar, aceita o id pra não travar o usuário."""
    modelos = await catalog.list_models("gemini")
    if not modelos:
        return True
    return any(mid == model_id for mid, _ in modelos)


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
            "Modelos disponíveis (lista dinâmica da API):\n"
            "• `/provider modelos` (gemini) | `/provider modelos anthropic` | `/provider modelos openai`\n\n"
            "No Gemini dá pra escolher o modelo (alias ou id completo):\n"
            "• geração nova (estável): `/provider gemini 3.5` | `/provider gemini 3.1-pro` | `/provider gemini 3.1-lite`\n"
            "• geração 2.5: `/provider gemini pro` | `/provider gemini flash`\n"
            "• id direto: `/provider gemini gemini-3.5-flash`",
            parse_mode="Markdown",
        )
        return

    # /provider modelos [provider] — lista dinâmica (metadata, não gasta token).
    if tokens[0] in ("modelos", "models", "lista"):
        prov = tokens[1] if len(tokens) > 1 else "gemini"
        if prov not in SUPPORTED_PROVIDERS:
            opts = ", ".join(SUPPORTED_PROVIDERS)
            await message.answer(f"Provider inválido. Opções: {opts}", parse_mode=None)
            return
        await message.answer(await _format_model_list(prov), parse_mode="HTML")
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
        elif variant.startswith("gemini-") and await _is_valid_gemini_id(variant):
            user.gemini_model = variant
        else:
            await message.answer(
                "Variante inválida. Use um alias (pro | flash | flash-lite | 3.5 | "
                "3.1-pro | 3.1-lite) ou um id completo válido "
                "(veja /provider modelos).",
                parse_mode=None,
            )
            return
    user.provider = prov
    await session.commit()

    label = prov
    if prov == "gemini":
        label = f"gemini ({user.gemini_model or settings.gemini_model})"
    await message.answer(f"✅ Provider definido como *{label}*.", parse_mode="Markdown")


_VOICE_STT_PROVIDERS = {"gemini", "openai"}

# Variantes de submodelo Gemini pra STT (mesmos aliases do /provider).
_VOICE_GEMINI_VARIANTS = _GEMINI_VARIANTS


@router.message(Command("voice"))
async def cmd_voice_provider(
    message: Message, command: CommandObject, user: User, session: AsyncSession
) -> None:
    """Troca o provider e/ou submodelo de transcrição de voz (STT) por usuário.

    /voice                        → mostra atual
    /voice gemini                 → provider gemini, submodelo = default do .env
    /voice gemini <variante>      → provider gemini + submodelo específico
                                    (ex.: '/voice gemini lite' = 2.5-flash-lite,
                                          '/voice gemini 3.5'  = 3.5-flash)
    /voice openai                 → Whisper (não tem submodelo aqui)
    /voice padrao                 → limpa ambos, volta ao .env
    """
    tokens = (command.args or "").strip().lower().split()
    if not tokens:
        prov_atual = user.voice_stt_provider or settings.voice_stt_provider
        model_atual = user.voice_stt_model or settings.voice_stt_model
        if prov_atual == "gemini":
            label = f"gemini ({model_atual})"
        else:
            label = prov_atual
        await message.answer(
            f"Transcrição de voz (STT): <b>{label}</b>\n\n"
            "<b>Comandos:</b>\n"
            "<code>/voice</code> · mostra o atual\n"
            "<code>/voice gemini</code> · provider gemini, submodelo do .env\n"
            "<code>/voice gemini &lt;alias&gt;</code> · escolhe submodelo Gemini\n"
            "<code>/voice openai</code> · Whisper / gpt-4o-transcribe\n"
            "<code>/voice padrao</code> · reseta tudo (volta ao .env)\n\n"
            "<b>Aliases do Gemini:</b>\n"
            "<code>3.5</code> · <code>3.5-flash</code> → gemini-3.5-flash\n"
            "<code>3.1-pro</code> → gemini-3.1-pro-preview (tier pago)\n"
            "<code>3.1-lite</code> → gemini-3.1-flash-lite\n"
            "<code>pro</code> → gemini-2.5-pro\n"
            "<code>flash</code> → gemini-2.5-flash\n"
            "<code>lite</code> · <code>flash-lite</code> → gemini-2.5-flash-lite\n\n"
            "<b>Diferenças entre providers:</b>\n"
            "• <b>gemini</b>: multimodal, faz conversão voz→/comando.\n"
            "• <b>openai</b>: transcrição literal (sem atalhos de slash).\n"
            "• Se a cadeia Gemini falhar (503/timeout), o bot cai "
            "automaticamente no Whisper como último recurso.",
            parse_mode="HTML",
        )
        return

    arg = tokens[0]
    variant = tokens[1] if len(tokens) > 1 else None

    if arg in ("padrao", "padrão", "auto", "limpar", "none"):
        user.voice_stt_provider = None
        user.voice_stt_model = None
        await session.commit()
        await message.answer(
            f"✅ STT volta ao default "
            f"(<b>{settings.voice_stt_provider}</b> · {settings.voice_stt_model}).",
            parse_mode="HTML",
        )
        return

    if arg not in _VOICE_STT_PROVIDERS:
        await message.answer(
            "Opção inválida. Use: /voice gemini [variante] | openai | padrao",
            parse_mode=None,
        )
        return

    if arg == "gemini":
        if variant is None:
            user.voice_stt_model = None  # volta ao VOICE_STT_MODEL do .env
        elif variant in _VOICE_GEMINI_VARIANTS:
            user.voice_stt_model = _VOICE_GEMINI_VARIANTS[variant]
        elif variant.startswith("gemini-") and await _is_valid_gemini_id(variant):
            user.voice_stt_model = variant
        else:
            opts = ", ".join(sorted(set(_VOICE_GEMINI_VARIANTS.keys())))
            await message.answer(
                f"Variante inválida. Use um alias ({opts}) ou um id completo "
                "válido (veja /provider modelos).",
                parse_mode=None,
            )
            return
    else:
        # openai não tem submodelo configurável aqui (VOICE_STT_OPENAI_MODEL global).
        if variant is not None:
            await message.answer(
                "OpenAI não aceita submodelo via /voice (use VOICE_STT_OPENAI_MODEL no .env).",
                parse_mode=None,
            )
            return
        # Trocando pra openai: limpa o submodelo Gemini pra não confundir o /voice atual.
        user.voice_stt_model = None

    user.voice_stt_provider = arg
    await session.commit()
    if arg == "gemini":
        label = f"gemini ({user.voice_stt_model or settings.voice_stt_model})"
    else:
        label = arg
    await message.answer(
        f"✅ Transcrição de voz definida como <b>{label}</b>.", parse_mode="HTML",
    )


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
