"""/provider: id conferido × aceito sem conferir.

Regressão real: `/provider gemini gemini-3.6-flash` respondeu "✅ Provider
definido" e, na mensagem seguinte, todo chat virou
"❌ erro no LLM (gemini): 400 INVALID_ARGUMENT".

A validação consulta a Models API viva, mas devolvia True quando a consulta
FALHAVA ("pra não travar o usuário"). O ✅ saía idêntico ao de um id
conferido, então o dono não tinha como ligar o erro seguinte ao comando que
o causou.
"""
from __future__ import annotations

import asyncio

import pytest

from bot.handlers import provider as prov_mod


def _catalogo(monkeypatch, modelos):
    async def _list(_provider, _modality="text"):
        return modelos

    monkeypatch.setattr(prov_mod.catalog, "list_models", _list)


def test_id_da_lista_e_valido(monkeypatch) -> None:
    _catalogo(monkeypatch, [("gemini-2.5-pro", "Pro"), ("gemini-2.5-flash", "Flash")])
    assert asyncio.run(prov_mod._is_valid_id("gemini", "gemini-2.5-pro")) is True


def test_id_fora_da_lista_e_invalido(monkeypatch) -> None:
    """O caso do 3.6-flash: a API respondeu e o id não estava lá."""
    _catalogo(monkeypatch, [("gemini-2.5-pro", "Pro")])
    assert asyncio.run(prov_mod._is_valid_id("gemini", "gemini-3.6-flash")) is False


def test_api_fora_devolve_none_e_nao_true(monkeypatch) -> None:
    """None ≠ válido. É o que permite avisar em vez de fingir conferência."""
    _catalogo(monkeypatch, [])
    assert asyncio.run(prov_mod._is_valid_id("gemini", "gemini-3.6-flash")) is None


@pytest.mark.parametrize("modelos,esperado", [([], True), ([("gemini-2.5-pro", "P")], False)])
def test_voice_continua_permissivo(monkeypatch, modelos, esperado) -> None:
    """O /voice mantém o comportamento antigo: só rejeita id que a API negou
    de fato. Trocar isso por 'None é falso' travaria o dono quando a API
    caísse — regressão que a mudança do /provider poderia arrastar junto."""
    _catalogo(monkeypatch, modelos)
    assert asyncio.run(prov_mod._is_valid_gemini_id("gemini-3.6-flash")) is esperado
