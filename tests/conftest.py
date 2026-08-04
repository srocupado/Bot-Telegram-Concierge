"""Fixtures/base dos testes.

`bot.config.Settings` exige BOT_TOKEN e ACCESS_PASSWORD no import (pydantic
`Field(...)`). Setamos valores dummy ANTES de qualquer import de `bot.*` pra
os testes rodarem sem um .env real.
"""
import os

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("ACCESS_PASSWORD", "test-pass")

import pytest


@pytest.fixture(autouse=True)
def _reset_inlabs_sessao():
    """A sessão do Inlabs é cache module-level (reuso de cookie entre fetches).
    Sem resetar, um teste que loga vaza o cookie pro próximo — que aí pula o
    login e não exercita o que devia. Zera antes de cada teste."""
    try:
        from bot.services import dou_monitor
        dou_monitor._SESSION["cookie"] = None
        dou_monitor._SESSION["ts"] = 0.0
    except Exception:
        pass
    yield
