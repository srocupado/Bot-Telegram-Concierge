"""Modo viagem (/viagem): destino + período por usuário.

Durante a viagem: clima do briefing vira o do destino, lembretes e janelas do
proativo passam a rodar no FUSO local, e (opcional) a cotação da moeda local
entra no briefing. Fora do período, tudo volta ao normal sozinho — os campos
ficam gravados mas `viagem_ativa()` é falsa.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from bot.config import settings
from bot.db.models import User

logger = logging.getLogger(__name__)


class ViagemError(Exception):
    pass


_PERIODO_RE = re.compile(
    r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\s*(?:a|até|ate|-|—)\s*"
    r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?",
    re.IGNORECASE,
)


def parse_periodo(texto: str, hoje: date) -> tuple[date, date, str] | None:
    """Extrai (inicio, fim, resto_sem_periodo) de '… 22/08 a 27/08 …'.
    Sem ano → próximo período que faz sentido (ano atual; rola pro seguinte se
    o FIM já passou). None se não achar o padrão."""
    m = _PERIODO_RE.search(texto)
    if not m:
        return None
    d1, m1, y1, d2, m2, y2 = m.groups()

    def _year(y: str | None) -> int | None:
        if not y:
            return None
        n = int(y)
        return n + 2000 if n < 100 else n

    try:
        ano1 = _year(y1) or hoje.year
        ano2 = _year(y2) or ano1
        ini = date(ano1, int(m1), int(d1))
        fim = date(ano2, int(m2), int(d2))
        if fim < ini:  # "20/12 a 05/01" cruza o ano
            fim = date(fim.year + 1, fim.month, fim.day)
        if not y1 and fim < hoje:  # período sem ano já passou → ano que vem
            ini = date(ini.year + 1, ini.month, ini.day)
            fim = date(fim.year + 1, fim.month, fim.day)
    except ValueError:
        return None
    resto = (texto[: m.start()] + " " + texto[m.end():]).strip()
    return ini, fim, resto


def viagem_ativa(user: User, hoje: date | None = None) -> bool:
    """True se hoje está dentro do período da viagem (bordas inclusas)."""
    ini, fim = getattr(user, "viagem_inicio", None), getattr(user, "viagem_fim", None)
    if not ini or not fim:
        return False
    try:
        d_ini, d_fim = date.fromisoformat(ini), date.fromisoformat(fim)
    except ValueError:
        return False
    if hoje is None:
        # "hoje" no fuso da VIAGEM (se houver) — é lá que o dia vira.
        tz = getattr(user, "viagem_tz", None) or user.timezone
        try:
            hoje = datetime.now(ZoneInfo(tz)).date()
        except Exception:
            hoje = datetime.now(ZoneInfo(user.timezone)).date()
    return d_ini <= hoje <= d_fim


def effective_tz(user: User) -> str:
    """Fuso efetivo: o da viagem durante o período; senão o do usuário."""
    if viagem_ativa(user) and getattr(user, "viagem_tz", None):
        return user.viagem_tz
    return user.timezone


def effective_coords(user: User) -> str | None:
    """Coords do destino durante a viagem (pro clima); None fora dela."""
    if viagem_ativa(user) and getattr(user, "viagem_coords", None):
        return user.viagem_coords
    return None


async def _timezone_for_coords(
    client: httpx.AsyncClient, api_key: str, coords: str,
) -> str | None:
    """IANA timezone das coords via Google Time Zone API (mesma chave do Maps)."""
    try:
        r = await client.get(
            "https://maps.googleapis.com/maps/api/timezone/json",
            params={
                "location": coords,
                "timestamp": int(datetime.now().timestamp()),
                "key": api_key,
            },
            timeout=15.0,
        )
        data = r.json()
        if data.get("status") == "OK":
            return data.get("timeZoneId") or None
        logger.warning("timezone api status=%s", data.get("status"))
    except Exception:
        logger.warning("timezone api falhou", exc_info=True)
    return None


async def resolver_destino(destino: str) -> tuple[str | None, str | None, str | None]:
    """(coords, tz_iana, endereco_formatado) do destino via Google.
    Qualquer parte pode vir None (sem chave/da API falhar) — o handler avisa e
    o modo segue com o que tiver."""
    key = settings.google_maps_api_key.get_secret_value() if settings.google_maps_api_key else None
    if not key:
        return None, None, None
    from bot.services.geocoding import geocode
    async with httpx.AsyncClient() as client:
        try:
            hit = await geocode(client, key, destino)
        except Exception:
            logger.warning("viagem: geocode falhou p/ %r", destino, exc_info=True)
            hit = None
        if hit is None:
            return None, None, None
        coords = hit.coords
        tz = await _timezone_for_coords(client, key, coords)
        return coords, tz, hit.formatted_address or destino
