"""Outbox da nota + extração de ementa/ano do trecho do DOU.

Premissa do projeto: NÃO PERDER a detecção de MP. Estes dois consertos servem
a ela por caminhos diferentes — o outbox impede que a nota suma num restart, e
a leitura do ano impede que a MP seja gravada com identidade errada (o que
quebraria dedup e conferência).
"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from bot.services import dou_monitor as dm

D = date(2026, 7, 31)

# Trecho REAL do DOU, como veio na mensagem de 01/08/2026: ementa, título, e a
# ementa de novo — o fallback antigo pegava do começo e cortava em 300 chars.
EXCERPT = (
    "Abre crédito extraordinário, em favor do Ministério de Minas e Energia, "
    "no valor de R$ 3.473.000.000,00, para os fins que especifica. "
    "MEDIDA PROVISÓRIA Nº 1.381, DE 30 DE JULHO DE 2026 "
    "Abre crédito extraordinário, em favor do Ministério de Minas e Energia, "
    "no valor de R$ 3.473.000.000,00, para os fins que especifica. "
    "O PRESIDENTE DA REPÚBLICA, no uso da atribuição que lhe confere o art. 62 "
    "da Constituição, adota a seguinte Medida Provisória..."
)


# ───────────────────────────── ementa ─────────────────────────────

def test_ementa_nao_sai_duplicada() -> None:
    ementa = dm._ementa_do_excerpt(EXCERPT)
    assert ementa.count("Abre crédito extraordinário") == 1, ementa


def test_ementa_nao_corta_no_meio_da_palavra() -> None:
    """O sintoma visível era "…para os fi" na mensagem do dono."""
    ementa = dm._ementa_do_excerpt(EXCERPT)
    assert ementa.endswith("para os fins que especifica."), ementa


def test_ementa_para_antes_do_preambulo() -> None:
    assert "PRESIDENTE DA REPÚBLICA" not in dm._ementa_do_excerpt(EXCERPT)


def test_corte_longo_termina_em_palavra_inteira() -> None:
    texto = "palavra " * 100
    corte = dm._corta_em_palavra(texto, 50)
    assert len(corte) <= 51
    assert not corte.rstrip("…").endswith("palav")


def test_excerpt_sem_titulo_ainda_devolve_algo() -> None:
    """Sem título reconhecível é melhor entregar o trecho cortado direito do
    que ementa vazia — o dono ficaria sem nenhuma descrição da MP."""
    assert dm._ementa_do_excerpt("Dispõe sobre coisas relevantes.") != ""


# ───────────────────────────── ano da MP ─────────────────────────────

def test_ano_vem_do_titulo_e_nao_da_consulta() -> None:
    """MP assinada em 31/12 sai no DOU de 01/01: gravar o ano da consulta
    criaria 1381/2027 pra uma MP que é 1381/2026, quebrando dedup, URL do
    Planalto e o cruzamento com a Câmara."""
    assert dm.ano_da_mp(EXCERPT, 2027) == 2026


def test_ano_sem_titulo_cai_no_padrao() -> None:
    assert dm.ano_da_mp("texto qualquer", 2026) == 2026


def test_ano_absurdo_e_ignorado() -> None:
    """Lixo de parse não pode reescrever a identidade da MP."""
    lixo = "MEDIDA PROVISÓRIA Nº 1.381, DE 30 DE JULHO DE 1999 ..."
    assert dm.ano_da_mp(lixo, 2026) == 2026


# ───────────────────────────── outbox ─────────────────────────────

class _Sessao:
    def __init__(self, pendencias=()):
        self.rows = [SimpleNamespace(key=k) for k in pendencias]

    async def scalars(self, _stmt):
        return list(self.rows)

    async def commit(self):
        return None


@pytest.fixture
def espiao(monkeypatch):
    reg = {"mark": [], "unmark": []}
    from bot.services import proactive

    async def _mark(_s, _uid, kind, key):
        reg["mark"].append((kind, key))

    async def _unmark(_s, _uid, kind, key):
        reg["unmark"].append((kind, key))

    monkeypatch.setattr(proactive, "mark_notified", _mark)
    monkeypatch.setattr(proactive, "unmark_notified", _unmark)
    return reg


def test_outbox_registra_antes_de_gerar(espiao) -> None:
    """A pendência tem que existir ANTES da geração: se o container reiniciar
    no meio dos ~68s, a MP já está marcada como vista e sem a pendência a nota
    nunca mais seria tentada — sem nem um aviso."""
    avisadas = [{"numero": "1381"}, {"numero": "1382"}]
    chave = asyncio.run(dm._abrir_outbox(_Sessao(), 1, D, avisadas))
    assert chave == "2026-07-31:1381,1382"
    assert espiao["mark"] == [("nota_pendente", chave)]


def test_outbox_nao_duplica_a_entrada_do_job(espiao) -> None:
    """O job da fila já tem a sua pendência; abrir outra mostraria duas linhas
    de status pro mesmo dia."""
    sessao = _Sessao(["2026-07-31:all"])
    chave = asyncio.run(dm._abrir_outbox(sessao, 1, D, [{"numero": "1381"}]))
    assert chave is None and espiao["mark"] == []


def test_sucesso_da_baixa_e_nao_deixa_resto(espiao) -> None:
    asyncio.run(dm._fechar_outbox(_Sessao(), 1, D, "2026-07-31:1381", falhas=[]))
    assert espiao["unmark"] == [("nota_pendente", "2026-07-31:1381")]
    assert espiao["mark"] == []


def test_falha_volta_pra_fila(espiao) -> None:
    """O ponto da premissa: nota que falhou não pode sumir."""
    asyncio.run(dm._fechar_outbox(_Sessao(), 1, D, "2026-07-31:1381,1382",
                                  falhas=["1382"]))
    assert ("nota_pendente", "2026-07-31:1381,1382") in espiao["unmark"]
    assert ("nota_pendente", "2026-07-31:1382") in espiao["mark"]


def test_falha_volta_pra_fila_mesmo_sem_outbox_proprio(espiao) -> None:
    """Quando o outbox é do job da fila, aquele job dá baixa por ter concluído
    a chamada — sem esta linha, a nota que falhou sumiria junto."""
    asyncio.run(dm._fechar_outbox(_Sessao(["2026-07-31:all"]), 1, D, None,
                                  falhas=["1381"]))
    assert espiao["mark"] == [("nota_pendente", "2026-07-31:1381")]
    assert espiao["unmark"] == []
