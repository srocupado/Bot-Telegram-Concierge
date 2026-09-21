"""Estouro do limite de rodadas de ferramenta: instrução e teto por TIPO de turno.

Dono, 21/09/2026. Perguntou onde comprar um drone no Japão e recebeu:

    "Nesta conversa, as seguintes ações já foram executadas e registradas no
     sistema:
     • Lançamento de R$ 236,00 no cartão de crédito (Almoço).
     • Lançamento de R$ 150,00 no cartão de crédito (Combustível).
     • Consulta web sobre a disponibilidade do drone…"

"Que negócio é esse de 'Nessa conversa...'?"

Não era lançamento indevido: eram lançamentos REAIS e antigos da conversa,
recitados porque o turno bateu em `max_iterations` e o código injeta uma
última rodada mandando o modelo INVENTARIAR o que executou. A instrução
existe por um motivo bom — turno que gravou 3 coisas e morreu no meio precisa
avisar, senão o dono repete o pedido e DUPLICA. Mas ela disparava igual num
turno que só leu páginas, onde não há nada a duplicar.

Duas correções, travadas aqui:
1. a instrução do estouro depende de ter havido ESCRITA no turno;
2. o teto de rodadas era 5 fixo (uma busca que abre 2 páginas estoura) e
   agora afrouxa enquanto nada foi gravado.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.services.llm.base import (
    ITER_LIMIT_FALLBACK,
    ITER_LIMIT_FALLBACK_LEITURA,
    ITER_LIMIT_INSTRUCTION,
    ITER_LIMIT_INSTRUCTION_LEITURA,
    MAX_ITER_PADRAO,
    MAX_ITER_SOMENTE_LEITURA,
    TOOLS_SOMENTE_LEITURA,
    fallback_limite,
    houve_escrita,
    instrucao_limite,
    teto_de_rodadas,
)


def _ctx(*chamadas):
    return SimpleNamespace(tools_chamadas=list(chamadas))


# ─────────────── o caso que originou tudo ───────────────

def test_turno_so_de_busca_nao_inventaria_acoes() -> None:
    """A pergunta do drone: só buscar_web e ler_pagina rodaram."""
    ctx = _ctx("buscar_web", "ler_pagina", "ler_pagina")
    assert houve_escrita(ctx) is False
    txt = instrucao_limite(ctx)
    assert txt == ITER_LIMIT_INSTRUCTION_LEITURA
    assert "NÃO liste ações executadas" in txt
    assert "registros no sistema" in txt


def test_turno_que_gravou_mantem_o_inventario() -> None:
    """O motivo de a instrução existir: lançou e morreu no meio. Aqui o
    relato PRECISA sair, senão o dono repete o pedido e duplica."""
    ctx = _ctx("buscar_web", "lancar_despesa_cartao")
    assert houve_escrita(ctx) is True
    assert instrucao_limite(ctx) == ITER_LIMIT_INSTRUCTION
    assert "JÁ executou" in instrucao_limite(ctx)


def test_fallback_segue_a_mesma_regra() -> None:
    """Quando nem a última rodada responde, o aviso cru também muda de tom."""
    assert fallback_limite(_ctx("buscar_web")) == ITER_LIMIT_FALLBACK_LEITURA
    assert "Nada foi gravado" in fallback_limite(_ctx("buscar_web"))
    assert fallback_limite(_ctx("criar_tarefa")) == ITER_LIMIT_FALLBACK
    assert "PODE já ter sido executada" in fallback_limite(_ctx("criar_tarefa"))


def test_turno_sem_tool_nenhuma_e_leitura() -> None:
    assert houve_escrita(_ctx()) is False


def test_ctx_sem_o_campo_nao_estoura() -> None:
    """Chamador antigo / dublê de teste sem `tools_chamadas`."""
    assert houve_escrita(SimpleNamespace()) is False


# ─────────────── default seguro da classificação ───────────────

def test_tool_desconhecida_conta_como_ESCRITA() -> None:
    """O default tem que ser o inverso do confortável: tool nova que grava e
    ninguém classificou não pode passar por leitura — isso esconderia do dono
    que algo foi gravado. Errar pro outro lado só gera um aviso a mais."""
    assert houve_escrita(_ctx("tool_dinamica_que_ninguem_classificou")) is True


def test_o_frozenset_nao_tem_nome_fantasma() -> None:
    """Nome que não existe em TOOLS é classificação morta — e eu escrevi cinco
    deles na primeira versão ('buscar_voos' em vez de 'buscar_voo', etc.)."""
    from bot.services.tools import TOOLS

    reais = {t.name for t in TOOLS}
    assert not (TOOLS_SOMENTE_LEITURA - reais)


@pytest.mark.parametrize("nome", [
    "lancar_despesa_cartao", "lancar_movimento_banco", "criar_lembrete",
    "criar_tarefa", "adicionar_lista_compras", "registrar_treino",
    "criar_watch_voo", "executar_agente", "apagar_lancamento",
])
def test_tools_que_gravam_nunca_entram_na_lista_de_leitura(nome: str) -> None:
    """Se alguma destas vazar pra leitura, o dono deixa de ser avisado de um
    lançamento feito pela metade."""
    assert nome not in TOOLS_SOMENTE_LEITURA


# ─────────────── teto adaptativo ───────────────

def test_teto_afrouxa_enquanto_so_le() -> None:
    """5 rodadas fixas estouravam numa busca que abre duas páginas."""
    assert teto_de_rodadas(_ctx("buscar_web"), MAX_ITER_PADRAO) == MAX_ITER_SOMENTE_LEITURA
    assert MAX_ITER_SOMENTE_LEITURA > MAX_ITER_PADRAO > 5


def test_teto_encurta_assim_que_grava() -> None:
    """Turno que começou lendo e gravou no meio volta ao teto curto — cada
    rodada extra ali tem efeito permanente."""
    assert teto_de_rodadas(_ctx("buscar_web", "criar_tarefa"),
                           MAX_ITER_PADRAO) == MAX_ITER_PADRAO


def test_teto_respeita_valor_explicito_maior() -> None:
    assert teto_de_rodadas(_ctx("criar_tarefa"), 30) == 30


# ─────────────── fiação nos três providers ───────────────

@pytest.mark.parametrize("modulo", ["anthropic_impl", "openai_impl", "gemini_impl"])
def test_provider_registra_a_tool_e_usa_a_instrucao_condicional(modulo: str) -> None:
    """Sem o append, `tools_chamadas` fica vazio e TODO turno parece leitura —
    o pior desfecho possível, porque some o aviso de duplicação."""
    import inspect
    import importlib

    src = inspect.getsource(importlib.import_module(f"bot.services.llm.{modulo}"))
    assert "ctx.tools_chamadas.append(" in src, "não registra a tool chamada"
    assert "instrucao_limite(ctx)" in src, "usa a instrução fixa"
    assert "fallback_limite(ctx)" in src, "usa o fallback fixo"
    assert "teto_de_rodadas(ctx" in src, "teto não é adaptativo"
    # A constante antiga não pode mais ser usada direto por provider nenhum.
    codigo = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "ITER_LIMIT_INSTRUCTION" not in codigo
