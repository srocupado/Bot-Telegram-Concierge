"""Revisão geral de 11/11/2026 — monitor de MP (itens que precisam do
collect_mp inteiro, com banco em memória)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.models import Base, ProactiveNotice, User
from bot.services import dou_planalto, dou_portal, proactive

HOJE = datetime.now(proactive.BRT).date()


async def _setup(marca):
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    s = async_sessionmaker(eng, expire_on_commit=False)()
    u = User(id=77, chat_id=77, is_authorized=True, dou_mp_subscribed=True,
             dou_ultimo_dia_ok=marca)
    s.add(u)
    await s.commit()
    return eng, s, u


def test_marca_dagua_nao_avanca_com_aviso_de_lacuna_pendente(monkeypatch) -> None:
    """Bot fora 3 semanas: o aviso dos dias velhos demais ainda não foi
    entregue e a marca já ia pra ontem — envio falho engolia esses dias."""
    monkeypatch.setattr(proactive.settings, "dou_planalto_enabled", False)
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", True)

    async def _portal(d, controle=None):
        return dou_portal.PortalDia([], True)
    monkeypatch.setattr(dou_portal, "checar_dia_portal", _portal)

    async def _run():
        marca = HOJE - timedelta(days=21)
        eng, s, u = await _setup(marca)
        try:
            f1 = await proactive.collect_mp(s, u, [HOJE - timedelta(days=1), HOJE])
            assert any(f.kind == "mp_lacuna" for f in f1)
            assert u.dou_ultimo_dia_ok == marca, "marca avançou antes do envio"
            # o envio "falhou" (pós-envio não rodou): a janela seguinte repete
            f2 = await proactive.collect_mp(s, u, [HOJE])
            assert any(f.kind == "mp_lacuna" for f in f2)
        finally:
            await s.close()
            await eng.dispose()
    asyncio.run(_run())


def test_mp_anunciada_sem_nota_nao_trava_a_baixa_pelo_planalto(monkeypatch) -> None:
    """Nota da 1399 não pedida: a régua (só notas entregues) ficava em 1398,
    a sonda devolvia a 1399 pra sempre e 'sem novas' nunca ligava."""
    monkeypatch.setattr(proactive.settings, "dou_planalto_enabled", True)
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", True)
    chamadas = []

    async def _sonda(ultimo, anos, **kw):
        chamadas.append((ultimo, kw.get("conhecidas")))
        return []

    async def _portal(d, controle=None):
        raise dou_portal.PortalError("502")
    monkeypatch.setattr(dou_planalto, "sondar_novas", _sonda)
    monkeypatch.setattr(dou_portal, "checar_dia_portal", _portal)

    async def _run():
        from bot.db.models import DouSeenMP
        eng, s, u = await _setup(HOJE - timedelta(days=3))
        try:
            s.add(DouSeenMP(user_id=77, numero="1398", ano=2026))
            s.add(ProactiveNotice(user_id=77, kind="mp", key="1399/2026"))
            await s.commit()
            fechado = HOJE - timedelta(days=2)
            facts = await proactive.collect_mp(s, u, [fechado])
            assert chamadas and chamadas[0][0] == 1399
            assert chamadas[0][1] == {1398, 1399}
            assert any(f.kind == "mp_retro" and "rede do Planalto" in f.text
                       and "1.399/2026" in f.text for f in facts)
        finally:
            await s.close()
            await eng.dispose()
    asyncio.run(_run())
