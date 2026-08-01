"""Teto de thinking do Gemini por usuário (/provider thinking).

Vivia só no .env, mas o valor útil depende do MODELO: o mesmo budget 0 que
economiza no 2.5-flash é recusado com 400 pelo 3.6-flash. Trocar de modelo é
um comando; acertar o budget não podia exigir deploy.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.services.llm import gemini_impl as gi
from bot.services.llm.factory import get_provider_for_user


@pytest.fixture(autouse=True)
def _limpa():
    gi._SEM_THINKING_BUDGET.clear()
    yield
    gi._SEM_THINKING_BUDGET.clear()


@pytest.fixture
def env_zero(monkeypatch):
    from bot.config import settings
    monkeypatch.setattr(settings, "gemini_thinking_budget", 0)


def test_usuario_sobrepoe_o_env(env_zero) -> None:
    assert gi.budget_efetivo(-1) == -1, "escolha do usuário tem que vencer o .env"
    assert gi.budget_efetivo(512) == 512


def test_sem_escolha_segue_o_env(env_zero) -> None:
    assert gi.budget_efetivo(None) == 0


def test_zero_do_env_nao_vira_automatico(env_zero) -> None:
    """`v or -1` transformaria 0 (desliga) em -1 (automático) — 0 é valor
    válido, não ausência."""
    assert gi.budget_efetivo(None) == 0
    assert gi._thinking_config("gemini-2.5-flash", None) is not None


def test_usuario_pode_desligar_com_env_automatico(monkeypatch) -> None:
    from bot.config import settings
    monkeypatch.setattr(settings, "gemini_thinking_budget", -1)
    assert gi.budget_efetivo(0) == 0
    tc = gi._thinking_config("gemini-2.5-flash", 0)
    assert tc is not None and tc.thinking_budget == 0


def test_automatico_nao_manda_nada(monkeypatch) -> None:
    from bot.config import settings
    monkeypatch.setattr(settings, "gemini_thinking_budget", 0)
    assert gi._thinking_config("gemini-2.5-flash", -1) is None


def test_clamp_do_pro_continua_valendo(monkeypatch) -> None:
    """O pro não aceita desligar (mín ~128); o clamp antigo segue de pé."""
    tc = gi._thinking_config("gemini-2.5-pro", 0)
    assert tc is not None and tc.thinking_budget == 128


def test_provider_do_usuario_carrega_o_budget(monkeypatch) -> None:
    """O caminho todo: coluna do usuário → factory → cliente Gemini."""
    from bot.config import settings
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    user = SimpleNamespace(
        provider="gemini", gemini_model="gemini-3.6-flash",
        anthropic_model=None, openai_model=None, gemini_thinking_budget=256,
    )
    prov = get_provider_for_user(user)
    assert prov.thinking_budget == 256
    assert prov.model == "gemini-3.6-flash"


def test_usuarios_com_budgets_diferentes_nao_se_misturam(monkeypatch) -> None:
    """O factory é cacheado; se o budget não entrasse na chave, o segundo
    usuário herdaria o provider do primeiro."""
    from bot.config import settings
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    base = dict(provider="gemini", gemini_model="gemini-2.5-flash",
                anthropic_model=None, openai_model=None)
    a = get_provider_for_user(SimpleNamespace(**base, gemini_thinking_budget=0))
    b = get_provider_for_user(SimpleNamespace(**base, gemini_thinking_budget=512))
    assert (a.thinking_budget, b.thinking_budget) == (0, 512)
