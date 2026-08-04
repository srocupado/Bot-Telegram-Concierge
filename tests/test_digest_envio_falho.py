"""Falha de ENVIO de digest não pode re-executar o fetch a cada tick.

Auditoria de 03/08/2026: com o fetch OK e o envio falhando (chat bloqueado,
rede do Pi fora), o dia não era marcado e o tick seguinte (60s) refazia o
scrape/chamada ao Google Maps — ~900 chamadas pagas por dia de pane, sem
ninguém saber. Agora a MENSAGEM pronta é cacheada (chave = dia/semana): o
fetch roda 1x e as re-tentativas só re-enviam.

E o _send_html_with_fallback do scheduled_actions não protegia a SEGUNDA
tentativa: a exceção subia até o run_reminders, o Reminder não era consumido
e a ação agendada re-rodava (com fetch pago) a cada 60s pra sempre.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.db.models import Base, User
from bot.services import scheduler


class _Bot:
    def __init__(self, falhas_iniciais: int = 0):
        self.falhas_restantes = falhas_iniciais
        self.enviadas: list[str] = []

    async def send_message(self, chat_id, text, **kw):
        if self.falhas_restantes > 0:
            self.falhas_restantes -= 1
            raise RuntimeError("rede fora")
        self.enviadas.append(text)


class _DT(datetime):
    """datetime com now() fixo numa SEGUNDA às 7h30 BRT (dia do digest)."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 8, 3, 7, 30, tzinfo=tz)


@pytest.fixture(autouse=True)
def _cache_limpo(monkeypatch):
    scheduler._congress_digest_cache.update(key=None, message=None)
    scheduler._traffic_digest_cache.update(key=None, message=None)
    monkeypatch.setattr(scheduler, "datetime", _DT)
    yield
    scheduler._congress_digest_cache.update(key=None, message=None)
    scheduler._traffic_digest_cache.update(key=None, message=None)


def test_congress_digest_nao_refaz_fetch_quando_o_envio_falha(monkeypatch) -> None:
    fetches = {"n": 0}

    async def _fetch(client, today):
        fetches["n"] += 1
        return []

    monkeypatch.setattr(scheduler, "fetch_week_mps", _fetch)
    monkeypatch.setattr(scheduler.settings, "congress_digest_enabled", True)

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    sm = async_sessionmaker(engine, expire_on_commit=False)
    # HTML falha e o fallback texto-puro também → 1º tick sem entrega.
    bot = _Bot(falhas_iniciais=2)

    async def _main():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sm() as s:
            s.add(User(id=1, chat_id=1, is_authorized=True,
                       congress_subscribed=True))
            await s.commit()
        await scheduler.run_congress_digest(sm, bot)   # tick 1: fetch + envio falha
        await scheduler.run_congress_digest(sm, bot)   # tick 2: cache + entrega
        await scheduler.run_congress_digest(sm, bot)   # tick 3: dia marcado, nada
        async with sm() as s:
            u = (await s.scalars(select(User))).one()
            return u.last_congress_digest_at

    marcado = asyncio.run(_main())
    assert fetches["n"] == 1, (
        f"fetch refeito {fetches['n']}x — era a chuva de scrapes da pane de envio"
    )
    assert len(bot.enviadas) == 1, "digest não foi entregue na re-tentativa"
    assert "Sem MP esta semana" in bot.enviadas[0]
    assert marcado is not None, "dia não marcado após entrega OK"


def test_send_fallback_do_agendado_nao_deixa_excecao_subir() -> None:
    from bot.services.scheduled_actions import _send_html_with_fallback

    bot = _Bot(falhas_iniciais=99)   # tudo falha, inclusive o fallback
    asyncio.run(_send_html_with_fallback(bot, 1, "<b>oi</b>"))
    # Sem exceção = o Reminder é consumido e a ação não re-roda a cada 60s.
    assert bot.enviadas == []
