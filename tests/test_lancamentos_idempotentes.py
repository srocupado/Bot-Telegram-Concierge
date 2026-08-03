"""Lançamentos financeiros: idempotência + confirmações que não somem.

Auditoria de 03/08/2026 — três caminhos reais de DUPLICATA na fatura da
família (tool_use duplo no mesmo turno, retry do modelo após timeout com
commit feito, dono repetindo o pedido), com a única defesa sendo prosa no
schema ("CHAME UMA VEZ"). Mais o amplificador: com duas tools verbatim no
mesmo turno, o slot único de direct_html guardava só a última confirmação —
a primeira sumia e induzia o re-lançamento.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from bot.services import tools
from bot.services.llm.base import ToolContext


@pytest.fixture(autouse=True)
def _janela_limpa():
    tools._lancamentos_recentes.clear()
    yield
    tools._lancamentos_recentes.clear()


def _ctx(user_text: str = "") -> ToolContext:
    return ToolContext(user=SimpleNamespace(id=1), session=None,
                       tz="America/Sao_Paulo", user_text=user_text)


def _patch_cartao(monkeypatch):
    """Serviço de cartão fake: registra chamadas, devolve entry plausível."""
    chamadas: list[dict] = []

    async def _fake(session, user, desc, valor, data_iso, *, categoria="outros",
                    parcelas=1, today=None):
        chamadas.append({"desc": desc, "valor": valor, "parcelas": parcelas})
        return {"id": "x1", "desc": desc, "amount": valor, "date": data_iso,
                "category": categoria, "installments": parcelas}

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(tools, "lancar_despesa_cartao", _fake)
    monkeypatch.setattr(tools, "record_action", _noop)
    return chamadas


# ─────────────────────── idempotência (janela curta) ───────────────────────

def test_lancamento_identico_repetido_nao_grava_de_novo(monkeypatch) -> None:
    chamadas = _patch_cartao(monkeypatch)
    args = {"desc": "cadeira", "valor": 300}

    async def _main():
        ctx1 = _ctx()
        r1 = await tools._h_lancar_despesa_cartao(args, ctx1)
        ctx2 = _ctx()
        r2 = await tools._h_lancar_despesa_cartao(args, ctx2)
        return r1, r2, ctx2

    r1, r2, ctx2 = asyncio.run(_main())
    assert len(chamadas) == 1, "gravou DUAS vezes o mesmo lançamento"
    assert "REPETIDO" in r2, "o modelo não foi avisado da repetição"
    assert "não dupliquei" in (ctx2.direct_html or ""), (
        "o usuário não foi avisado (repetição virou silêncio)"
    )


def test_lancamentos_diferentes_gravam_ambos(monkeypatch) -> None:
    """'pão por 10 e gasolina por 200' → duas compras legítimas, dois writes."""
    chamadas = _patch_cartao(monkeypatch)

    async def _main():
        ctx = _ctx()
        await tools._h_lancar_despesa_cartao({"desc": "pão", "valor": 10}, ctx)
        await tools._h_lancar_despesa_cartao({"desc": "gasolina", "valor": 200}, ctx)
        return ctx

    ctx = asyncio.run(_main())
    assert len(chamadas) == 2
    # O amplificador corrigido: as DUAS confirmações chegam ao usuário.
    assert "pão" in ctx.direct_html and "gasolina" in ctx.direct_html, (
        "uma confirmação sobrescreveu a outra (slot único de direct_html)"
    )


def test_falha_na_gravacao_nao_bloqueia_a_retentativa(monkeypatch) -> None:
    """O registro na janela acontece APÓS o commit: se a gravação falhou, o
    retry legítimo (segundos depois) tem que passar."""
    tentativas = {"n": 0}

    async def _flaky(session, user, desc, valor, data_iso, **kw):
        tentativas["n"] += 1
        if tentativas["n"] == 1:
            raise tools.FinanceiroError("Firestore fora")
        return {"id": "x1", "desc": desc, "amount": valor, "date": data_iso,
                "category": "outros", "installments": 1}

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(tools, "lancar_despesa_cartao", _flaky)
    monkeypatch.setattr(tools, "record_action", _noop)

    async def _main():
        r1 = await tools._h_lancar_despesa_cartao({"desc": "sofá", "valor": 900}, _ctx())
        r2 = await tools._h_lancar_despesa_cartao({"desc": "sofá", "valor": 900}, _ctx())
        return r1, r2

    r1, r2 = asyncio.run(_main())
    assert r1.startswith("erro:")
    assert tentativas["n"] == 2, "retry legítimo após falha foi bloqueado"
    assert "REPETIDO" not in r2


def test_janela_expira_e_compra_igual_horas_depois_passa(monkeypatch) -> None:
    chamadas = _patch_cartao(monkeypatch)
    args = {"desc": "café", "valor": 8}

    async def _main():
        await tools._h_lancar_despesa_cartao(args, _ctx())
        # envelhece a assinatura além do TTL (simula horas depois)
        for k in list(tools._lancamentos_recentes):
            tools._lancamentos_recentes[k] -= tools._IDEMP_TTL_S + 1
        await tools._h_lancar_despesa_cartao(args, _ctx())

    asyncio.run(_main())
    assert len(chamadas) == 2, "compra igual legítima (fora da janela) foi bloqueada"


# ─────────────── redirect banco→cartão: valor total em "Nx de V" ───────────────

def test_redirect_corrige_valor_de_parcela_para_total(monkeypatch) -> None:
    chamadas = _patch_cartao(monkeypatch)

    async def _main():
        ctx = _ctx(user_text="comprei uma TV no crédito em 3x de 100")
        return await tools._h_lancar_movimento_banco(
            {"desc": "TV", "valor": 100, "tipo": "debito"}, ctx,
        )

    asyncio.run(_main())
    assert len(chamadas) == 1
    assert chamadas[0]["valor"] == 300.0, (
        f"valor da parcela gravado como total: {chamadas[0]['valor']}"
    )
    assert chamadas[0]["parcelas"] == 3


def test_redirect_com_valor_total_nao_multiplica(monkeypatch) -> None:
    """Se o modelo já mandou o TOTAL (300 em '3x de 100'), não mexe."""
    chamadas = _patch_cartao(monkeypatch)

    async def _main():
        ctx = _ctx(user_text="comprei uma TV no crédito em 3x de 100")
        return await tools._h_lancar_movimento_banco(
            {"desc": "TV", "valor": 300, "tipo": "debito"}, ctx,
        )

    asyncio.run(_main())
    assert chamadas[0]["valor"] == 300


# ─────────────────── data_iso: plausibilidade além do formato ───────────────────

def test_data_com_ano_alucinado_e_rejeitada() -> None:
    tz = "America/Sao_Paulo"
    hoje = datetime.now(ZoneInfo(tz)).date()
    antiga = (hoje - timedelta(days=500)).isoformat()
    assert tools._resolve_data_iso({"data_iso": antiga}, tz) == ""
    futura = (hoje + timedelta(days=90)).isoformat()
    assert tools._resolve_data_iso({"data_iso": futura}, tz) == ""


def test_data_retroativa_legitima_passa() -> None:
    tz = "America/Sao_Paulo"
    hoje = datetime.now(ZoneInfo(tz)).date()
    mes_passado = (hoje - timedelta(days=40)).isoformat()
    assert tools._resolve_data_iso({"data_iso": mes_passado}, tz) == mes_passado
    assert tools._resolve_data_iso({}, tz) == hoje.isoformat()


# ────────────── confirmação retroativa: fatura de DESTINO ──────────────

def test_confirmacao_retroativa_diz_a_fatura_destino() -> None:
    from bot.services.financeiro import confirm_cartao

    entry = {"desc": "conserto", "amount": 100.0, "date": "2026-07-05",
             "category": "outros", "_fatura_destino": (2026, 7)}
    txt = confirm_cartao(entry, 1)
    assert "Entrou na fatura de 07/2026" in txt
    assert "Fatura aberta (ciclo atual)" not in txt, (
        "confirmação retroativa mostrando o ciclo atual — induz re-lançamento"
    )
