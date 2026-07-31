"""Teto de tokens da OpenAI: `max_tokens` × `max_completion_tokens`.

Modelos de raciocínio (o1/o3/o4, gpt-5…) recusam `max_tokens` — e o /provider
oferece esses ids. Escolher um deles quebrava TODO chat com "❌ erro no LLM".
"""
from __future__ import annotations

import pytest

from bot.services.llm.openai_impl import (
    _MIN_REASONING_TOKENS,
    OpenAIProvider,
    _campo_teto,
)


@pytest.mark.parametrize(
    "model,esperado",
    [
        ("gpt-4o", "max_tokens"),
        ("gpt-4.1-mini", "max_tokens"),
        ("chatgpt-4o-latest", "max_tokens"),
        ("o1", "max_completion_tokens"),
        ("o3-mini", "max_completion_tokens"),
        ("o4-mini", "max_completion_tokens"),
        ("gpt-5.1", "max_completion_tokens"),
        ("GPT-5", "max_completion_tokens"),
    ],
)
def test_campo_teto_por_modelo(model: str, esperado: str) -> None:
    assert _campo_teto(model) == esperado


class _FakeCompletions:
    """Dublê que registra a chamada e pode recusar um dos parâmetros."""

    def __init__(self, recusa: str | None = None) -> None:
        self.recusa = recusa
        self.chamadas: list[dict] = []

    def create(self, **kwargs):
        self.chamadas.append(kwargs)
        if self.recusa and self.recusa in kwargs:
            raise ValueError(
                f"Unsupported parameter: '{self.recusa}' is not supported with "
                "this model."
            )
        return "RESP"


def _provider(model: str, recusa: str | None = None) -> tuple[OpenAIProvider, _FakeCompletions]:
    p = OpenAIProvider.__new__(OpenAIProvider)  # sem tocar na API real
    p.model = model
    fake = _FakeCompletions(recusa)
    p.client = type("C", (), {"chat": type("Ch", (), {"completions": fake})()})()
    return p, fake


def test_modelo_normal_usa_max_tokens() -> None:
    p, fake = _provider("gpt-4o")
    assert p._create(max_tokens=1024, model="gpt-4o", messages=[]) == "RESP"
    assert fake.chamadas[0]["max_tokens"] == 1024
    assert "max_completion_tokens" not in fake.chamadas[0]


def test_modelo_de_raciocinio_usa_max_completion_tokens_com_piso() -> None:
    p, fake = _provider("o3-mini")
    p._create(max_tokens=1024, model="o3-mini", messages=[])
    assert "max_tokens" not in fake.chamadas[0]
    # o piso existe porque o teto cobre os tokens de raciocínio: com 1024 a
    # resposta voltaria vazia.
    assert fake.chamadas[0]["max_completion_tokens"] == _MIN_REASONING_TOKENS


def test_modelo_desconhecido_que_recusa_troca_de_parametro_e_refaz() -> None:
    """Prefixo é palpite; a mensagem da API é fato — modelo novo fora da lista
    não pode quebrar o chat."""
    p, fake = _provider("gpt-7-turbo", recusa="max_tokens")
    assert p._create(max_tokens=800, model="gpt-7-turbo", messages=[]) == "RESP"
    assert len(fake.chamadas) == 2
    assert fake.chamadas[0]["max_tokens"] == 800
    assert fake.chamadas[1]["max_completion_tokens"] == _MIN_REASONING_TOKENS


def test_erro_alheio_ao_teto_sobe_sem_retry() -> None:
    p, fake = _provider("gpt-4o")
    fake.recusa = "messages"
    with pytest.raises(ValueError):
        p._create(max_tokens=100, model="gpt-4o", messages=[])
    assert len(fake.chamadas) == 1
