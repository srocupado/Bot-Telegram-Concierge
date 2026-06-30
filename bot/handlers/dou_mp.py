"""Comandos do monitor de MPs no Diário Oficial (Inlabs/DOU).

/mp_dou_on  /mp_dou_off  — assina/desassina o digest diário (18h BRT).
/mp_dou_agora [AAAA-MM-DD] — força a busca de hoje (ou data dada).
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User
from bot.services.dou_monitor import DouError, deliver_to_user

logger = logging.getLogger(__name__)
router = Router(name="dou_mp")

# Aliases de modelo Claude pra nota técnica (/dou_provider anthropic <alias>).
# sonnet é o recomendado: qualidade alta a ~metade do custo do opus.
_ANTHROPIC_VARIANTS = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
}


async def _list_claude_models() -> list[tuple[str, str]]:
    """[(id, display_name)] dos modelos Claude via Models API — DINÂMICO, então
    modelos novos (ex.: claude-sonnet-5) aparecem sozinhos sem mexer no código.
    [] se faltar ANTHROPIC_API_KEY ou a API falhar."""
    if not settings.anthropic_api_key:
        return []
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        out: list[tuple[str, str]] = []
        async for m in client.models.list():
            mid = getattr(m, "id", "") or ""
            if mid.startswith("claude-"):
                out.append((mid, getattr(m, "display_name", "") or mid))
        return out
    except Exception:
        logger.exception("dou: falha ao listar modelos Anthropic")
        return []


def nota_keyboard(date_iso: str, numeros: list[str] | None = None) -> InlineKeyboardMarkup:
    """Botões Sim/Não pra oferecer a nota técnica das MPs de uma data.

    Se `numeros` for dado, a nota cobre SOMENTE essas MPs (os números detectados
    naquela notificação) — evita regerar todas as MPs do dia quando uma janela
    posterior avisa de uma MP nova. callback_data tem teto de 64 bytes; se a
    lista não couber, cai pro modo data (todas as MPs do dia)."""
    cb = f"doump:y:{date_iso}"
    if numeros:
        candidate = f"{cb}:{','.join(numeros)}"
        if len(candidate.encode()) <= 64:
            cb = candidate
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📄 Sim, gerar nota", callback_data=cb),
        InlineKeyboardButton(text="Não", callback_data="doump:n"),
    ]])


@router.callback_query(F.data == "doump:n")
async def cb_nota_nao(query: CallbackQuery, user: User) -> None:
    await query.answer("Ok, sem nota.")
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("doump:y:"))
async def cb_nota_sim(query: CallbackQuery, user: User, session: AsyncSession) -> None:
    """callback_data = 'doump:y:<AAAA-MM-DD>' (todas as MPs do dia) ou
    'doump:y:<AAAA-MM-DD>:<num1,num2,...>' (só essas MPs — vindo do proativo,
    que avisa de um subconjunto e não deve regerar o dia todo)."""
    if not user.is_authorized:
        await query.answer()
        return
    try:
        payload = query.data.split(":", 2)[2]  # "<data>" ou "<data>:<nums>"
        date_part, _, nums_part = payload.partition(":")
        target = date.fromisoformat(date_part)
        only_numeros = [n for n in nums_part.split(",") if n] or None
    except (ValueError, IndexError):
        await query.answer("⚠️ data inválida", show_alert=True)
        return
    await query.answer("Gerando a nota técnica…")
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    try:
        n = await deliver_to_user(query.bot, session, user, target,
                                  force=True, only_numeros=only_numeros)
    except DouError as e:
        await query.message.answer(f"⚠️ {e}", parse_mode=None)
        return
    except Exception:
        logger.exception("doump callback failed")
        await query.message.answer("⚠️ Erro ao gerar a nota técnica.", parse_mode=None)
        return
    if n == 0:
        await query.message.answer("Nenhuma MP encontrada nessa data.", parse_mode=None)


@router.message(Command("dou_provider"))
async def cmd_dou_provider(
    message: Message, command: CommandObject, user: User, session: AsyncSession
) -> None:
    """Escolhe o motor da nota técnica por usuário (vale na agendada e na
    on-demand). Sem args mostra o atual.

    /dou_provider                   → mostra atual
    /dou_provider anthropic         → Claude, modelo do .env (ANTHROPIC_MODEL)
    /dou_provider anthropic <alias> → Claude + modelo (sonnet | opus)
    /dou_provider gemini            → gemini, modelo do .env
    /dou_provider gemini <alias>    → gemini + modelo específico
    /dou_provider <alias>           → atalho: gemini-* assume gemini;
                                      sonnet/opus assumem anthropic
    /dou_provider padrao            → volta ao .env
    """
    from bot.handlers.provider import _GEMINI_VARIANTS

    if not user.is_authorized:
        return
    tokens = (command.args or "").strip().lower().split()

    if not tokens:
        prov = user.dou_mp_provider or settings.dou_mp_provider
        if prov == "gemini":
            label = f"gemini ({user.dou_mp_model or settings.dou_mp_gemini_model})"
        else:
            label = f"anthropic ({user.dou_mp_model or settings.anthropic_model})"
        gem_aliases = ", ".join(sorted(set(_GEMINI_VARIANTS)))
        ant_aliases = ", ".join(sorted(_ANTHROPIC_VARIANTS))
        await message.answer(
            f"Motor da nota técnica: <b>{label}</b>\n"
            f"Fallback gemini (fixo no .env): <code>{settings.dou_mp_gemini_model_fallback}</code>\n\n"
            "<b>Comandos:</b>\n"
            "<code>/dou_provider anthropic [alias]</code> · Claude (web_search)\n"
            "<code>/dou_provider gemini [alias]</code> · Gemini\n"
            "<code>/dou_provider &lt;alias&gt;</code> · atalho (infere o provider)\n"
            "<code>/dou_provider modelos</code> · lista os modelos Claude da API\n"
            "<code>/dou_provider anthropic &lt;id&gt;</code> · qualquer id (ex.: claude-sonnet-5)\n"
            "<code>/dou_provider padrao</code> · volta ao .env\n\n"
            f"<b>Aliases Gemini:</b> {gem_aliases}\n"
            f"<b>Aliases Claude:</b> {ant_aliases} <i>(ou use o id completo — veja /dou_provider modelos)</i>",
            parse_mode="HTML",
        )
        return

    if tokens[0] in ("modelos", "modelo", "listar", "models", "list"):
        modelos = await _list_claude_models()
        if not modelos:
            await message.answer(
                "Não consegui listar os modelos Claude agora "
                "(sem ANTHROPIC_API_KEY ou API fora).", parse_mode=None,
            )
            return
        atual = user.dou_mp_model or settings.anthropic_model
        linhas = [
            f"• <code>{mid}</code> — {nome}" + (" ⬅️ atual" if mid == atual else "")
            for mid, nome in modelos
        ]
        await message.answer(
            "<b>Modelos Claude disponíveis</b> (direto da API):\n" + "\n".join(linhas)
            + "\n\nPra usar na nota: <code>/dou_provider anthropic &lt;id&gt;</code>",
            parse_mode="HTML",
        )
        return

    arg = tokens[0]
    variant = tokens[1] if len(tokens) > 1 else None

    if arg in ("padrao", "padrão", "auto", "limpar", "none"):
        user.dou_mp_provider = None
        user.dou_mp_model = None
        await session.commit()
        await message.answer(
            f"✅ Nota técnica volta ao default do .env "
            f"(<b>{settings.dou_mp_provider}</b> · {settings.dou_mp_gemini_model}).",
            parse_mode="HTML",
        )
        return

    if arg == "anthropic":
        if variant is None:
            user.dou_mp_model = None  # volta ao ANTHROPIC_MODEL do .env
        elif variant in _ANTHROPIC_VARIANTS:
            user.dou_mp_model = _ANTHROPIC_VARIANTS[variant]
        elif variant.startswith("claude-"):
            # id completo (ex.: claude-sonnet-5) — valida na Models API quando
            # possível; se a lista vier vazia (sem chave), aceita e a API valida no uso.
            ids = {m[0] for m in await _list_claude_models()}
            if ids and variant not in ids:
                await message.answer(
                    f"Modelo <code>{variant}</code> não está na lista da API. "
                    "Veja <code>/dou_provider modelos</code>.", parse_mode="HTML",
                )
                return
            user.dou_mp_model = variant
        else:
            opts = ", ".join(sorted(_ANTHROPIC_VARIANTS))
            await message.answer(
                f"Use um alias ({opts}), um id <code>claude-…</code>, ou "
                "<code>/dou_provider modelos</code> pra listar.", parse_mode="HTML",
            )
            return
        user.dou_mp_provider = "anthropic"
        await session.commit()
        await message.answer(
            f"✅ Nota técnica via <b>anthropic ({user.dou_mp_model or settings.anthropic_model})</b> "
            "(Claude + web_search).",
            parse_mode="HTML",
        )
        return

    if arg == "gemini":
        if variant is None:
            user.dou_mp_model = None  # volta ao DOU_MP_GEMINI_MODEL do .env
        elif variant in _GEMINI_VARIANTS:
            user.dou_mp_model = _GEMINI_VARIANTS[variant]
        else:
            opts = ", ".join(sorted(set(_GEMINI_VARIANTS)))
            await message.answer(f"Alias Gemini inválido. Opções: {opts}", parse_mode=None)
            return
        user.dou_mp_provider = "gemini"
        await session.commit()
        await message.answer(
            f"✅ Nota técnica via <b>gemini ({user.dou_mp_model or settings.dou_mp_gemini_model})</b>.",
            parse_mode="HTML",
        )
        return

    # Atalhos de uma palavra: sonnet/opus → anthropic; gemini-* → gemini.
    if arg in _ANTHROPIC_VARIANTS:
        user.dou_mp_provider = "anthropic"
        user.dou_mp_model = _ANTHROPIC_VARIANTS[arg]
        await session.commit()
        await message.answer(
            f"✅ Nota técnica via <b>anthropic ({user.dou_mp_model})</b> (Claude + web_search).",
            parse_mode="HTML",
        )
        return

    if arg in _GEMINI_VARIANTS:
        user.dou_mp_provider = "gemini"
        user.dou_mp_model = _GEMINI_VARIANTS[arg]
        await session.commit()
        await message.answer(
            f"✅ Nota técnica via <b>gemini ({user.dou_mp_model})</b>.",
            parse_mode="HTML",
        )
        return

    await message.answer(
        "Opção inválida. Use: /dou_provider anthropic [sonnet|opus] | "
        "gemini [alias] | padrao",
        parse_mode=None,
    )


@router.message(Command("mp_dou_on"))
async def cmd_on(message: Message, user: User, session: AsyncSession) -> None:
    if not user.is_authorized:
        return
    user.dou_mp_subscribed = True
    await session.commit()
    await message.answer(
        f"✅ Monitor de MPs no DOU ativado. Você recebe as MPs novas "
        f"todo dia às {settings.dou_mp_hour:02d}h (se houver). "
        "Use /mp_dou_agora pra checar agora.",
        parse_mode=None,
    )


@router.message(Command("mp_dou_off"))
async def cmd_off(message: Message, user: User, session: AsyncSession) -> None:
    if not user.is_authorized:
        return
    user.dou_mp_subscribed = False
    await session.commit()
    await message.answer("🔕 Monitor de MPs no DOU desativado.", parse_mode=None)


@router.message(Command("mp_dou_agora"))
async def cmd_agora(
    message: Message, command: CommandObject, user: User, session: AsyncSession,
) -> None:
    if not user.is_authorized:
        return
    arg = (command.args or "").strip()
    if arg:
        try:
            target = date.fromisoformat(arg)
        except ValueError:
            await message.answer("Data inválida. Use AAAA-MM-DD.", parse_mode=None)
            return
    else:
        target = datetime.now(ZoneInfo(user.timezone)).date()

    await message.answer(
        f"🔎 Buscando MPs publicadas no DOU em {target.strftime('%d/%m/%Y')}…",
        parse_mode=None,
    )
    try:
        n = await deliver_to_user(message.bot, session, user, target, force=True)
    except DouError as e:
        await message.answer(f"⚠️ {e}", parse_mode=None)
        return
    except Exception:
        logger.exception("mp_dou_agora failed")
        await message.answer("⚠️ Erro ao consultar o DOU.", parse_mode=None)
        return
    if n == 0:
        await message.answer(
            "Nenhuma MP encontrada no DOU nessa data.",
            parse_mode=None,
        )
