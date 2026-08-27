"""Texto DO USUÁRIO dentro de mensagem com Markdown não pode sumir.

Item 15 da varredura de 26/08/2026, promovido a prioridade em 27/08 depois
que a MESMA classe matou o /tool_ativar em produção (o '_' de 'onvif_scan'
abriu um itálico sem fim e o Telegram recusou a mensagem inteira).

Nos comandos daqui o efeito é pior que uma tela feia:
- /lembrar: o lembrete JÁ está gravado quando a confirmação falha — o dono
  acha que não funcionou, repete, e fica com dois;
- /tarefas: UMA tarefa com '_' quebra a listagem inteira, e ela segue
  quebrada enquanto a tarefa existir;
- /apagar_lembrete: apagou de verdade e não confirmou.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest

from bot.handlers._send import answer_md


class _MsgQuebraMarkdown:
    """Telegram recusando Markdown, como faz de verdade com marcador solto."""

    def __init__(self):
        self.enviadas: list[tuple[str, str | None]] = []

    async def answer(self, texto, parse_mode=None, **kw):
        if parse_mode == "Markdown":
            raise TelegramBadRequest(
                method=None,
                message="can't parse entities: Can't find end of the entity",
            )
        self.enviadas.append((texto, parse_mode))


class _MsgOk:
    def __init__(self):
        self.enviadas: list[tuple[str, str | None]] = []

    async def answer(self, texto, parse_mode=None, **kw):
        self.enviadas.append((texto, parse_mode))


def test_markdown_ok_mantem_formatacao() -> None:
    m = _MsgOk()
    asyncio.run(answer_md(m, "🔔 *comprar pão*", plano="🔔 comprar pão"))
    assert m.enviadas == [("🔔 *comprar pão*", "Markdown")]


def test_markdown_recusado_entrega_versao_plana() -> None:
    m = _MsgQuebraMarkdown()
    asyncio.run(answer_md(m, "🔔 *pagar João_Silva*", plano="🔔 pagar João_Silva"))
    assert m.enviadas == [("🔔 pagar João_Silva", None)], (
        "mensagem tem que CHEGAR — sem o fallback ela some inteira")


def test_sem_plano_reenvia_o_mesmo_texto() -> None:
    """Feio (asteriscos à mostra), porém íntegro: perder conteúdo é pior."""
    m = _MsgQuebraMarkdown()
    asyncio.run(answer_md(m, "🔔 *x_y*"))
    assert m.enviadas == [("🔔 *x_y*", None)]


# ───────────────── os três comandos que sofriam do defeito ─────────────────

def _user():
    return SimpleNamespace(id=1, timezone="America/Sao_Paulo",
                           viagem_destino=None, viagem_inicio=None,
                           viagem_fim=None, viagem_tz=None)


def test_lembrar_confirma_mesmo_com_underscore(monkeypatch) -> None:
    from bot.handlers import reminders as rh

    async def _criar(_s, _uid, texto, _due):
        return SimpleNamespace(id=7, text=texto)

    monkeypatch.setattr(rh, "create_reminder", _criar)
    monkeypatch.setattr(
        rh, "parse_reminder",
        lambda raw, tz: ("pagar João_Silva", datetime(2026, 9, 1, 12, tzinfo=timezone.utc)),
    )
    m = _MsgQuebraMarkdown()
    cmd = SimpleNamespace(args="pagar João_Silva amanhã 9h")
    asyncio.run(rh.cmd_lembrar(m, cmd, _user(), None))

    assert len(m.enviadas) == 1, "confirmação sumiu → dono repete e duplica"
    texto, modo = m.enviadas[0]
    assert modo is None and "pagar João_Silva" in texto and "#7" in texto


def test_tarefas_lista_mesmo_com_underscore_em_uma(monkeypatch) -> None:
    from bot.handlers import tasks as th

    tarefas = [
        SimpleNamespace(id=1, text="comprar pão",
                        created_at=datetime.now(timezone.utc)),
        SimpleNamespace(id=2, text="revisar função get_user",
                        created_at=datetime.now(timezone.utc)),
    ]

    async def _listar(_s, _uid):
        return tarefas

    monkeypatch.setattr(th, "list_open_tasks", _listar)
    m = _MsgQuebraMarkdown()
    asyncio.run(th.cmd_tarefas(m, _user(), None))

    assert len(m.enviadas) == 1, "uma tarefa com '_' derrubava a lista inteira"
    texto, modo = m.enviadas[0]
    assert modo is None
    assert "comprar pão" in texto and "get_user" in texto, (
        "as duas tarefas precisam aparecer no fallback")


def test_apagar_lembrete_confirma_mesmo_com_underscore(monkeypatch) -> None:
    from bot.handlers import reminders as rh

    async def _apagar(_s, _uid, rid):
        return SimpleNamespace(id=rid, text="pagar João_Silva")

    monkeypatch.setattr(rh, "delete_reminder", _apagar)
    m = _MsgQuebraMarkdown()
    cmd = SimpleNamespace(args="7")
    asyncio.run(rh.cmd_apagar_lembrete(m, cmd, _user(), None))

    assert len(m.enviadas) == 1
    texto, modo = m.enviadas[0]
    assert modo is None and "apagado" in texto and "pagar João_Silva" in texto
