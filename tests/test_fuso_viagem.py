"""Fuso EFETIVO (viagem) nos caminhos que usavam o de casa (auditoria 03/08).

Plumbing verificado aqui no ponto puro; os demais (rota por GPS, clima com
coords efetivas, prompt agendado, datas do DOU em BRT) são cobertos pela
suíte + revisão — todos passaram a usar effective_tz/effective_coords.
"""
from __future__ import annotations

from bot.handlers.search import _dated_system


def test_dated_system_usa_o_fuso_pedido() -> None:
    out = _dated_system("Asia/Tokyo")
    assert "Asia/Tokyo" in out, "busca seguiria resolvendo 'amanhã' no fuso de casa"


def test_dated_system_sem_fuso_cai_no_default() -> None:
    assert "America/Sao_Paulo" in _dated_system()
