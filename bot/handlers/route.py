from __future__ import annotations

import html
import logging
import unicodedata

import httpx
from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User
from bot.services.geocoding import GeocodingError, geocode
from bot.services.route_pending import pending_routes
from bot.services.traffic import (
    USER_AGENT,
    TrafficError,
    fetch_traffic_with_alternative,
    format_traffic_message_dual,
)

logger = logging.getLogger(__name__)

router = Router(name="route")

_USAGE = (
    "Uso: /rota &lt;destino&gt;\n"
    "Ex: /rota casa  |  /rota trabalho  |  /rota Avenida Paulista 1000"
)
_MISSING_CONFIG = (
    "⚠️ Rota não está configurada. Verifique GOOGLE_MAPS_API_KEY (e, para "
    "atalhos, HOME_COORDS / WORK_COORDS) no .env."
)
_BTN_SEND = "📍 Enviar localização"
_BTN_CANCEL = "❌ Cancelar"


def _normalize(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s.strip().casefold())
        if not unicodedata.combining(c)
    )


# Preposições/artigos/possessivos iniciais comuns na fala ("rota PARA casa",
# "de volta PRA casa", "PRO trabalho") — removidos só pra DETECTAR o atalho.
_LEADING_TOKENS = {
    "para", "pra", "pro", "ao", "a", "o", "de", "da", "do", "volta",
    "minha", "meu", "em", "rumo", "ate", "la",
}
_HOME_WORDS = {"casa"}
_WORK_WORDS = {"trabalho", "servico", "escritorio"}


def _strip_leading(norm: str) -> str:
    tokens = norm.split()
    while tokens and tokens[0] in _LEADING_TOKENS:
        tokens.pop(0)
    return " ".join(tokens)


def _resolve_shortcut(arg: str) -> tuple[str, str | None, bool]:
    """Retorna (label, coords, is_shortcut). is_shortcut=True quando arg é o
    atalho casa/trabalho (mesmo se a coord não estiver no .env). Tolera
    preposições da fala ('para casa', 'pro trabalho'). Geocoding (quando NÃO é
    atalho) segue usando o arg original."""
    core = _strip_leading(_normalize(arg))
    if core in _HOME_WORDS:
        return "casa", settings.home_coords, True
    if core in _WORK_WORDS:
        return "trabalho", settings.work_coords, True
    return arg, None, False


def _build_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text=_BTN_SEND, request_location=True),
            KeyboardButton(text=_BTN_CANCEL),
        ]],
        resize_keyboard=True,
        one_time_keyboard=True,
        selective=True,
    )


@router.message(Command("rota"))
async def cmd_rota(
    message: Message, command: CommandObject, user: User, session: AsyncSession
) -> None:
    arg = (command.args or "").strip()
    if not arg:
        await message.answer(_USAGE, parse_mode="HTML")
        return

    if not settings.google_maps_api_key:
        await message.answer(_MISSING_CONFIG)
        return

    label, resolved, is_shortcut = _resolve_shortcut(arg)

    # Se é atalho mas a coord correspondente não está configurada, falha cedo.
    if is_shortcut and resolved is None:
        await message.answer(
            f"⚠️ {label.upper()}_COORDS não configurado no .env."
        )
        return

    pending_routes.put(
        user_id=user.id,
        label=label,
        raw_query=arg,
        resolved_coords=resolved,
    )
    await message.answer(
        f"📍 Toque para enviar sua localização e ver a rota até "
        f"<b>{html.escape(label)}</b>.",
        parse_mode="HTML",
        reply_markup=_build_keyboard(),
    )


@router.message(F.location)
async def on_location(
    message: Message, user: User, session: AsyncSession
) -> None:
    pending = pending_routes.pop(user.id)
    if pending is None:
        await message.answer(
            "Localização recebida, mas não havia /rota pendente "
            "(ou já expirou). Use /rota &lt;destino&gt; primeiro.",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    if not message.location:
        return
    user_coords = f"{message.location.latitude},{message.location.longitude}"
    api_key = settings.google_maps_api_key.get_secret_value()

    # Resolve destino: pré-resolvido (casa/trabalho) ou geocode com viewport bias.
    if pending.resolved_coords:
        dest_coords = pending.resolved_coords
        dest_label = pending.label
    else:
        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                hit = await geocode(client, api_key, pending.raw_query,
                                    bias_coords=user_coords)
        except GeocodingError:
            logger.exception("/rota geocoding failed")
            await message.answer(
                "⚠️ Erro consultando o Google Geocoding. Verifique se a API "
                "está habilitada no Cloud Console e se a chave permite "
                "Geocoding (logs do servidor têm o detalhe).",
                reply_markup=ReplyKeyboardRemove(),
            )
            return

        if hit is None:
            await message.answer(
                f"🤷 Não encontrei <b>{html.escape(pending.raw_query)}</b>. "
                f"Tente um endereço mais específico.",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        dest_coords = hit.coords
        dest_label = hit.formatted_address

    # "Melhor horário pra sair": varre a próxima 1h a partir de ONDE o usuário
    # está (o pin que acabou de chegar), em vez de assumir casa.
    if pending.kind == "melhor_horario":
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        from bot.services.departure import parse_arrive_by, plan_departure
        now = _dt.now(ZoneInfo(user.timezone))
        arrive_by = parse_arrive_by(pending.arrive_by_raw, now) if pending.arrive_by_raw else None
        if arrive_by is not None and arrive_by <= now:
            arrive_by = None  # alvo já passou → cai no modo 'sair em breve'
        try:
            async with httpx.AsyncClient(
                timeout=25.0, follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                html_out = await plan_departure(
                    client, api_key, user_coords, dest_coords,
                    now=now, arrive_by=arrive_by,
                    origin_label="sua localização", dest_label=dest_label,
                )
        except Exception:
            logger.exception("melhor_horario directions failed")
            html_out = None
        if not html_out:
            await message.answer(
                "⚠️ Não consegui prever o trânsito agora. Tente de novo em alguns minutos.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return
        try:
            await message.answer(
                html_out, parse_mode="HTML", disable_web_page_preview=True,
                reply_markup=ReplyKeyboardRemove(),
            )
        except Exception:
            logger.exception("HTML send failed in melhor_horario")
            await message.answer(
                html_out, parse_mode=None, disable_web_page_preview=True,
                reply_markup=ReplyKeyboardRemove(),
            )
        return

    # Calcula rota + alternativa do Google (mostra as duas, ⭐ na mais rápida).
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            pref, alt = await fetch_traffic_with_alternative(
                client, api_key, user_coords, dest_coords, [],
            )
    except TrafficError:
        logger.exception("/rota directions failed")
        await message.answer(
            "⚠️ Não consegui calcular a rota agora. Tente de novo em alguns minutos.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    text = format_traffic_message_dual(pref, alt, f"até {dest_label}")
    try:
        await message.answer(
            text, parse_mode="HTML", disable_web_page_preview=True,
            reply_markup=ReplyKeyboardRemove(),
        )
    except Exception:
        logger.exception("HTML send failed in /rota")
        await message.answer(
            text, parse_mode=None, disable_web_page_preview=True,
            reply_markup=ReplyKeyboardRemove(),
        )


@router.message(F.text == _BTN_CANCEL)
async def on_cancel(message: Message, user: User, session: AsyncSession) -> None:
    pending_routes.discard(user.id)
    await message.answer("Cancelado.", reply_markup=ReplyKeyboardRemove())
