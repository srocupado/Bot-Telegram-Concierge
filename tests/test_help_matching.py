"""Casamento do `ajuda` com perguntas reais.

Regra do projeto: feature nova só entra com o help atualizado E o matching
verificado com frases que o dono usaria de verdade.
"""
from __future__ import annotations

from bot.handlers.start import find_help_sections


def _titulos(pergunta: str) -> list[str]:
    return [s.splitlines()[0] for s in find_help_sections(pergunta)]


def _casa_dou(pergunta: str) -> bool:
    return any("Medidas Provis" in t for t in _titulos(pergunta))


def test_pergunta_sobre_perder_mp_acha_a_secao() -> None:
    """O medo do dono ("perder MP publicada") tem que achar a seção que
    explica conferência, pendência e re-tentativa."""
    for p in (
        "o bot confere se perdi alguma MP?",
        "como sei se escapou alguma medida provisoria?",
        "e se o inlabs estiver fora do ar?",
        "o que acontece com a pendencia de um dia que falhou",
        "perdeu MP publicada?",
    ):
        assert _casa_dou(p), f"sem match: {p!r}"


def test_comando_com_underscore_casa() -> None:
    """`_` é caractere de palavra pro regex, então `\\bmp\\b` não casava
    dentro de "/mp_dou_agora" — perguntar pelo comando pelo nome não achava
    nada. Vale pra todo comando, não só os do DOU."""
    assert _casa_dou("como uso o /mp_dou_agora")
    assert _casa_dou("o que faz o /dou_provider")
    assert any("proativo" in t.lower() for t in _titulos("/proativo_on serve pra que"))


def test_tradutor_devolve_so_a_secao_do_tradutor() -> None:
    """Bug real (dono, 10/08/2026): 'Como uso o tradutor?' devolvia a seção
    LLM INTEIRA (provider, thinking, reset, memória…) porque os bullets do
    tradutor moravam lá dentro. Agora o tradutor tem seção própria — e a
    resposta é SÓ ela."""
    for p in (
        "Como uso o tradutor?",
        "como funciona o modo tradutor",
        "traduz japonês pra mim?",
        "quero um intérprete de voz",
    ):
        blocos = find_help_sections(p)
        titulos = [b.splitlines()[0] for b in blocos]
        assert any("Tradutor" in t for t in titulos), f"sem match: {p!r}"
        assert not any(t.startswith("<b>LLM") for t in titulos), (
            f"veio a seção LLM junto: {p!r} → {titulos}"
        )
    # e o /tradutor pelo nome do comando também acha
    assert any("Tradutor" in t for t in _titulos("o que faz o /tradutor_provider"))
    # perguntas de provider/modelo continuam achando a seção LLM
    assert any("<b>LLM" in t for t in _titulos("como troco o provider?"))


def test_limite_de_palavra_continua_valendo() -> None:
    """A troca de `_` por espaço não pode ter afrouxado o limite de palavra:
    'mp' dentro de 'compras' seguiria casando com o Diário Oficial."""
    assert not _casa_dou("quero saber de compras")
    assert not _casa_dou("como faco uma comparacao de precos")
