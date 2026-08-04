"""finance_guard: matching por fronteira de palavra (auditoria 03/08/2026).

O matching por substring furava nas duas direções:
- "cade" ⊂ "CADEira" e "qual" ⊂ "QUALidade" desarmavam o guard — "comprei
  uma cadeira por 300 no crédito" + confirmação alucinada PASSAVA, que é
  exatamente o que a camada existe pra impedir;
- "feito" ⊂ "PerFEITO" bloqueava pergunta de esclarecimento legítima;
- "Pronto! Já está no seu financeiro." não casava nenhum radical de
  sucesso e a confirmação alucinada passava.
"""
from __future__ import annotations

from bot.services.finance_guard import (
    GUARD_MESSAGE,
    guard_financial_reply,
    is_financial_logging_intent,
)


def test_cadeira_nao_desarma_o_guard() -> None:
    """'cadeira' contém 'cade' — mas não é pergunta de consulta."""
    assert is_financial_logging_intent("comprei uma cadeira por 300 no crédito")
    out = guard_financial_reply(
        "comprei uma cadeira por 300 no crédito", False, "Pronto, registrado!",
    )
    assert out == GUARD_MESSAGE, "confirmação alucinada de compra passou"


def test_qualidade_nao_desarma_o_guard() -> None:
    assert is_financial_logging_intent("comprei um fone de qualidade por 200 no cartão")
    out = guard_financial_reply(
        "comprei um fone de qualidade por 200 no cartão", False, "Anotado!",
    )
    assert out == GUARD_MESSAGE


def test_consultas_reais_continuam_desarmando() -> None:
    assert not is_financial_logging_intent("cadê o pix de 200 que recebi?")
    assert not is_financial_logging_intent("quanto gastei no cartão em julho?")
    assert not is_financial_logging_intent("apaga o lançamento de 300 da cadeira")


def test_pronto_no_seu_financeiro_e_alegacao_de_sucesso() -> None:
    out = guard_financial_reply(
        "lança 40 no débito, mercado", False, "Pronto! Já está no seu financeiro.",
    )
    assert out == GUARD_MESSAGE, "fraseado sem radical clássico passou pela blindagem"


def test_esclarecimento_com_perfeito_nao_e_bloqueado() -> None:
    """'Perfeito — qual o valor?' é esclarecimento, não confirmação:
    'feito' ⊂ 'perfeito' bloqueava por substring."""
    reply = "Perfeito — qual o valor e a data da compra?"
    out = guard_financial_reply("lança a compra do mercado de 40", False, reply)
    assert out == reply


def test_gravada_feminino_segue_bloqueada() -> None:
    """Radical 'gravad' continua casando por PREFIXO de palavra."""
    out = guard_financial_reply("lança 50 no débito", False, "Despesa gravada!")
    assert out == GUARD_MESSAGE
