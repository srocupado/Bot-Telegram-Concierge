"""Taxa do aporte no Tesouro: % a.a. na entrada, FRAÇÃO no dado gravado.

Bug da auditoria de 03/08/2026: o schema da tool declara `taxa` em % a.a.
("aportei a 6%"), o serviço gravava o número cru (`rate: 6`) e a projeção
usa o campo como fração — (1+i)*(1+r)-1 com r=6 multiplica o aporte por 7
AO ANO. R$ 1.000 viravam "R$ 7.000" no consultar_saldo em um ano, e o campo
corrompido é o MESMO que o app React lê.
"""
from __future__ import annotations

from datetime import date, timedelta

from bot.services.financeiro import (
    _project_contribution_to_today,
    _taxa_para_fracao,
)


def test_percentual_vira_fracao() -> None:
    assert _taxa_para_fracao(6) == 0.06
    assert _taxa_para_fracao(14.5) == 0.145
    assert _taxa_para_fracao(1) == 0.01


def test_taxa_menor_que_um_tambem_e_percentual() -> None:
    """Virada de contrato (26/08/2026): a heurística antiga tratava <1 como
    "fração já normalizada" — e "aportei no Selic a 0,15%" gravava rate=0.15
    = 15% a.a. (spread real do Tesouro Selic é 0,05–0,2%; a premissa "nenhum
    título paga menos de 1%" era falsa). A ambiguidade não tem solução por
    valor: o contrato da tool é % a.a. SEMPRE, e o schema agora diz isso em
    caixa alta. 0.06 vindo do modelo significa 0,06% — não 6%."""
    assert _taxa_para_fracao(0.15) == 0.0015
    assert _taxa_para_fracao(0.06) == 0.0006


def test_taxa_fora_da_faixa_estoura_alto() -> None:
    """Gravar lixo no Firestore que o app lê é pior que recusar: fora de
    (0, 100] o serviço levanta FinanceiroError e a tool devolve o erro pro
    modelo confirmar com o usuário."""
    import pytest
    from bot.services.financeiro import FinanceiroError
    with pytest.raises(FinanceiroError):
        _taxa_para_fracao(200)
    with pytest.raises(FinanceiroError):
        _taxa_para_fracao(0)


def test_projecao_com_taxa_normalizada_e_sana() -> None:
    """R$ 1.000 a 6% a.a. por 1 ano ≈ R$ 1.060 — não R$ 7.000."""
    um_ano_atras = (date.today() - timedelta(days=365)).isoformat()
    contrib = {
        "amount": 1000.0,
        "date": um_ano_atras,
        "rate": _taxa_para_fracao(6),
    }
    valor = _project_contribution_to_today(contrib, None, 0.0, date.today())
    assert 1050 < valor < 1075, f"projeção fora do sano: {valor}"


def test_regressao_taxa_crua_explodia() -> None:
    """Documenta o bug: com o rate cru (6), a projeção explodia ~7x/ano."""
    um_ano_atras = (date.today() - timedelta(days=365)).isoformat()
    contrib = {"amount": 1000.0, "date": um_ano_atras, "rate": 6}
    explodido = _project_contribution_to_today(contrib, None, 0.0, date.today())
    assert explodido > 6000, "premissa do bug mudou — revise o teste"
    contrib["rate"] = _taxa_para_fracao(6)
    corrigido = _project_contribution_to_today(contrib, None, 0.0, date.today())
    assert corrigido < 1100
