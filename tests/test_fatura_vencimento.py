"""Vencimento e gatilho do fechamento da fatura (auditoria 03/08/2026).

- Vencimento era hardcoded no mês SEGUINTE ao fechamento — errado quando
  dueDay > closingDay (fecha 10, vence 20 → o bot anunciava 20/08 pra
  fatura que vence 20/07, contradizendo o card_due_soon no mesmo dia);
- Resumo de fechamento com closing=31 nunca disparava em mês de 30 dias
  (o clamp que o _open_invoice_range já tinha faltava no gatilho).
"""
from __future__ import annotations

from datetime import date

from bot.services.financeiro import _closed_bill_to_pay, _vencimento_da_fatura


def test_due_maior_que_closing_vence_no_mesmo_mes() -> None:
    """Fecha dia 10, vence dia 20: a fatura de julho vence em 20/07."""
    assert _vencimento_da_fatura(2026, 7, 10, 20) == date(2026, 7, 20)


def test_due_menor_ou_igual_ao_closing_vence_no_mes_seguinte() -> None:
    """Fecha dia 20, vence dia 5: a fatura de julho vence em 05/08."""
    assert _vencimento_da_fatura(2026, 7, 20, 5) == date(2026, 8, 5)
    assert _vencimento_da_fatura(2026, 12, 20, 5) == date(2027, 1, 5)


def test_clamp_de_fim_de_mes() -> None:
    """Fecha dia 20, vence dia 30 (mesmo mês): em fevereiro clampa no 28."""
    assert _vencimento_da_fatura(2026, 2, 20, 30) == date(2026, 2, 28)
    # E no caminho do mês seguinte: fecha 30, vence 30 → mês seguinte, clamp.
    assert _vencimento_da_fatura(2026, 1, 30, 30) == date(2026, 2, 28)


def test_closed_bill_com_due_no_mesmo_mes() -> None:
    """15/07, fecha 10, vence 20: a fatura de julho está fechada e vence em
    CINCO dias (20/07) — o hardcode antigo dizia 20/08."""
    closed = _closed_bill_to_pay(date(2026, 7, 15), 10, 20)
    assert closed is not None
    cy, cm, due = closed
    assert (cy, cm) == (2026, 7)
    assert due == date(2026, 7, 20)


def test_closed_bill_ja_vencida_nao_aparece() -> None:
    """25/07, fecha 10, vence 20: a fatura de julho já venceu — nada a pagar
    (antes, com o vencimento jogado pra 20/08, ela ficava 'a pagar' um mês
    além da conta)."""
    assert _closed_bill_to_pay(date(2026, 7, 25), 10, 20) is None


def test_fechamento_31_clampa_no_ultimo_dia_do_mes_curto() -> None:
    """closing=31: o gatilho do resumo usa min(closing, último dia do mês) —
    em junho (30 dias) dispara no dia 30; em julho, no 31. Sem o clamp, o
    resumo mensal nunca saía em abr/jun/set/nov/fev."""
    from calendar import monthrange

    closing = 31
    assert min(closing, monthrange(2026, 6)[1]) == 30
    assert min(closing, monthrange(2026, 7)[1]) == 31
    assert min(closing, monthrange(2026, 2)[1]) == 28
