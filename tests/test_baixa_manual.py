"""Baixa manual de pendência do DOU (pergunta do dono, 04/08/2026).

O /mp_dou_agora 03/08 rodou com sucesso ("Nenhuma MP nova"), mas o dia
continuava na fila retroativa — a baixa só existia dentro do ciclo proativo.
A checagem manual usa o MESMO pipeline de fetch; quando ela é conclusiva
(fetch completo + dia fechado), manter a pendência só re-compra o download
na janela seguinte — e nota_pendente da mesma data virava nota DUPLICADA.

A régua é a mesma da retroativa (_Colheita.baixa): na dúvida (incompleto,
dia aberto, nota que falhou), NADA muda — pendência fica.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.db.models import Base, ProactiveNotice, User
from bot.services import dou_monitor
from bot.services.proactive import BRT, baixa_checagem_manual, mark_notified

UID = 1


def _sm():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _dia_fechado() -> date:
    # 3 dias atrás em BRT: sempre encerrado (o dia fecha às 6h do seguinte),
    # em qualquer hora que o teste rode.
    return datetime.now(BRT).date() - timedelta(days=3)


async def _setup(sm, engine, *, marca: date | None, pendencias: list[tuple[str, str]]):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with sm() as s:
        s.add(User(id=UID, chat_id=UID, is_authorized=True,
                   dou_ultimo_dia_ok=marca))
        await s.commit()
        for kind, key in pendencias:
            await mark_notified(s, UID, kind, key)


async def _kinds_restantes(sm) -> set[tuple[str, str]]:
    async with sm() as s:
        rows = await s.scalars(select(ProactiveNotice).where(
            ProactiveNotice.user_id == UID,
            ProactiveNotice.kind.in_(("mp_pendente", "nota_pendente")),
        ))
        return {(r.kind, r.key) for r in rows}


def test_sem_mp_conclusivo_baixa_pendencias_e_avanca_marca() -> None:
    """O caso do dono: dia fechado, DOU saiu sem MP — mp_pendente E as
    nota_pendente da data (all e subset) saem da fila; marca d'água avança
    no passo contíguo."""
    d = _dia_fechado()
    engine, sm = _sm()

    async def _main():
        await _setup(sm, engine, marca=d - timedelta(days=1), pendencias=[
            ("mp_pendente", d.isoformat()),
            ("nota_pendente", f"{d.isoformat()}:all"),
            ("nota_pendente", f"{d.isoformat()}:1382"),
            ("mp_pendente", (d + timedelta(days=1)).isoformat()),  # outro dia: fica
        ])
        async with sm() as s:
            user = await s.get(User, UID)
            ok = await baixa_checagem_manual(s, user, d, 0, [], "sem_mp")
            return ok, await _kinds_restantes(sm), user.dou_ultimo_dia_ok

    ok, restantes, marca = asyncio.run(_main())
    assert ok is True
    assert restantes == {("mp_pendente", (d + timedelta(days=1)).isoformat())}, (
        "pendências da data checada tinham que sair; as de OUTRO dia, ficar"
    )
    assert marca == d, "marca d'água não avançou no passo contíguo"


def test_incompleto_e_dia_aberto_nao_dao_baixa() -> None:
    """Na dúvida é pendência: fonte incompleta e dia ainda aberto
    (sem_mp_extra/provisorio) mantêm a fila intacta."""
    d = _dia_fechado()
    engine, sm = _sm()

    async def _main():
        await _setup(sm, engine, marca=None, pendencias=[
            ("mp_pendente", d.isoformat()),
        ])
        outs = []
        async with sm() as s:
            user = await s.get(User, UID)
            for motivo in ("incompleto", "sem_mp_extra", "provisorio", None):
                outs.append(await baixa_checagem_manual(s, user, d, 0, [], motivo))
        return outs, await _kinds_restantes(sm)

    outs, restantes = asyncio.run(_main())
    assert outs == [False, False, False, False]
    assert restantes == {("mp_pendente", d.isoformat())}


def test_entrega_com_mp_precisa_de_checagem_completa_recente(monkeypatch) -> None:
    """entregues>0 não diz sozinho se o fetch foi completo — a evidência é a
    memória do processo (_ultima_ok, que SÓ registra checagem completa)."""
    d = _dia_fechado()
    engine, sm = _sm()

    async def _main(esperado_ultima_ok: bool):
        await _setup(sm, engine, marca=None, pendencias=[
            ("mp_pendente", d.isoformat()),
        ])
        async with sm() as s:
            user = await s.get(User, UID)
            return await baixa_checagem_manual(s, user, d, 2, [], None)

    # Sem registro de checagem completa → sem baixa.
    monkeypatch.delitem(dou_monitor._ultima_ok, d, raising=False)
    assert asyncio.run(_main(False)) is False

    # Com checagem completa recente → baixa.
    engine, sm = _sm()

    async def _main2():
        await _setup(sm, engine, marca=None, pendencias=[
            ("mp_pendente", d.isoformat()),
        ])
        async with sm() as s:
            user = await s.get(User, UID)
            ok = await baixa_checagem_manual(s, user, d, 2, [], None)
        return ok, await _kinds_restantes(sm)

    monkeypatch.setitem(dou_monitor._ultima_ok, d, (datetime.now(BRT), 2))
    ok, restantes = asyncio.run(_main2())
    assert ok is True
    assert restantes == set()


def test_falha_de_nota_ou_dia_aberto_mantem_pendencia(monkeypatch) -> None:
    d_aberto = datetime.now(BRT).date()  # hoje nunca está encerrado
    d = _dia_fechado()
    engine, sm = _sm()

    async def _main():
        await _setup(sm, engine, marca=None, pendencias=[
            ("mp_pendente", d.isoformat()),
            ("mp_pendente", d_aberto.isoformat()),
        ])
        async with sm() as s:
            user = await s.get(User, UID)
            com_falha = await baixa_checagem_manual(
                s, user, d, 2, ["MP 1382/2026"], None)
            dia_aberto = await baixa_checagem_manual(
                s, user, d_aberto, 2, [], None)
        return com_falha, dia_aberto, await _kinds_restantes(sm)

    monkeypatch.setitem(dou_monitor._ultima_ok, d, (datetime.now(BRT), 2))
    monkeypatch.setitem(dou_monitor._ultima_ok, d_aberto, (datetime.now(BRT), 2))
    com_falha, dia_aberto, restantes = asyncio.run(_main())
    assert com_falha is False, "nota que falhou não pode dar baixa"
    assert dia_aberto is False, "dia aberto ainda pode receber edição extra"
    assert len(restantes) == 2


def test_marca_dagua_nao_pula_dia_nao_checado() -> None:
    """Avanço só no passo contíguo: marca em d-3 e baixa manual de d NÃO pode
    levar a marca até d — os dias no meio nunca foram checados e sumiriam da
    _cobrir_lacuna (perda silenciosa)."""
    d = _dia_fechado()
    marca_antiga = d - timedelta(days=3)
    engine, sm = _sm()

    async def _main():
        await _setup(sm, engine, marca=marca_antiga, pendencias=[
            ("mp_pendente", d.isoformat()),
        ])
        async with sm() as s:
            user = await s.get(User, UID)
            ok = await baixa_checagem_manual(s, user, d, 0, [], "sem_edicao")
            return ok, user.dou_ultimo_dia_ok

    ok, marca = asyncio.run(_main())
    assert ok is True, "a pendência do dia checado sai mesmo assim"
    assert marca == marca_antiga, "marca pulou por cima de dias não checados"


def test_help_menciona_a_baixa_e_fila_roteia_pra_secao() -> None:
    """Regra do projeto: feature nova documentada no help, com matching
    verificado por frase real."""
    from bot.handlers.start import HELP_TEXT, find_help_sections
    assert "dá baixa na fila de re-checagem" in HELP_TEXT
    secoes = find_help_sections("por que o dia continua na fila do mp?")
    assert any("mp_dou_agora" in s for s in secoes)
