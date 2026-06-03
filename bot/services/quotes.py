"""Cotação de ativos da B3 via brapi.dev.

Usado pela revisão de carteira do agente proativo (última janela do dia):
busca o preço de mercado atual de ações/FIIs/ETFs e atualiza o
`currentPrice` no Firestore (mesmo campo que o app gerenciador-financeiro
usa), pra mostrar valor investido vs valor de mercado e P&L real.

Só B3 (sem cripto). Token gratuito do cadastro em brapi.dev (BRAPI_TOKEN).

NOTA: o plano FREE da brapi limita a 1 ticker por request (erro 400 com
QUOTES_PER_REQUEST_EXCEEDED se multi-ticker). Por isso fazemos N requests
em paralelo (asyncio.gather), uma por ticker. Cota mensal do free (~10k
req/mês) cobre folgado a carteira pessoal — 1 chamada por ativo por dia.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from bot.config import settings

logger = logging.getLogger(__name__)

_BRAPI_URL = "https://brapi.dev/api/quote/{ticker}"
USER_AGENT = "Bot-Telegram-Concierge/1.0"


class QuotesError(Exception):
    pass


async def _fetch_one(
    client: httpx.AsyncClient, ticker: str, token: str,
) -> tuple[str, float | None, str | None]:
    """Cotação de UM ticker. Retorna (ticker, preço_ou_None, erro_ou_None).
    Falha individual não derruba a carteira inteira."""
    try:
        resp = await client.get(
            _BRAPI_URL.format(ticker=ticker), params={"token": token},
        )
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get("results") or []:
                price = item.get("regularMarketPrice")
                if price is not None:
                    try:
                        return ticker, float(price), None
                    except (TypeError, ValueError):
                        pass
            return ticker, None, "resposta sem preço"
        body = (resp.text or "").strip()[:200]
        return ticker, None, f"HTTP {resp.status_code}: {body or '(sem corpo)'}"
    except Exception as e:
        return ticker, None, f"erro: {e}"


async def fetch_quotes(tickers: list[str]) -> dict[str, float]:
    """Retorna {TICKER: preço de mercado atual} pros tickers da B3 dados.

    Faz N requests em paralelo (1 por ticker — limite do plano free da
    brapi). Tickers que falham individualmente são logados como WARNING e
    omitidos do dict de retorno (a carteira segue mostrando os que vieram).
    Só levanta QuotesError se TODOS os tickers falharem.
    """
    clean = sorted({(t or "").strip().upper() for t in tickers if (t or "").strip()})
    if not clean:
        return {}
    if not settings.brapi_token:
        raise QuotesError("BRAPI_TOKEN não configurado")

    token = settings.brapi_token.get_secret_value()
    try:
        async with httpx.AsyncClient(
            timeout=20.0, headers={"User-Agent": USER_AGENT}
        ) as client:
            results = await asyncio.gather(*(
                _fetch_one(client, t, token) for t in clean
            ))
    except Exception as e:
        raise QuotesError(f"brapi indisponível: {e}") from e

    out: dict[str, float] = {}
    failures: list[str] = []
    for ticker, price, err in results:
        if price is not None:
            out[ticker] = price
        else:
            failures.append(f"{ticker} ({err})")
    if failures:
        logger.warning("quotes: %d/%d ticker(s) falharam: %s",
                       len(failures), len(clean), "; ".join(failures))
    if not out:
        raise QuotesError(
            f"todos os {len(clean)} ticker(s) falharam: {'; '.join(failures)}"
        )
    return out
