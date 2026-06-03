"""Cotação de ativos da B3 via brapi.dev.

Usado pela revisão de carteira do agente proativo (última janela do dia):
busca o preço de mercado atual de ações/FIIs/ETFs e atualiza o
`currentPrice` no Firestore (mesmo campo que o app gerenciador-financeiro
usa), pra mostrar valor investido vs valor de mercado e P&L real.

Só B3 (sem cripto). Token gratuito do cadastro em brapi.dev (BRAPI_TOKEN).
"""
from __future__ import annotations

import logging

import httpx

from bot.config import settings

logger = logging.getLogger(__name__)

_BRAPI_URL = "https://brapi.dev/api/quote/{tickers}"
USER_AGENT = "Bot-Telegram-Concierge/1.0"


class QuotesError(Exception):
    pass


async def fetch_quotes(tickers: list[str]) -> dict[str, float]:
    """Retorna {TICKER: preço de mercado atual} pros tickers da B3 dados.

    Pula tickers sem cotação (mantém só os que vieram com preço válido).
    Levanta QuotesError se o token não estiver configurado ou a API falhar
    de forma dura (o chamador trata como 'sem cotação agora').
    """
    clean = sorted({(t or "").strip().upper() for t in tickers if (t or "").strip()})
    if not clean:
        return {}
    if not settings.brapi_token:
        raise QuotesError("BRAPI_TOKEN não configurado")

    token = settings.brapi_token.get_secret_value()
    url = _BRAPI_URL.format(tickers=",".join(clean))
    try:
        async with httpx.AsyncClient(
            timeout=20.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = await client.get(url, params={"token": token})
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        body = (e.response.text or "").strip()[:300]
        raise QuotesError(
            f"brapi HTTP {e.response.status_code}: {body or '(sem corpo)'}"
        ) from e
    except Exception as e:
        raise QuotesError(f"brapi indisponível: {e}") from e

    out: dict[str, float] = {}
    for item in data.get("results") or []:
        sym = (item.get("symbol") or "").strip().upper()
        price = item.get("regularMarketPrice")
        if not sym or price is None:
            continue
        try:
            out[sym] = float(price)
        except (TypeError, ValueError):
            continue
    return out
