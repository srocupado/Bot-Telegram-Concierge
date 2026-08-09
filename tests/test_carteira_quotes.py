"""Carteira/brapi sem perda silenciosa (item 14 da auditoria; dono 09/08/2026).

Dois buracos: (1) preço 0/negativo da brapi era aceito e ZERAVA o
currentPrice no Firestore — carteira inteira 'no prejuízo' em silêncio;
(2) falha total da brapi (ou do Firestore) sumia com a revisão do dia sem
nenhum aviso — o dono lia como 'dia sem nada' com preços de ontem no banco.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import httpx
import respx

from bot.services import proactive, quotes
from bot.services.quotes import QuotesError, _fetch_one


def _quote_resp(price):
    return {"results": [{"symbol": "PETR4", "regularMarketPrice": price}]}


def _busca(price):
    async def _main():
        with respx.mock:
            respx.route(host="brapi.dev").respond(200, json=_quote_resp(price))
            async with httpx.AsyncClient() as client:
                return await _fetch_one(client, "PETR4", "tok")

    return asyncio.run(_main())


def test_preco_valido_passa() -> None:
    ticker, price, err = _busca(37.42)
    assert (ticker, price, err) == ("PETR4", 37.42, None)


def test_preco_zero_e_rejeitado_nao_persiste() -> None:
    ticker, price, err = _busca(0)
    assert price is None
    assert "sem preço válido" in err


def test_preco_negativo_e_lixo_tambem() -> None:
    _, price, err = _busca(-3.2)
    assert price is None and "sem preço válido" in err


# ───────────────────── revisão da carteira fala ao falhar ────────────────────

def _rodar_carteira(monkeypatch, *, efeito):
    from bot.services import financeiro

    async def _tickers(_s, _u):
        return ["PETR4"]

    async def _quotes(_t):
        raise efeito

    monkeypatch.setattr(financeiro, "get_carteira_tickers", _tickers)
    monkeypatch.setattr(quotes, "fetch_quotes", _quotes)
    user = SimpleNamespace(id=7)
    agora = datetime.now(proactive.BRT)
    return asyncio.run(proactive.collect_carteira(None, user, agora, force=True))


def test_brapi_fora_vira_aviso_no_resumo(monkeypatch) -> None:
    facts = _rodar_carteira(monkeypatch, efeito=QuotesError("brapi indisponível"))
    assert [f.kind for f in facts] == ["carteira_falhou"]
    assert "NÃO foram atualizados" in facts[0].text
    # A mensagem da lib pode carregar a URL com o token da brapi — o texto
    # pro dono é genérico de propósito.
    assert "tok" not in facts[0].text and "http" not in facts[0].text


def test_falha_inesperada_tambem_e_dita(monkeypatch) -> None:
    facts = _rodar_carteira(monkeypatch, efeito=RuntimeError("Firestore fora"))
    assert [f.kind for f in facts] == ["carteira_falhou"]
    assert "RuntimeError" in facts[0].text


def test_fora_da_ultima_janela_segue_em_silencio(monkeypatch) -> None:
    """O gate de horário continua: sem force, só a última janela do dia roda
    a revisão — falha só é dita quando a revisão DEVIA sair."""
    user = SimpleNamespace(id=7)
    agora = datetime.now(proactive.BRT).replace(hour=3)
    facts = asyncio.run(proactive.collect_carteira(None, user, agora))
    assert facts == []


# ─────────────────── catch-up do digest do Congresso ────────────────────────

def test_digest_congresso_catchup_ate_quarta() -> None:
    """Item 12 da auditoria: bot fora do ar na segunda perdia a pauta da
    SEMANA em silêncio. Ter/qua agora recuperam (dedup ancorado na segunda
    real); de quinta em diante não vale mais o envio atrasado."""
    from datetime import date as _date
    from bot.services.scheduler import _segunda_do_digest

    segunda = _date(2026, 8, 10)
    for dia, esperado in [
        (datetime(2026, 8, 10, 8, 0), segunda),   # segunda → envia
        (datetime(2026, 8, 11, 9, 0), segunda),   # terça  → catch-up
        (datetime(2026, 8, 12, 21, 0), segunda),  # quarta → catch-up
        (datetime(2026, 8, 13, 8, 0), None),      # quinta → não
        (datetime(2026, 8, 16, 8, 0), None),      # domingo → não
    ]:
        assert _segunda_do_digest(dia) == esperado, dia
