from __future__ import annotations

from functools import lru_cache

from bot.config import settings
from bot.services.llm.anthropic_impl import AnthropicProvider
from bot.services.llm.base import LLMProvider
from bot.services.llm.gemini_impl import GeminiProvider
from bot.services.llm.openai_impl import OpenAIProvider


@lru_cache(maxsize=8)
def _build(name: str) -> LLMProvider:
    if name == "anthropic":
        return AnthropicProvider(settings.anthropic_api_key or "", settings.anthropic_model)
    if name == "openai":
        return OpenAIProvider(settings.openai_api_key or "", settings.openai_model)
    if name == "gemini":
        return GeminiProvider(settings.gemini_api_key or "", settings.gemini_model)
    raise ValueError(f"provider desconhecido: {name}")


def get_provider(name: str | None = None) -> LLMProvider:
    return _build(name or settings.ai_provider)


SUPPORTED_PROVIDERS = ("anthropic", "openai", "gemini")
