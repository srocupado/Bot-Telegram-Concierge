"""Log em JSON precisa sair com acento legível, não escapado.

O log do bot é todo em português. Com o padrão do json.dumps
(`ensure_ascii=True`), "não publicada" vira "n\\u00e3o publicada" e
`docker compose logs | grep "não publicada"` não acha nada — o log parece
vazio e uma correção que FUNCIONOU parece não ter rodado. Foi exatamente o
que aconteceu em 01/08/2026 ao conferir a classificação do Inlabs.
"""
from __future__ import annotations

import io
import json
import logging

from bot.logging_setup import make_formatter


def _linha_logada() -> str:
    """Loga uma linha com o MESMO formatter que roda em produção."""
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(make_formatter())
    lg = logging.getLogger("teste.acentos")
    lg.handlers, lg.propagate, lg.level = [h], False, logging.INFO
    lg.info("DO1 não publicada em 2026-08-01 (Inlabs serviu a listagem)")
    return buf.getvalue().strip()


def test_acento_sai_legivel_e_grepavel() -> None:
    linha = _linha_logada()
    assert "não publicada" in linha, (
        "acento escapado: grep em português não acha a linha e o log "
        f"parece vazio — saiu {linha!r}"
    )
    assert "\\u00e3" not in linha


def test_continua_sendo_json_valido() -> None:
    """Legibilidade não pode custar o parsing — UTF-8 é JSON válido."""
    dados = json.loads(_linha_logada())
    assert "não publicada" in dados["message"]
