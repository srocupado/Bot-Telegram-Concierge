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


def test_limite_de_palavra_continua_valendo() -> None:
    """A troca de `_` por espaço não pode ter afrouxado o limite de palavra:
    'mp' dentro de 'compras' seguiria casando com o Diário Oficial."""
    assert not _casa_dou("quero saber de compras")
    assert not _casa_dou("como faco uma comparacao de precos")
