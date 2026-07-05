"""Fixtures/base dos testes.

`bot.config.Settings` exige BOT_TOKEN e ACCESS_PASSWORD no import (pydantic
`Field(...)`). Setamos valores dummy ANTES de qualquer import de `bot.*` pra
os testes rodarem sem um .env real.
"""
import os

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("ACCESS_PASSWORD", "test-pass")
