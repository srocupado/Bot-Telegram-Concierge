"""Cotação ATUAL sob demanda: B3, câmbio e cripto — sempre dado ao vivo (pra o
agente nunca inventar valor). Fontes:

- Ação/FII/ETF da B3 → brapi.dev (reaproveita quotes.fetch_quotes; BRAPI_TOKEN).
- Câmbio (dólar/euro/qualquer ISO → BRL) → open.er-api.com (grátis, sem chave).
- Cripto (BTC/ETH/…) → CoinGecko (grátis, sem chave).

Câmbio e cripto NÃO usam a brapi porque o plano free dela não cobre esses
recursos (FEATURE_NOT_AVAILABLE); as duas APIs grátis acima resolvem sem chave.
"""
from __future__ import annotations

import re

import httpx

from bot.services.quotes import QuotesError, fetch_quotes


class CotacaoError(Exception):
    pass


# nome/sinônimo (minúsculo) → (código ISO, rótulo)
_MOEDAS: dict[str, tuple[str, str]] = {
    "dolar": ("USD", "Dólar"), "dólar": ("USD", "Dólar"), "usd": ("USD", "Dólar"),
    "dolar americano": ("USD", "Dólar"),
    "euro": ("EUR", "Euro"), "eur": ("EUR", "Euro"),
    "libra": ("GBP", "Libra esterlina"), "gbp": ("GBP", "Libra esterlina"),
    "iene": ("JPY", "Iene"), "jpy": ("JPY", "Iene"),
    "peso argentino": ("ARS", "Peso argentino"), "ars": ("ARS", "Peso argentino"),
    "franco suico": ("CHF", "Franco suíço"), "chf": ("CHF", "Franco suíço"),
    "dolar canadense": ("CAD", "Dólar canadense"), "cad": ("CAD", "Dólar canadense"),
}

# nome/sinônimo (minúsculo) → (id CoinGecko, rótulo)
_CRIPTOS: dict[str, tuple[str, str]] = {
    "bitcoin": ("bitcoin", "Bitcoin (BTC)"), "btc": ("bitcoin", "Bitcoin (BTC)"),
    "ethereum": ("ethereum", "Ethereum (ETH)"), "eth": ("ethereum", "Ethereum (ETH)"),
    "solana": ("solana", "Solana (SOL)"), "sol": ("solana", "Solana (SOL)"),
    "bnb": ("binancecoin", "BNB"), "binancecoin": ("binancecoin", "BNB"),
    "cardano": ("cardano", "Cardano (ADA)"), "ada": ("cardano", "Cardano (ADA)"),
    "xrp": ("ripple", "XRP"), "ripple": ("ripple", "XRP"),
    "dogecoin": ("dogecoin", "Dogecoin (DOGE)"), "doge": ("dogecoin", "Dogecoin (DOGE)"),
    "litecoin": ("litecoin", "Litecoin (LTC)"), "ltc": ("litecoin", "Litecoin (LTC)"),
    "tether": ("tether", "Tether (USDT)"), "usdt": ("tether", "Tether (USDT)"),
}


def _brl(v: float) -> str:
    """R$ no padrão pt-BR (1.234,56)."""
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


async def _cambio(code: str, nome: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=12.0) as c:
            r = await c.get(f"https://open.er-api.com/v6/latest/{code}")
            r.raise_for_status()
            d = r.json()
    except httpx.HTTPError as e:
        raise CotacaoError(f"câmbio indisponível: {e}") from e
    if d.get("result") != "success":
        raise CotacaoError(f"não reconheci a moeda '{code}'")
    brl = (d.get("rates") or {}).get("BRL")
    if brl is None:
        raise CotacaoError(f"sem cotação em reais para {code}")
    return f"💵 {nome} ({code}): {_brl(float(brl))}"


async def _cripto(coin_id: str, nome: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=12.0) as c:
            r = await c.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_id, "vs_currencies": "brl",
                        "include_24hr_change": "true"},
            )
            r.raise_for_status()
            d = r.json()
    except httpx.HTTPError as e:
        raise CotacaoError(f"cripto indisponível: {e}") from e
    info = d.get(coin_id) or {}
    brl = info.get("brl")
    if brl is None:
        raise CotacaoError(f"não achei a cripto '{nome}'")
    ch = info.get("brl_24h_change")
    var = f" ({'+' if (ch or 0) >= 0 else ''}{ch:.1f}% 24h)" if ch is not None else ""
    return f"🪙 {nome}: {_brl(float(brl))}{var}"


async def _acao(ticker: str) -> str:
    try:
        precos = await fetch_quotes([ticker.upper()])
    except QuotesError as e:
        raise CotacaoError(str(e)) from e
    p = precos.get(ticker.upper())
    if p is None:
        raise CotacaoError(f"não achei {ticker.upper()} na B3 (o ticker existe?)")
    return f"📈 {ticker.upper()}: {_brl(float(p))}"


def _classify(ativo: str, tipo: str | None) -> str | None:
    if tipo in ("moeda", "cripto", "acao"):
        return tipo
    a = ativo.strip().lower()
    if a in _CRIPTOS:
        return "cripto"
    if a in _MOEDAS:
        return "moeda"
    if re.fullmatch(r"[a-z]{4}\d{1,2}", a):   # PETR4, HGLG11, ITUB4
        return "acao"
    if re.fullmatch(r"[a-z]{3}", a):          # ISO de moeda (USD, EUR, GBP…)
        return "moeda"
    return None


async def consultar_cotacao(ativo: str, tipo: str | None = None) -> str:
    """Cotação atual de um ativo (ação B3, moeda ou cripto) em reais. `tipo`
    opcional força a classe; senão é detectado. Levanta CotacaoError em falha."""
    ativo = (ativo or "").strip()
    if not ativo:
        raise CotacaoError("informe o ativo (ex: 'dólar', 'PETR4', 'bitcoin')")
    kind = _classify(ativo, tipo)
    a = ativo.lower()
    if kind == "moeda":
        code, nome = _MOEDAS.get(a, (ativo.upper(), ativo.upper()))
        return await _cambio(code, nome)
    if kind == "cripto":
        coin_id, nome = _CRIPTOS.get(a, (a, ativo.title()))
        return await _cripto(coin_id, nome)
    if kind == "acao":
        return await _acao(ativo)
    raise CotacaoError(
        f"não reconheci '{ativo}' como ação da B3, moeda ou cripto. "
        "Tente o ticker (PETR4), o nome da moeda (dólar) ou da cripto (bitcoin)."
    )
