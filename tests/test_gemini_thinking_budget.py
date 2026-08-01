"""thinking_budget que o modelo recusa não pode derrubar o chat.

Reproduzido contra a API viva em 01/08/2026, no Orange Pi:

  POST .../gemini-3.6-flash:generateContent
  {"contents":[{"role":"user","parts":[{"text":"oi"}]}]}                → 200
  ... + {"generationConfig":{"thinkingConfig":{"thinkingBudget":0}}}   → 400
        {"error":{"code":400,"message":"Request contains an invalid
                  argument.","status":"INVALID_ARGUMENT"}}

Com GEMINI_THINKING_BUDGET=0 no .env, TODA mensagem virava
"❌ erro no LLM (gemini/gemini-3.6-flash): 400 INVALID_ARGUMENT". O clamp que
existia cobria só modelo com "pro" no nome — heurístico de substring que
envelheceu junto com a lista de modelos.
"""
from __future__ import annotations

import logging

import pytest

from bot.services.llm import gemini_impl as gi


class _RespFake:
    text = "ok"


class _ClienteFake:
    """Recusa thinking_config como o gemini-3.6-flash faz."""

    def __init__(self, *, recusa_thinking=True):
        self.recusa = recusa_thinking
        self.chamadas: list[object] = []

    @property
    def models(self):
        return self

    def generate_content(self, *, model, contents, config):
        self.chamadas.append(getattr(config, "thinking_config", None))
        if self.recusa and getattr(config, "thinking_config", None) is not None:
            raise RuntimeError(
                "400 INVALID_ARGUMENT. {'error': {'code': 400, 'message': "
                "'Request contains an invalid argument.', "
                "'status': 'INVALID_ARGUMENT'}}"
            )
        return _RespFake()


@pytest.fixture(autouse=True)
def _limpa_cache():
    gi._SEM_THINKING_BUDGET.clear()
    yield
    gi._SEM_THINKING_BUDGET.clear()


@pytest.fixture
def budget_zero(monkeypatch):
    from bot.config import settings
    monkeypatch.setattr(settings, "gemini_thinking_budget", 0)


def test_400_no_thinking_vira_nova_tentativa_sem_ele(budget_zero, caplog) -> None:
    cli = _ClienteFake()
    with caplog.at_level(logging.WARNING):
        resp = gi.gerar(cli, "gemini-3.6-flash", [], "chat", max_output_tokens=8192)
    assert resp.text == "ok", "o chat tem que responder, não estourar"
    assert len(cli.chamadas) == 2, "deveria tentar com e sem thinking_config"
    assert cli.chamadas[0] is not None and cli.chamadas[1] is None
    assert any("recusou thinking_budget" in r.getMessage() for r in caplog.records), (
        "a queda tem que aparecer no log — silêncio esconde config inútil"
    )


def test_modelo_recusado_e_memorizado(budget_zero) -> None:
    """Sem memorizar, TODA mensagem pagaria a ida e volta dupla."""
    cli = _ClienteFake()
    gi.gerar(cli, "gemini-3.6-flash", [], "chat", max_output_tokens=8192)
    assert "gemini-3.6-flash" in gi._SEM_THINKING_BUDGET

    cli2 = _ClienteFake()
    gi.gerar(cli2, "gemini-3.6-flash", [], "chat", max_output_tokens=8192)
    assert cli2.chamadas == [None], "segunda mensagem já deve ir sem thinking"


def test_modelo_que_aceita_continua_com_thinking(budget_zero) -> None:
    """A queda é por modelo: quem aceita budget 0 não perde o ajuste."""
    cli = _ClienteFake(recusa_thinking=False)
    gi.gerar(cli, "gemini-2.5-flash", [], "chat", max_output_tokens=8192)
    assert len(cli.chamadas) == 1 and cli.chamadas[0] is not None
    assert "gemini-2.5-flash" not in gi._SEM_THINKING_BUDGET


def test_erro_que_nao_e_invalid_argument_sobe(budget_zero) -> None:
    """Só o 400 de argumento justifica repetir. Quota, rede e chave errada
    têm que continuar chegando ao usuário como são."""
    class _Quota(_ClienteFake):
        def generate_content(self, **kw):
            self.chamadas.append(None)
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

    cli = _Quota()
    with pytest.raises(RuntimeError, match="RESOURCE_EXHAUSTED"):
        gi.gerar(cli, "gemini-3.6-flash", [], "chat", max_output_tokens=8192)
    assert len(cli.chamadas) == 1, "não pode repetir erro que não é de argumento"


def test_sem_budget_configurado_nao_ha_o_que_cair(monkeypatch) -> None:
    """GEMINI_THINKING_BUDGET=-1 (padrão): nada é enviado, nada a repetir."""
    from bot.config import settings
    monkeypatch.setattr(settings, "gemini_thinking_budget", -1)
    cli = _ClienteFake()
    gi.gerar(cli, "gemini-3.6-flash", [], "chat", max_output_tokens=8192)
    assert cli.chamadas == [None]
