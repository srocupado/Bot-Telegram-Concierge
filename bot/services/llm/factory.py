from __future__ import annotations

from functools import lru_cache

from bot.config import settings
from bot.services.llm.anthropic_impl import AnthropicProvider
from bot.services.llm.base import LLMProvider
from bot.services.llm.gemini_impl import GeminiProvider
from bot.services.llm.openai_impl import OpenAIProvider


@lru_cache(maxsize=16)
def _build(name: str, gemini_model: str | None = None) -> LLMProvider:
    if name == "anthropic":
        return AnthropicProvider(settings.anthropic_api_key or "", settings.anthropic_model)
    if name == "openai":
        return OpenAIProvider(settings.openai_api_key or "", settings.openai_model)
    if name == "gemini":
        return GeminiProvider(settings.gemini_api_key or "", gemini_model or settings.gemini_model)
    raise ValueError(f"provider desconhecido: {name}")


def get_provider(name: str | None = None, *, gemini_model: str | None = None) -> LLMProvider:
    """gemini_model: override do modelo Gemini por usuário (/provider gemini
    pro|flash). Ignorado pelos outros providers."""
    return _build(name or settings.ai_provider, gemini_model)


SUPPORTED_PROVIDERS = ("anthropic", "openai", "gemini")
