from __future__ import annotations

import logging
import unicodedata

import httpx
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import User
from bot.services.traffic import (
    USER_AGENT,
    TrafficError,
    fetch_traffic_with_alternative,
    format_traffic_message_dual,
    parse_route_waypoints,
)

logger = logging.getLogger(__name__)

router = Router(name="traffic")

_USAGE = "Uso: /transito_agora casa  (ou)  /transito_agora trabalho"
_MISSING_CONFIG = (
    "⚠️ Trânsito ainda não está configurado. "
    "Veja .env: HOME_COORDS, WORK_COORDS, GOOGLE_MAPS_API_KEY."
)
_FETCH_ERROR = (
    "⚠️ Não consegui calcular o trânsito agora. Tenta de novo em alguns minutos."
)


def _normalize(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s.strip().casefold())
        if not unicodedata.combining(c)
    )


@router.message(Command("transito_on"))
async def cmd_transito_on(message: Message, user: User, session: AsyncSession) -> None:
    user.traffic_subscribed = True
    await session.commit()
    await message.answer(
        f"🚗 Inscrito no resumo diário de trânsito casa→trabalho "
        f"(seg-sex {settings.traffic_hour:02d}:{settings.traffic_minute:02d} BRT)."
    )


@router.message(Command("transito_off"))
async def cmd_transito_off(message: Message, user: User, session: AsyncSession) -> None:
    user.traffic_subscribed = False
    await session.commit()
    await message.answer("🚗 Resumo diário de trânsito cancelado.")


@router.message(Command("transito_alerta_on"))
async def cmd_transito_alerta_on(message: Message, user: User, session: AsyncSession) -> None:
    user.traffic_alert_enabled = True
    await session.commit()
    await message.answer(
        "🚨 Alertas proativos ativados. Avisarei se a rota estiver "
        "≥30% acima da mediana habitual (mínimo 30 min de viagem)."
    )


@router.message(Command("transito_alerta_off"))
async def cmd_transito_alerta_off(message: Message, user: User, session: AsyncSession) -> None:
    user.traffic_alert_enabled = False
    await session.commit()
    await message.answer("🚨 Alertas proativos de trânsito desativados.")


def _parse_hhmm(s: str) -> tuple[int, int] | None:
    parts = s.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


@router.message(Command("transito_at"))
async def cmd_transito_at(
    message: Message, command: CommandObject, user: User, session: AsyncSession
) -> None:
    arg = (command.args or "").strip()
    if not arg:
        user.traffic_hour = None
        user.traffic_minute = None
        await session.commit()
        await message.answer(
            f"⏰ Horário do digest de trânsito voltou pro default "
            f"({settings.traffic_hour:02d}:{settings.traffic_minute:02d} BRT)."
        )
        return
    parsed = _parse_hhmm(arg)
    if parsed is None:
        await message.answer(
            "Uso: /transito_at HH:MM (ex: /transito_at 08:15). "
            "Sem argumento volta pro default."
        )
        return
    user.traffic_hour, user.traffic_minute = parsed
    user.last_traffic_digest_at = None
    await session.commit()
    await message.answer(
        f"⏰ Digest de trânsito agendado para {parsed[0]:02d}:{parsed[1]:02d} BRT. "
        f"Marca de envio de hoje zerada."
    )


@router.message(Command("transito_reset"))
async def cmd_transito_reset(message: Message, user: User, session: AsyncSession) -> None:
    user.last_traffic_digest_at = None
    await session.commit()
    await message.answer(
        "✅ Marca de envio de hoje zerada. No próximo tick o digest sai de novo "
        "(se o horário agendado já passou)."
    )


@router.message(Command("transito_agora"))
async def cmd_transito_agora(message: Message, command: CommandObject) -> None:
    arg = _normalize(command.args or "")
    if arg not in ("casa", "trabalho"):
        await message.answer(_USAGE)
        return

    if not (settings.home_coords and settings.work_coords and settings.google_maps_api_key):
        await message.answer(_MISSING_CONFIG)
        return

    if arg == "trabalho":
        origin = settings.home_coords
        destination = settings.work_coords
        label = "casa → trabalho"
        reverse = False
    else:
        origin = settings.work_coords
        destination = settings.home_coords
        label = "trabalho → casa"
        reverse = True

    api_key = settings.google_maps_api_key.get_secret_value()
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            waypoints: list[str] = []
            if settings.route_google_maps_url:
                waypoints = await parse_route_waypoints(
                    client, settings.route_google_maps_url
                )
                if reverse:
                    waypoints = list(reversed(waypoints))
            pref, alt = await fetch_traffic_with_alternative(
                client,
                api_key,
                origin,
                destination,
                waypoints,
                maps_url=settings.route_google_maps_url or "",
            )
    except TrafficError:
        logger.exception("/transito_agora fetch failed")
        await message.answer(_FETCH_ERROR)
        return

    if alt is not None:
        logger.info(
            "/transito_agora: 2 rotas (pref=%d min via %s, alt=%d min via %s)",
            pref.duration_minutes, pref.summary or "—",
            alt.duration_minutes, alt.summary or "—",
        )
    text = format_traffic_message_dual(pref, alt, label)
    try:
        await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        logger.exception("HTML send failed in /transito_agora")
        await message.answer(text, parse_mode=None, disable_web_page_preview=True)
