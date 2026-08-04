"""Chave da fatura ABERTA no consultar_lancamentos × régua das compras.

Bug da auditoria de 03/08/2026: a chave da fatura aberta era derivada do
mês do FIM do intervalo (`end.month`) em vez de `_bill_month_for_date`.
Com fechamento no dia 1º as duas contas divergem TODOS os dias do ano — o
filtro caía na fatura anterior: compras do mês passado rotuladas como "em
aberto", as do mês corrente omitidas, e o mesmo bill contado como "fechada
a pagar" E "aberta" ao mesmo tempo. Pra fechamento 2..31, coincidem.

A gravação nunca foi afetada (a compra persiste só a DATA; a fatura é
calculada na leitura) — o bug era de consulta/exibição.
"""
from __future__ import annotations

from datetime import date, timedelta

from bot.services.financeiro import (
    _bill_month_for_date,
    _entry_in_bill,
    _open_invoice_range,
)


def _state(closing: int) -> dict:
    return {"settings": {"cardClosingDay": closing}}


def test_chave_da_fatura_aberta_bate_com_a_regua_das_compras() -> None:
    """Invariante que o código violava: para QUALQUER fechamento e QUALQUER
    dia do ano, a fatura aberta (que contém `today`) é a de
    _bill_month_for_date(today). O código antigo usava end.month — igual
    para closing 2..31, errado 365/365 dias para closing=1."""
    for closing in range(1, 32):
        d = date(2026, 1, 1)
        while d <= date(2026, 12, 31):
            start, end, _ = _open_invoice_range(_state(closing), d)
            chave_certa = _bill_month_for_date(d, closing)
            assert (
                start <= d <= end
            ), f"today fora do próprio intervalo (closing={closing}, {d})"
            chave_antiga = (end.year, end.month)
            if closing == 1:
                assert chave_antiga != chave_certa, (
                    "premissa do bug mudou — com closing=1 as contas deviam "
                    f"divergir ({d})"
                )
            else:
                assert chave_antiga == chave_certa, (closing, d)
            d += timedelta(days=17)   # amostra o ano inteiro sem 365 iterações


def test_fechamento_dia_1_compra_do_mes_aparece_na_fatura_aberta() -> None:
    """Cenário do relatório: fechamento dia 1º, hoje 15/07. A compra de
    15/07 pertence à fatura de AGOSTO (a aberta). Com a chave antiga
    (julho), _entry_in_bill não a encontrava — sumia da consulta."""
    hoje = date(2026, 7, 15)
    closing = 1
    y, m = _bill_month_for_date(hoje, closing)
    assert (y, m) == (2026, 8), "compra dia>=1 entra na fatura seguinte"

    compra = {"date": "2026-07-15", "amount": 300.0, "installments": 1}
    assert _entry_in_bill(compra, y, m, closing) is not None, (
        "compra do mês não está na fatura aberta (chave certa)"
    )
    assert _entry_in_bill(compra, 2026, 7, closing) is None, (
        "premissa do bug mudou: a chave antiga (julho) devia EXCLUIR a compra"
    )
