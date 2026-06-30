from __future__ import annotations

from functools import lru_cache

from bot.config import settings
from bot.services.llm.anthropic_impl import AnthropicProvider
from bot.services.llm.base import LLMProvider
from bot.services.llm.gemini_impl import GeminiProvider
from bot.services.llm.openai_impl import OpenAIProvider


@lru_cache(maxsize=32)
def _build(
    name: str,
    gemini_model: str | None = None,
    anthropic_model: str | None = None,
    openai_model: str | None = None,
) -> LLMProvider:
    if name == "anthropic":
        return AnthropicProvider(settings.anthropic_api_key or "", anthropic_model or settings.anthropic_model)
    if name == "openai":
        return OpenAIProvider(settings.openai_api_key or "", openai_model or settings.openai_model)
    if name == "gemini":
        return GeminiProvider(settings.gemini_api_key or "", gemini_model or settings.gemini_model)
    raise ValueError(f"provider desconhecido: {name}")


def get_provider(
    name: str | None = None,
    *,
    gemini_model: str | None = None,
    anthropic_model: str | None = None,
    openai_model: str | None = None,
) -> LLMProvider:
    """Overrides de modelo por usuário (/provider <prov> <id>). Cada um só vale
    quando o provider efetivo for o respectivo; os outros são ignorados."""
    return _build(name or settings.ai_provider, gemini_model, anthropic_model, openai_model)


def get_provider_for_user(user, name: str | None = None) -> LLMProvider:
    """Atalho: monta o provider já com TODOS os overrides de modelo do usuário.
    `name` permite forçar outro provider (ex.: visão) mantendo os modelos do user.
    """
    return get_provider(
        name or user.provider,
        gemini_model=user.gemini_model,
        anthropic_model=user.anthropic_model,
        openai_model=user.openai_model,
    )


SUPPORTED_PROVIDERS = ("anthropic", "openai", "gemini")
