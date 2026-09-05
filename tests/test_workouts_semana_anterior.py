"""Academia: guarda a semana anterior e compara ATÉ O MESMO DIA.

Pedido do dono (04/09/2026): "todo domingo meus registros zeram; vamos gravar
a semana anterior pra eu consultar e o bot medir se estou melhor ou pior".

A armadilha que estes testes existem pra travar: comparar semana PELA METADE
com semana CHEIA. Quinta-feira com 3 treinos contra os 5 da semana passada
inteira diria "pior" com 3 dias ainda por vir — número que engana é pior que
número nenhum. A comparação é sempre quinta contra quinta.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.db.models import Base, User, WorkoutLog
from bot.services import workouts
from bot.services.workouts import (
    SEMANAS_RETIDAS,
    format_summary,
    purge_old_weeks,
    summary_current_week,
    week_start,
)

TZ = "America/Sao_Paulo"
UID = 4242


def _sm():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _setup(engine, sm, treinos: list[tuple[date, str, int | None]]):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with sm() as s:
        s.add(User(id=UID, chat_id=UID, is_authorized=True, timezone=TZ))
        for d, grupos, cardio in treinos:
            s.add(WorkoutLog(user_id=UID, date=d, groups=grupos,
                             cardio_minutes=cardio))
        await s.commit()


def _quinta_desta_semana() -> date:
    """Quinta-feira da semana corrente — a semana fica PELA METADE nela."""
    return week_start(datetime.now(ZoneInfo(TZ))) + timedelta(days=4)


def _agora_na_quinta() -> datetime:
    """'Agora' FIXADO na quinta ao meio-dia. Sem isso o teste do recorte
    parcial dependeria do dia em que a suíte roda: num sábado a semana já
    está cheia e o caso parcial nem seria exercitado."""
    return datetime.combine(_quinta_desta_semana(),
                            datetime.min.time().replace(hour=12),
                            tzinfo=ZoneInfo(TZ))


# ─────────────────────────── purge: 2 semanas ───────────────────────────

def test_purge_mantem_a_semana_anterior_e_corta_a_retrasada() -> None:
    """O ponto do pedido: a semana passada PRECISA sobreviver ao domingo."""
    inicio = week_start(datetime.now(ZoneInfo(TZ)))
    anterior = inicio - timedelta(days=7)
    retrasada = inicio - timedelta(days=14)
    engine, sm = _sm()

    async def _main():
        await _setup(engine, sm, [
            (inicio, "peito", None),
            (anterior, "costas", None),
            (retrasada, "pernas", None),
        ])
        async with sm() as s:
            n = await purge_old_weeks(s, TZ)
        async with sm() as s:
            restantes = sorted(
                r.date for r in (await s.scalars(select(WorkoutLog))).all()
            )
        return n, restantes

    n, restantes = asyncio.run(_main())
    assert SEMANAS_RETIDAS == 2
    assert n == 1, "só a retrasada devia sair"
    assert restantes == [anterior, inicio], "a semana anterior foi apagada"


# ──────────────── comparação like-for-like (o coração) ────────────────

def test_semana_parcial_compara_ate_o_mesmo_dia() -> None:
    """Hoje é quinta com 3 treinos; a semana passada teve 2 até quinta e 5 no
    total. O veredito tem que ser ↑ +1 (contra os 2), NUNCA ↓ contra os 5."""
    quinta = _quinta_desta_semana()
    inicio = week_start(datetime.now(ZoneInfo(TZ)))
    ant = inicio - timedelta(days=7)
    engine, sm = _sm()

    async def _main():
        await _setup(engine, sm, [
            # Semana corrente: dom, ter, qui = 3 treinos até quinta.
            (inicio, "peito", None),
            (inicio + timedelta(days=2), "costas", None),
            (quinta, "pernas", None),
            # Semana anterior: dom e ter (2 até quinta) + sex e sab (5 no total).
            (ant, "peito", None),
            (ant + timedelta(days=2), "costas", None),
            (ant + timedelta(days=5), "pernas", None),
            (ant + timedelta(days=6), "cardio", 20),
        ])
        async with sm() as s:
            return await summary_current_week(s, UID, TZ, agora=_agora_na_quinta())

    resumo = asyncio.run(_main())
    comp = resumo["comparacao"]
    assert comp["parcial"] is True
    assert comp["dias"] == 5, "domingo→quinta são 5 dias"
    assert comp["atual"]["dias_treinou"] == 3
    assert comp["anterior"]["dias_treinou"] == 2, (
        "comparou contra a semana anterior INTEIRA (4) em vez de até quinta"
    )
    texto = format_summary(resumo)
    assert "↑ +1" in texto
    assert "semana passada até qui: 2" in texto


def test_semana_encerrada_compara_cheia_contra_cheia() -> None:
    """Consultando a semana PASSADA (já fechada), o recorte é a semana toda."""
    inicio = week_start(datetime.now(ZoneInfo(TZ)))
    ant = inicio - timedelta(days=7)
    retrasada = inicio - timedelta(days=14)
    engine, sm = _sm()

    async def _main():
        await _setup(engine, sm, [
            (ant, "peito", None),
            (ant + timedelta(days=3), "costas", None),
            (retrasada, "pernas", None),
        ])
        async with sm() as s:
            return await summary_current_week(s, UID, TZ, semanas_atras=1)

    resumo = asyncio.run(_main())
    assert resumo["passada"] is True
    assert resumo["hoje"] is None, "semana passada não tem 'hoje' dentro dela"
    comp = resumo["comparacao"]
    assert comp["parcial"] is False and comp["dias"] == 7
    assert comp["atual"]["dias_treinou"] == 2
    assert comp["anterior"]["dias_treinou"] == 1
    texto = format_summary(resumo)
    assert texto.startswith("🏋️ Semana passada")
    assert "📅 Hoje:" not in texto
    assert "Na semana: 2 treinos (semana passada: 1) ↑ +1" in texto


def test_sem_registro_anterior_nao_inventa_comparacao() -> None:
    """Semana anterior VAZIA é ambígua (não treinou × não temos o dado, por
    purge/primeira semana). Afirmar '0 treinos ↑ +3' seria inventar."""
    inicio = week_start(datetime.now(ZoneInfo(TZ)))
    engine, sm = _sm()

    async def _main():
        await _setup(engine, sm, [(inicio, "peito", None)])
        async with sm() as s:
            return await summary_current_week(s, UID, TZ, agora=_agora_na_quinta())

    resumo = asyncio.run(_main())
    assert resumo["comparacao"]["tem_anterior"] is False
    texto = format_summary(resumo)
    assert "Sem registro da semana anterior" in texto
    assert "↑" not in texto and "↓" not in texto


def test_cardio_comparado_no_mesmo_recorte() -> None:
    quinta = _quinta_desta_semana()
    inicio = week_start(datetime.now(ZoneInfo(TZ)))
    ant = inicio - timedelta(days=7)
    engine, sm = _sm()

    async def _main():
        await _setup(engine, sm, [
            (quinta, "cardio", 30),
            (ant, "cardio", 20),                       # dentro do recorte
            (ant + timedelta(days=6), "cardio", 999),  # sábado: FORA do recorte
        ])
        async with sm() as s:
            return await summary_current_week(s, UID, TZ, agora=_agora_na_quinta())

    texto = format_summary(asyncio.run(_main()))
    assert "🔥 Cardio: 30min (semana passada até qui: 20min)" in texto
    assert "999" not in texto, "somou cardio de dia fora do recorte"


# ─────────────────────────── tool + help ───────────────────────────

def test_tool_aceita_semana_passada(monkeypatch) -> None:
    """'como foi minha semana passada?' tem que chegar em semanas_atras=1."""
    from types import SimpleNamespace

    from bot.services import tools

    capturado = {}

    async def _fake(_s, _uid, _tz, *, semanas_atras=0):
        capturado["semanas_atras"] = semanas_atras
        return {"inicio": date(2026, 8, 30), "fim": date(2026, 9, 5),
                "hoje": None, "passada": True, "dias_passados": 7,
                "dias_restantes": 0, "dias_treinou": 0, "dias_descansou": 7,
                "por_grupo": {}, "cardio_min_total": 0, "por_dia": [],
                "comparacao": None}

    monkeypatch.setattr(tools, "summary_current_week", _fake)
    ctx = SimpleNamespace(session=None, user=SimpleNamespace(id=UID), tz=TZ,
                          direct_html=None, short_circuit=False)
    asyncio.run(tools._h_consultar_treinos({"semana": "passada"}, ctx))
    assert capturado["semanas_atras"] == 1
    assert ctx.short_circuit is True

    asyncio.run(tools._h_consultar_treinos({}, ctx))
    assert capturado["semanas_atras"] == 0, "default tem que ser a semana atual"


@pytest.mark.parametrize("frase", [
    "treinei mais que semana passada?",
    "como foi minha semana passada de academia?",
    "quantos treinos eu fiz?",
])
def test_help_roteia_as_frases_novas(frase: str) -> None:
    """Regra do projeto: feature nova no help, com matching por frase real."""
    from bot.handlers.start import HELP_TEXT, find_help_sections

    assert "semana passada até qui" in HELP_TEXT
    secoes = find_help_sections(frase)
    assert any("Academia" in s for s in secoes), f"{frase!r} não achou a seção"
