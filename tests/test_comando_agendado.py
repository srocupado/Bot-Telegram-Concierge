"""Comando LITERAL agendado (dono, 05/08/2026): agendado tipo 'chat' cujo
texto começa com '/' executa o handler REAL do comando, sem LLM no meio.

Antes o texto ia pro modelo, que PODIA rotear pra tool certa — variância
onde se quer determinismo ('agenda o /mp_dou_agora pras 10h' tem um
significado só). Reusa o registro da voz (_DISPATCH/_invocar).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiogram import Bot

from bot.handlers import voice
from bot.services import scheduled_actions

UID = 777


def _bot_capturador(envios: list) -> Bot:
    bot = Bot("123:ABC")

    async def _send(chat_id, text, **kw):
        envios.append((chat_id, text))
        return SimpleNamespace(message_id=1)

    object.__setattr__(bot, "send_message", _send)
    return bot


def _user():
    return SimpleNamespace(id=UID, is_authorized=True, timezone="America/Sao_Paulo")


def test_barra_executa_o_handler_real(monkeypatch) -> None:
    chamadas: dict = {}

    async def _fake_handler(message, command=None, user=None, session=None):
        chamadas.update(
            message_text=message.text, args=command.args if command else None,
            user_id=user.id, chat_id=message.chat.id, tem_bot=message.bot is not None,
        )

    monkeypatch.setitem(voice._DISPATCH, "mp_dou_agora", _fake_handler)
    envios: list = []
    bot = _bot_capturador(envios)

    asyncio.run(scheduled_actions.run_action(
        bot, session=None, user=_user(), kind="chat",
        args="/mp_dou_agora 05/08/2026",
    ))

    assert chamadas["args"] == "05/08/2026", "args do comando têm que chegar parseados"
    assert chamadas["user_id"] == UID and chamadas["chat_id"] == UID
    assert chamadas["tem_bot"], "Message sintética precisa do bot pro .answer() funcionar"
    # O dono vê a origem: eco '⏰ (agendado) /comando' antes da execução.
    assert any("(agendado) /mp_dou_agora" in t for _, t in envios)


def test_comando_desconhecido_avisa_em_vez_de_silenciar() -> None:
    envios: list = []
    bot = _bot_capturador(envios)

    asyncio.run(scheduled_actions.run_action(
        bot, session=None, user=_user(), kind="chat", args="/nao_existe_isso",
    ))

    assert len(envios) == 1
    assert "não existe" in envios[0][1]
    assert "/nao_existe_isso".lstrip("/") in envios[0][1]


def test_sem_barra_segue_pro_llm(monkeypatch) -> None:
    """Prompt normal continua no caminho antigo (chat com o modelo)."""
    chamado: dict = {}

    async def _fake_chat(bot, chat_id, user, session, prompt):
        chamado["prompt"] = prompt

    monkeypatch.setattr(scheduled_actions, "_run_chat", _fake_chat)
    envios: list = []
    bot = _bot_capturador(envios)

    asyncio.run(scheduled_actions.run_action(
        bot, session=None, user=_user(), kind="chat",
        args="resumo dos meus gastos",
    ))

    assert chamado["prompt"] == "resumo dos meus gastos"
    assert envios == [], "prompt livre não ganha eco de comando"


def test_barra_com_mencao_do_bot_e_maiuscula_normaliza(monkeypatch) -> None:
    """'/MP_DOU_AGORA@ViniConciergeBot' → handler mp_dou_agora (mesma
    normalização que o Telegram aplica em grupos)."""
    chamadas: dict = {}

    async def _fake_handler(message, command=None, user=None, session=None):
        chamadas["ok"] = True

    monkeypatch.setitem(voice._DISPATCH, "mp_dou_agora", _fake_handler)
    envios: list = []
    bot = _bot_capturador(envios)

    asyncio.run(scheduled_actions.run_action(
        bot, session=None, user=_user(), kind="chat",
        args="/MP_DOU_AGORA@ViniConciergeBot",
    ))
    assert chamadas.get("ok") is True


def test_help_documenta_o_comando_literal() -> None:
    from bot.handlers.start import HELP_TEXT, find_help_sections
    assert "comando literal" in HELP_TEXT
    secoes = find_help_sections("como agendo um comando?")
    assert any("comando literal" in s for s in secoes)
