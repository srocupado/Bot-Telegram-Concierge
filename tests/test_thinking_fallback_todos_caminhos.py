"""A queda automática do thinking vale nos QUATRO caminhos que usam Gemini.

O conserto original ficou só no chat. Nota técnica do DOU, STT de voz e
tradutor montavam `ThinkingConfig(thinking_budget=0)` na mão e chamavam
`generate_content` direto — sem queda. Cada um quebraria com o mesmo
400 INVALID_ARGUMENT no dia em que apontasse pra um modelo que recusa o 0
(`/dou_provider`, `/voice`, `/tradutor_provider`), do jeito que o chat
quebrou com o gemini-3.6-flash.

O budget 0 é PROPOSITAL nos três (transcrição e JSON não precisam raciocinar,
e o thinking come max_output_tokens, truncando a nota). O conserto não é tirar
o 0 — é fazer o 0 degradar sozinho.
"""
from __future__ import annotations

import inspect

import pytest

from bot.services import dou_monitor, translator, voice
from bot.services.llm import gemini_impl as gi


@pytest.fixture(autouse=True)
def _limpa():
    gi._SEM_THINKING_BUDGET.clear()
    yield
    gi._SEM_THINKING_BUDGET.clear()


class _RespFake:
    text = "ok"


class _ClienteQueRecusa:
    """Modelo que aceita o corpo mas recusa thinking_config, como o 3.6-flash."""

    def __init__(self):
        self.chamadas: list[object] = []

    @property
    def models(self):
        return self

    def generate_content(self, *, model, contents, config):
        tc = getattr(config, "thinking_config", None)
        self.chamadas.append(tc)
        if tc is not None:
            raise RuntimeError("400 INVALID_ARGUMENT. Request contains an invalid argument.")
        return _RespFake()


def test_budget_zero_cai_e_a_chamada_completa() -> None:
    """Contrato compartilhado pelos quatro caminhos."""
    cli = _ClienteQueRecusa()
    resp = gi.gerar(cli, "gemini-3.6-flash", [], "dou:nota", budget=0,
                    max_output_tokens=16384)
    assert resp.text == "ok"
    assert [c is None for c in cli.chamadas] == [False, True]


def test_budget_zero_continua_sendo_pedido_em_quem_aceita() -> None:
    """A queda não pode virar 'nunca mais manda 0': o thinking ligado por
    engano trunca a nota técnica (JSON cortado no meio)."""
    tc = gi._thinking_config("gemini-2.5-flash", 0)
    assert tc is not None and tc.thinking_budget == 0


@pytest.mark.parametrize("modulo,nome", [
    (dou_monitor, "dou_monitor"),
    (voice, "voice"),
    (translator, "translator"),
])
def test_nenhum_caminho_chama_generate_content_direto(modulo, nome) -> None:
    """Chamada direta escapa da queda automática. Se alguém adicionar uma,
    este teste acusa antes de virar '❌ erro' em produção."""
    fonte = inspect.getsource(modulo)
    assert "client.models.generate_content(" not in fonte, (
        f"{nome} voltou a chamar generate_content direto — sem queda do "
        "thinking, um modelo novo derruba esse caminho inteiro"
    )


@pytest.mark.parametrize("modulo", [dou_monitor, voice, translator])
def test_os_tres_usam_o_helper(modulo) -> None:
    assert "gerar(" in inspect.getsource(modulo)


def test_helper_e_publico() -> None:
    """Quatro módulos importam: nome público, não `_gerar` atravessado."""
    assert hasattr(gi, "gerar") and not hasattr(gi, "_gerar")
