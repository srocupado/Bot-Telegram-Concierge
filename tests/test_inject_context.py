"""Bloco volátil (data/hora + memória) prefixado na mensagem do usuário.

Bug do dono (26/08/2026): "Consegue fazer um post com essas informações?"
chegava ao modelo colado logo DEPOIS do resumo de memória de longo prazo — e
o "essas informações" amarrava no antecedente mais próximo (o perfil: oficina,
compressor, drone…) em vez da resposta sobre a Câmara dada no turno anterior.
O conserto delimita o bloco e rotula a mensagem real; estes testes prendem a
ESTRUTURA (a semântica é do modelo, mas a estrutura é nossa).
"""
from __future__ import annotations

from bot.handlers.chat import inject_context

TZ = "America/Sao_Paulo"
RESUMO = "Perfil: gosta de oficina, compressor Chiaperini, drone DJI."


def _msgs(texto: str):
    return [
        {"role": "user", "content": "última sessão deliberativa?"},
        {"role": "assistant", "content": "Foi em 13/08/2026 (Extraordinária 152)."},
        {"role": "user", "content": texto},
    ]


def test_memoria_fica_delimitada_e_a_mensagem_rotulada() -> None:
    pergunta = "Consegue fazer um post com essas informações?"
    out = inject_context(_msgs(pergunta), TZ, RESUMO)
    corpo = out[-1]["content"]

    i_ctx = corpo.index("[CONTEXTO AUTOMÁTICO")
    i_resumo = corpo.index(RESUMO)
    i_fim = corpo.index("[FIM DO CONTEXTO AUTOMÁTICO]")
    i_rotulo = corpo.index("MENSAGEM ATUAL DO USUÁRIO")
    i_pergunta = corpo.index(pergunta)

    assert i_ctx < i_resumo < i_fim < i_rotulo < i_pergunta, (
        "a memória tem que ficar DENTRO dos delimitadores e a pergunta DEPOIS "
        "do rótulo — era o encadeamento cru que fazia 'essas informações' "
        "apontar pro perfil")
    assert "apontam pra CONVERSA acima" in corpo


def test_sem_memoria_ainda_rotula_a_mensagem() -> None:
    out = inject_context(_msgs("oi"), TZ, None)
    corpo = out[-1]["content"]
    assert "MEMÓRIA DE CONVERSAS" not in corpo
    assert corpo.index("MENSAGEM ATUAL DO USUÁRIO") < corpo.index("\n\noi")


def test_nao_muta_o_historico_original() -> None:
    """Os dicts são compartilhados com o store de memória — mutação aqui
    contaminaria o histórico gravado."""
    msgs = _msgs("pergunta")
    inject_context(msgs, TZ, RESUMO)
    assert msgs[-1]["content"] == "pergunta"
    assert all("CONTEXTO" not in m["content"] for m in msgs)


def test_conteudo_em_lista_ganha_o_bloco_como_primeira_parte() -> None:
    """Caminho de foto/PDF: content é lista de parts."""
    msgs = [{"role": "user", "content": [{"type": "image", "data": "x"}]}]
    out = inject_context(msgs, TZ, RESUMO)
    partes = out[-1]["content"]
    assert partes[0]["type"] == "text"
    assert "MENSAGEM ATUAL DO USUÁRIO" in partes[0]["text"]
    assert partes[1] == {"type": "image", "data": "x"}
