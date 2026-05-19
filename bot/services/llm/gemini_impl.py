from __future__ import annotations

import asyncio

import google.generativeai as genai

from bot.services.llm.base import ChatMessage, LLMProvider


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY ausente")
        genai.configure(api_key=api_key)
        self.model_name = model

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> str:
        # Gemini: histórico estilo {role: "user"|"model", parts: [text]}.
        history: list[dict] = []
        last_user: str | None = None
        for m in messages:
            role = "user" if m["role"] == "user" else "model"
            if m is messages[-1] and m["role"] == "user":
                last_user = m["content"]
                break
            history.append({"role": role, "parts": [m["content"]]})
        if last_user is None and messages:
            last_user = messages[-1]["content"]

        def _call() -> str:
            model = genai.GenerativeModel(
                self.model_name,
                system_instruction=system,
                generation_config={"max_output_tokens": max_tokens},
            )
            chat = model.start_chat(history=history)
            resp = chat.send_message(last_user or "")
            return (resp.text or "").strip()

        return await asyncio.to_thread(_call)
