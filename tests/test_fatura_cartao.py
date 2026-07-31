"""Fatura do cartão: janela em aberto e entrada de dado torto.

Duas regressões cobertas:
- compra sumindo de TODAS as faturas por parcelamento inválido (corte
  silencioso — o pecado que este projeto não aceita);
- rótulo da fatura aberta discordando da matemática que decide em que fatura
  a compra cai (fechamento que não existe no mês, ex.: dia 31 em junho).
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from bot.services.financeiro import (
    _bill_month_for_date,
    _entry_in_bill,
    _open_invoice_range,
)


@pytest.mark.parametrize(
    "installments,current",
    [(-3, 1), (0, 0), ("x", 1), (None, -2), (-1, -1)],
)
def test_parcelamento_invalido_nao_faz_a_compra_sumir(installments, current) -> None:
    entry = {
        "date": "2026-07-10", "amount": 300.0,
        "installments": installments, "currentInstallment": current,
    }
    info = _entry_in_bill(entry, 2026, 7, 20)
    assert info is not None, "compra sumiu da fatura"
    assert info["value"] == 300.0


def test_parcelamento_valido_continua_correto() -> None:
    entry = {
        "date": "2026-07-10", "amount": 300.0,
        "installments": 3, "currentInstallment": 1,
    }
    assert _entry_in_bill(entry, 2026, 6, 20) is None            # antes da compra
    p1 = _entry_in_bill(entry, 2026, 7, 20)
    p3 = _entry_in_bill(entry, 2026, 9, 20)
    assert p1 and (p1["num"], round(p1["value"], 2)) == (1, 100.0)
    assert p3 and (p3["num"], round(p3["value"], 2)) == (3, 100.0)
    assert _entry_in_bill(entry, 2026, 10, 20) is None           # acabou


@pytest.mark.parametrize("closing", list(range(1, 32)))
@pytest.mark.parametrize("mes", [1, 2, 6, 12])
def test_rotulo_da_fatura_aberta_bate_com_a_matematica(closing: int, mes: int) -> None:
    """Toda data DENTRO do intervalo anunciado tem que cair na MESMA fatura —
    senão o bot diz 'a fatura aberta começa em 30/06' e joga a compra de 30/06
    na fatura fechada."""
    from calendar import monthrange

    state = {"settings": {"cardClosingDay": closing}}
    for dia in (1, 14, 27, monthrange(2026, mes)[1]):
        hoje = date(2026, mes, dia)
        ini, fim, _ = _open_invoice_range(state, hoje)
        assert ini <= hoje <= fim, f"hoje fora da própria fatura ({ini}→{fim})"
        alvo = _bill_month_for_date(ini, closing)
        d = ini
        while d <= fim:
            assert _bill_month_for_date(d, closing) == alvo, (
                f"{d} está no intervalo {ini}→{fim} mas cai noutra fatura"
            )
            d += timedelta(days=1)
