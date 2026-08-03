"""Baseline do alerta de trânsito: hábito das semanas anteriores, não o hoje.

Bug da auditoria de 03/08/2026 — o alerta era DECORATIVO:
- janela de 7 dias + filtro por weekday ⇒ só as amostras de HOJE entravam;
- record_sample rodava ANTES de baseline_p50 ⇒ a amostra atual (e as
  anteriores do mesmo engarrafamento) definiam o próprio "normal": 30, 32,
  48, 52, 55 min → mediana 48 → 55 nunca passa de +30%. Nunca alertava,
  em silêncio, enquanto o /transito_alerta_on prometia "≥30% acima do
  habitual".

Agora: janela de 28 dias (4 ocorrências do mesmo dia-da-semana, retenção é
30) e `antes_de` exclui o dia corrente da mediana.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.db.models import Base, TrafficSample, User
from bot.services.traffic_baseline import MIN_SAMPLES, baseline_p50, should_alert

UID = 1
WD, HORA = 0, 7   # segunda, 7h


def _sm():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _amostra(quando: datetime, minutos: int) -> TrafficSample:
    return TrafficSample(user_id=UID, weekday=WD, hour=HORA,
                         sampled_at=quando, duration_seconds=minutos * 60)


async def _seed(engine, sm, amostras):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with sm() as s:
        s.add(User(id=UID, chat_id=UID, is_authorized=True))
        for a in amostras:
            s.add(a)
        await s.commit()


def test_dia_corrente_fora_da_mediana_engarrafamento_alerta() -> None:
    """O cenário do relatório: hábito ~31 min (semana passada), hoje o
    congestionamento sobe 48→55. Com o corte, baseline=hábito e 55 alerta;
    sem o corte (comportamento antigo), a mediana incluía o próprio evento
    e nunca alertava."""
    agora = datetime.now(timezone.utc)
    hoje_0h = datetime.combine(agora.date(), time(0, 0), tzinfo=timezone.utc)
    semana_passada = agora - timedelta(days=7)

    habito = [_amostra(semana_passada + timedelta(minutes=10 * i), m)
              for i, m in enumerate([30, 30, 31, 32, 33])]
    evento_hoje = [_amostra(agora - timedelta(minutes=20), 48),
                   _amostra(agora - timedelta(minutes=10), 52),
                   _amostra(agora, 55)]

    engine, sm = _sm()

    async def _main():
        await _seed(engine, sm, habito + evento_hoje)
        async with sm() as s:
            com_corte = await baseline_p50(s, UID, WD, HORA, antes_de=hoje_0h)
            sem_corte = await baseline_p50(s, UID, WD, HORA)
            return com_corte, sem_corte

    com_corte, sem_corte = asyncio.run(_main())
    assert com_corte == 31 * 60, "baseline não é a mediana do hábito"
    assert should_alert(55 * 60, com_corte) is True, "engarrafamento real não alertou"
    # Sem o corte, o próprio evento contamina a mediana e a puxa pra cima —
    # a essência do bug (na janela antiga de 7 dias, SÓ o evento sobrava).
    assert sem_corte is not None and sem_corte > com_corte


def test_janela_de_28_dias_enxerga_as_semanas_anteriores() -> None:
    """Com a janela antiga (7 dias) + corte do dia corrente, NUNCA haveria
    baseline (o mesmo weekday só existe hoje e há exatos 7 dias). As
    amostras de 2 e 3 semanas atrás têm que contar."""
    agora = datetime.now(timezone.utc)
    hoje_0h = datetime.combine(agora.date(), time(0, 0), tzinfo=timezone.utc)
    amostras = []
    for semanas in (2, 3):
        base = agora - timedelta(days=7 * semanas)
        amostras += [_amostra(base + timedelta(minutes=10 * i), 25 + i)
                     for i in range(3)]
    assert len(amostras) >= MIN_SAMPLES

    engine, sm = _sm()

    async def _main():
        await _seed(engine, sm, amostras)
        async with sm() as s:
            return await baseline_p50(s, UID, WD, HORA, antes_de=hoje_0h)

    assert asyncio.run(_main()) is not None, (
        "amostras de 2-3 semanas atrás fora da janela — alerta volta a ser decorativo"
    )


def test_sem_historico_suficiente_nao_ha_baseline() -> None:
    """Menos de MIN_SAMPLES no histórico (excluído o hoje) → None: melhor
    não alertar que alertar contra base inventada."""
    agora = datetime.now(timezone.utc)
    hoje_0h = datetime.combine(agora.date(), time(0, 0), tzinfo=timezone.utc)
    amostras = [_amostra(agora - timedelta(days=7, minutes=10 * i), 30)
                for i in range(2)]
    amostras += [_amostra(agora - timedelta(minutes=10 * i), 55)
                 for i in range(6)]   # hoje não conta

    engine, sm = _sm()

    async def _main():
        await _seed(engine, sm, amostras)
        async with sm() as s:
            return await baseline_p50(s, UID, WD, HORA, antes_de=hoje_0h)

    assert asyncio.run(_main()) is None
