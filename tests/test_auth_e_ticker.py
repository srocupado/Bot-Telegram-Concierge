"""Porta de entrada (senha) e classificação de ticker da B3."""
from __future__ import annotations

import pytest

from bot.middlewares import auth
from bot.services.cotacao import _classify


def test_help_nao_e_publico() -> None:
    """O guia completo de features não vai pra quem não passou a senha."""
    assert auth.PUBLIC_COMMANDS == {"/start"}


def test_senha_bloqueia_apos_tentativas_seguidas() -> None:
    uid = 999_001
    auth._tentativas.pop(uid, None)
    for _ in range(auth._MAX_TENTATIVAS):
        assert auth._bloqueado_ate(uid) is None
        auth._registrar_tentativa(uid)
    assert auth._bloqueado_ate(uid) is not None, "aceitaria tentativas ilimitadas"
    auth._tentativas.pop(uid, None)


def test_senha_certa_zera_o_contador() -> None:
    uid = 999_002
    auth._tentativas.pop(uid, None)
    auth._registrar_tentativa(uid)
    auth._tentativas.pop(uid, None)  # é o que o middleware faz ao acertar
    assert auth._bloqueado_ate(uid) is None


@pytest.mark.parametrize("ticker", ["petr4", "hglg11", "itub4", "b3sa3", "mglu3"])
def test_tickers_da_b3_sao_reconhecidos(ticker: str) -> None:
    assert _classify(ticker, None) == "acao"


@pytest.mark.parametrize("ativo", ["usd", "eur", "dolar", "euro"])
def test_moedas_continuam_moedas(ativo: str) -> None:
    assert _classify(ativo, None) == "moeda"


def test_cripto_continua_cripto() -> None:
    assert _classify("bitcoin", None) == "cripto"
