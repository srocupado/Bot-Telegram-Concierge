"""run_reminders de ponta a ponta (banco em memória).

Regressão real (31/07/2026, commit 7dd2d71 — achada na auditoria de 03/08):
`run_reminders` usava `effective_tz` SEM import no módulo (só `run_proactive`
tinha, local). O lembrete recorrente era ENVIADO, o reagendamento estourava
`NameError`, o rollback mantinha `sent=False` → reenvio a cada 60s pra
sempre. Nenhum teste chamava `run_reminders` (só `next_due_from` isolado).

Segundo bug do mesmo lote: o `rollback()` do tratamento de erro expirava
TODOS os objetos ORM da sessão única compartilhada — uma falha de envio
derrubava (MissingGreenlet) os lembretes restantes de todos os usuários do
tick. Agora cada lembrete roda em sessão própria.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.db.models import Base, Reminder, User
from bot.services.scheduler import run_reminders


class _FakeBot:
    """Registra envios; falha para os chat_ids configurados."""

    def __init__(self, falhar_para: set[int] | None = None):
        self.enviadas: list[tuple[int, str]] = []
        self.falhar_para = falhar_para or set()

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.falhar_para:
            raise RuntimeError("chat bloqueado")
        self.enviadas.append((chat_id, text))


def _sessionmaker():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _prep(sm, *reminders_por_user):
    """Cria N usuários (ids 1..N) e seus lembretes: cada item é uma lista de
    dicts com kwargs extras do Reminder."""
    agora = datetime.now(timezone.utc)
    async with sm() as s:
        for uid, rems in enumerate(reminders_por_user, start=1):
            s.add(User(id=uid, chat_id=uid, is_authorized=True))
            for kw in rems:
                s.add(Reminder(
                    user_id=uid,
                    text=kw.pop("text", "lembrete"),
                    due_at=kw.pop("due_at", agora - timedelta(minutes=5)),
                    **kw,
                ))
        await s.commit()
    return agora


def test_recorrente_e_enviado_e_reagendado() -> None:
    """O caso da regressão: recorrente dispara UMA vez e o due_at avança —
    este teste quebra com NameError no código de 31/07."""
    engine, sm = _sessionmaker()
    bot = _FakeBot()

    async def _main():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        agora = await _prep(sm, [{"text": "água", "recurrence": "daily"}])
        await run_reminders(sm, bot)
        # Segundo tick imediato: NÃO pode reenviar (o bug reenviava a cada 60s).
        await run_reminders(sm, bot)
        async with sm() as s:
            rem = (await s.scalars(select(Reminder))).one()
            return agora, rem.due_at, rem.sent

    agora, due_at, sent = asyncio.run(_main())
    assert len(bot.enviadas) == 1, "recorrente reenviado (loop de 60s da regressão)"
    assert "água" in bot.enviadas[0][1]
    assert sent is False, "recorrente deve continuar armado pro próximo disparo"
    due = due_at if due_at.tzinfo else due_at.replace(tzinfo=timezone.utc)
    assert due > agora, "due_at não avançou — reagendamento falhou"


def test_falha_de_um_usuario_nao_derruba_o_lote() -> None:
    """Chat do usuário 1 bloqueado: o lembrete do usuário 2 tem que sair
    mesmo assim (antes, o rollback expirava a sessão única e o lote todo
    morria em MissingGreenlet)."""
    engine, sm = _sessionmaker()
    bot = _FakeBot(falhar_para={1})

    async def _main():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _prep(sm, [{"text": "do bloqueado"}], [{"text": "do ok"}])
        await run_reminders(sm, bot)
        async with sm() as s:
            rems = {r.text: r for r in (await s.scalars(select(Reminder))).all()}
            return {t: (r.sent,) for t, r in rems.items()}

    estados = asyncio.run(_main())
    assert [c for c, _ in bot.enviadas] == [2], "lembrete do usuário 2 não saiu"
    assert estados["do ok"][0] is True, "entregue mas não marcado como sent"
    # O do bloqueado fica pendente (retenta no próximo tick) — sem sumir.
    assert estados["do bloqueado"][0] is False


def test_nao_recorrente_e_marcado_como_enviado() -> None:
    engine, sm = _sessionmaker()
    bot = _FakeBot()

    async def _main():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _prep(sm, [{"text": "pagar conta"}])
        await run_reminders(sm, bot)
        await run_reminders(sm, bot)   # não pode duplicar
        async with sm() as s:
            rem = (await s.scalars(select(Reminder))).one()
            return rem.sent, rem.sent_at

    sent, sent_at = asyncio.run(_main())
    assert len(bot.enviadas) == 1
    assert sent is True and sent_at is not None
