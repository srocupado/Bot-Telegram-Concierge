"""Resposta sem texto do Anthropic não vira "(sem resposta)" (27/11/2026:
Opus 5.5 por voz, "filmes em cartaz no Cine Araújo")."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bot.services.llm import anthropic_impl as ai
from bot.services.llm.base import ToolContext


def _resp(stop, *blocos):
    return SimpleNamespace(stop_reason=stop, content=list(blocos),
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def _texto(t):
    return SimpleNamespace(type="text", text=t)


def _rodar(respostas):
    chamadas = []

    def _create(**kw):
        chamadas.append(kw["max_tokens"])
        return respostas.pop(0)
    prov = ai.AnthropicProvider.__new__(ai.AnthropicProvider)
    prov.model = "claude-opus-5-5"
    prov.client = SimpleNamespace(messages=SimpleNamespace(create=_create))
    ctx = ToolContext(user=SimpleNamespace(id=1), session=None,
                      tz="America/Sao_Paulo", user_text="x")
    out = asyncio.run(prov.chat_with_tools(
        [{"role": "user", "content": "filmes no Cine Araújo hoje"}], [], ctx,
        system="s", max_tokens=600))
    return out, chamadas


def test_cortada_sem_texto_refaz_com_teto_maior() -> None:
    out, chamadas = _rodar([
        _resp("max_tokens", SimpleNamespace(type="thinking")),
        _resp("end_turn", _texto("Em cartaz: A, B, C")),
    ])
    assert out == "Em cartaz: A, B, C"
    assert chamadas == [600, ai.TETO_AMPLIADO]


def test_vazia_refaz_uma_vez() -> None:
    out, chamadas = _rodar([_resp("end_turn"), _resp("end_turn", _texto("oi"))])
    assert out == "oi" and len(chamadas) == 2


def test_vazia_duas_vezes_diz_isso_em_vez_de_sem_resposta() -> None:
    out, chamadas = _rodar([_resp("end_turn"), _resp("end_turn")])
    assert "terminou sem escrever resposta" in out and "end_turn" in out
    assert len(chamadas) == 2
