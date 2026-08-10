"""Família "falha de fonte externa ≠ silêncio/falso negativo" (auditoria 03/08).

Cinco irmãos do mesmo bug, cada um numa fonte:
- cotação: erro voltava como texto pro LLM → modelo respondia preço de
  memória do treino;
- extrato: valores monetários passavam pela paráfrase (a tool análise de
  gastos, irmã deste bug, foi removida em 10/08/2026 — dono nunca usou);
- congresso: scrape PARCIAL (4 dias em 503) saía como semana completa;
- cinema: falha de API por filme virava "Sem sessões nessa data" verbatim;
- geocode: todos os métodos falhando viravam "não encontrei o endereço".
"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from bot.services import congress, geocoding, tools


def _ctx(**kw):
    base = dict(direct_html=None, short_circuit=False, fallback_text=None,
                tz="America/Sao_Paulo", session=None, user=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ─────────────────────────── cotação ───────────────────────────

def test_erro_de_cotacao_vai_verbatim(monkeypatch) -> None:
    from bot.services import cotacao

    async def _boom(*a, **kw):
        raise cotacao.CotacaoError("fonte indisponível: quota-reached")

    monkeypatch.setattr(cotacao, "consultar_cotacao", _boom)
    ctx = _ctx()
    out = asyncio.run(tools._h_consultar_cotacao({"ativo": "dólar"}, ctx))
    assert ctx.short_circuit is True
    assert "Não consegui consultar" in (ctx.direct_html or "")
    assert "não escreva nada" in out


# ─────────────────────── extrato (lançamentos) ───────────────────────

def test_extrato_vai_verbatim_e_ids_ficam_com_o_modelo(monkeypatch) -> None:
    corpo = "🏦 Banco\n• 01/08 — mercado · R$ 300,00"
    ids = "[IDS_INTERNOS — NÃO mostre ao usuário; use SÓ para apagar_lancamento]\nbanco · mercado → #abc123"

    async def _fake(*a, **kw):
        return f"{corpo}\n\n{ids}"

    monkeypatch.setattr(tools, "consultar_lancamentos", _fake)
    ctx = _ctx()
    out = asyncio.run(tools._h_consultar_lancamentos({}, ctx))
    assert "R$ 300,00" in (ctx.direct_html or ""), "corpo não foi verbatim"
    assert "IDS_INTERNOS" not in ctx.direct_html, "ids vazaram pro usuário"
    assert "#abc123" in out, "modelo perdeu os ids (quebra o apagar em cadeia)"
    assert ctx.short_circuit is False, (
        "short_circuit mataria o encadeamento consulta→apagar no mesmo turno"
    )


def test_extrato_sem_dados_nao_manda_verbatim(monkeypatch) -> None:
    async def _fake(*a, **kw):
        return "(sem dados)"

    monkeypatch.setattr(tools, "consultar_lancamentos", _fake)
    ctx = _ctx()
    out = asyncio.run(tools._h_consultar_lancamentos({}, ctx))
    assert ctx.direct_html is None
    assert "nenhum lançamento" in out


# ─────────────────────────── congresso ───────────────────────────

def test_scrape_parcial_marca_os_dias_falhos(monkeypatch) -> None:
    """Parcial COM item: entrega o que veio, mas diz quais dias ficaram fora.
    (Parcial com ZERO itens continua levantando — coberto no teste abaixo o
    caso de falha total; com item nenhum e erro algum, afirmar 'sem MP' seria
    o falso negativo.)"""
    seg = date(2026, 8, 3)

    async def _fetch_day(client, d):
        if d.weekday() in (1, 3):   # ter e qui caem
            raise congress.CongressScrapeError(f"503 em {d}")
        if d.weekday() == 0:
            return [congress.MPItem(date=d, hora="10h", descricao="MP 1.381", link=None)]
        return []

    monkeypatch.setattr(congress, "_fetch_day", _fetch_day)

    async def _main():
        async with httpx.AsyncClient() as client:
            return await congress.fetch_week_mps(client, seg)

    items = asyncio.run(_main())
    assert [d.weekday() for d in items.dias_falhos] == [1, 3]
    msg = congress.format_week_message(items, seg)
    assert "Não consegui checar" in msg
    assert "NÃO assuma pauta vazia" in msg


def test_scrape_total_falho_continua_levantando(monkeypatch) -> None:
    async def _fetch_day(client, d):
        raise congress.CongressScrapeError("tudo fora")

    monkeypatch.setattr(congress, "_fetch_day", _fetch_day)

    async def _main():
        async with httpx.AsyncClient() as client:
            return await congress.fetch_week_mps(client, date(2026, 8, 3))

    with pytest.raises(congress.CongressScrapeError):
        asyncio.run(_main())


def test_semana_completa_sem_aviso(monkeypatch) -> None:
    async def _fetch_day(client, d):
        return []

    monkeypatch.setattr(congress, "_fetch_day", _fetch_day)

    async def _main():
        async with httpx.AsyncClient() as client:
            return await congress.fetch_week_mps(client, date(2026, 8, 3))

    items = asyncio.run(_main())
    msg = congress.format_week_message(items, date(2026, 8, 3))
    assert "Sem MP esta semana" in msg
    assert "Não consegui checar" not in msg


# ─────────────────────────── cinema ───────────────────────────

def test_falha_por_filme_nao_vira_sem_sessao(monkeypatch) -> None:
    from bot.services import cinema

    async def _get(client, path, params=None):
        if params and params.get("movieId") == 2:
            raise cinema.CinemaError("429")
        return {"sessions": []}

    monkeypatch.setattr(cinema, "_get", _get)
    th = {"id": 10, "name": "Teste", "city": "Brasília", "state": "DF"}
    filmes = [{"id": 1, "name": "Filme OK"}, {"id": 2, "name": "Filme Caído"}]

    async def _main():
        async with httpx.AsyncClient() as client:
            return await cinema._programacao(
                client, th, filmes, date(2026, 8, 3), date(2026, 8, 3),
            )

    texto = asyncio.run(_main())
    assert "Não consegui checar: Filme Caído" in texto
    assert "NÃO significa que não há sessão" in texto
    linha_sem_sessao = next(
        (l for l in texto.splitlines() if l.startswith("Sem sessões nessa data:")), "",
    )
    assert "Filme Caído" not in linha_sem_sessao, (
        "filme com FALHA listado como 'sem sessão' (falso negativo verbatim)"
    )


# ─────────────────────────── geocode ───────────────────────────

def test_geocode_com_todas_as_apis_fora_levanta_erro(monkeypatch) -> None:
    async def _boom(*a, **kw):
        raise geocoding.GeocodingError("REQUEST_DENIED: billing")

    monkeypatch.setattr(geocoding, "_geocode_address", _boom)
    monkeypatch.setattr(geocoding, "_places_text_search", _boom)

    async def _main():
        async with httpx.AsyncClient() as client:
            return await geocoding.geocode(client, "k", "Av. Paulista 1000")

    with pytest.raises(geocoding.GeocodingError):
        asyncio.run(_main())


def test_geocode_nao_encontrado_de_verdade_segue_none(monkeypatch) -> None:
    """Uma API respondeu (sem resultado): aí sim é 'não encontrei'."""
    async def _none(*a, **kw):
        return None

    async def _boom(*a, **kw):
        raise geocoding.GeocodingError("quota")

    monkeypatch.setattr(geocoding, "_geocode_address", _none)
    monkeypatch.setattr(geocoding, "_places_text_search", _boom)

    async def _main():
        async with httpx.AsyncClient() as client:
            return await geocoding.geocode(client, "k", "Av. Paulista 1000")

    assert asyncio.run(_main()) is None
