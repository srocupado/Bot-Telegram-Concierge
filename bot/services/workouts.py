"""Tracker de academia. Registro semanal com purge automática no domingo.

Convenção de semana: domingo 00:00 → sábado 23:59 (mesma do calendário
brasileiro padrão). Categorias canônicas: peito, costas, pernas, cardio.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import WorkoutLog

logger = logging.getLogger(__name__)

CANONICAL_GROUPS = {"peito", "costas", "pernas", "cardio"}
_DIAS_PT = ["seg", "ter", "qua", "qui", "sex", "sab", "dom"]


def week_start(now_local: datetime) -> date:
    """Retorna o domingo da semana corrente (semana começa no domingo)."""
    today = now_local.date()
    # weekday(): seg=0, dom=6. Pra ir até o domingo passado:
    #   se hoje é domingo → 0 dias a recuar
    #   se hoje é segunda → 1 dia
    #   ...
    days_since_sunday = (today.weekday() + 1) % 7
    return today - timedelta(days=days_since_sunday)


def _format_dia_label(d: date) -> str:
    return f"{_DIAS_PT[d.weekday()]} {d.strftime('%d/%m')}"


def normalize_groups(raw_groups: list[str]) -> list[str]:
    """Retorna lista única e ordenada com só as canônicas válidas."""
    seen: list[str] = []
    for g in raw_groups:
        g_norm = (g or "").strip().lower()
        if g_norm in CANONICAL_GROUPS and g_norm not in seen:
            seen.append(g_norm)
    # Ordem fixa pra ficar consistente.
    order = ["peito", "costas", "pernas", "cardio"]
    return [g for g in order if g in seen]


async def log_workout(
    session: AsyncSession,
    user_id: int,
    workout_date: date,
    groups: list[str],
    cardio_minutes: int | None = None,
    notes: str | None = None,
) -> WorkoutLog:
    normalized = normalize_groups(groups)
    if not normalized:
        raise ValueError("nenhum grupo canônico fornecido")
    if "cardio" not in normalized:
        cardio_minutes = None  # ignora minutos se não tem cardio
    groups_str = ",".join(normalized)

    # Idempotência: se já existe um treino IDÊNTICO no mesmo dia (mesmos
    # grupos + mesmo cardio), não cria duplicata. Protege contra o LLM
    # chamar registrar_treino duas vezes pela mesma fala (bug "cardio em
    # dobro"). Treinos genuinamente distintos no dia (grupos ou cardio
    # diferentes) ainda geram registros separados.
    dup_stmt = select(WorkoutLog).where(
        WorkoutLog.user_id == user_id,
        WorkoutLog.date == workout_date,
        WorkoutLog.groups == groups_str,
        WorkoutLog.cardio_minutes.is_(None)
        if cardio_minutes is None
        else WorkoutLog.cardio_minutes == cardio_minutes,
    )
    dup = (await session.scalars(dup_stmt)).first()
    if dup is not None:
        logger.info("log_workout: duplicata ignorada (user=%s date=%s groups=%s)",
                    user_id, workout_date, groups_str)
        return dup

    log = WorkoutLog(
        user_id=user_id,
        date=workout_date,
        groups=groups_str,
        cardio_minutes=cardio_minutes,
        notes=notes,
    )
    session.add(log)
    await session.commit()
    await session.refresh(log)
    return log


async def summary_current_week(
    session: AsyncSession, user_id: int, tz_name: str,
) -> dict:
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    start = week_start(now_local)
    end = start + timedelta(days=6)

    stmt = select(WorkoutLog).where(
        WorkoutLog.user_id == user_id,
        WorkoutLog.date >= start,
        WorkoutLog.date <= end,
    ).order_by(WorkoutLog.date, WorkoutLog.id)
    rows = list((await session.scalars(stmt)).all())

    # Agrupa por dia. Pode haver várias entradas no mesmo dia.
    by_day: dict[date, dict] = {}
    for r in rows:
        d = by_day.setdefault(r.date, {"groups": [], "cardio_min": 0})
        for g in r.groups.split(","):
            g = g.strip()
            if g and g not in d["groups"]:
                d["groups"].append(g)
        if r.cardio_minutes:
            d["cardio_min"] += r.cardio_minutes

    por_dia: list[tuple[date, list[str], int | None]] = []
    por_grupo: dict[str, int] = {"peito": 0, "costas": 0, "pernas": 0, "cardio": 0}
    cardio_min_total = 0
    dias_treinou = 0

    for offset in range(7):
        d = start + timedelta(days=offset)
        info = by_day.get(d)
        if info is None or not info["groups"]:
            por_dia.append((d, [], None))
            continue
        groups_norm = [g for g in ["peito", "costas", "pernas", "cardio"] if g in info["groups"]]
        cardio_min = info["cardio_min"] or None
        por_dia.append((d, groups_norm, cardio_min))
        dias_treinou += 1
        for g in groups_norm:
            por_grupo[g] += 1
        if cardio_min:
            cardio_min_total += cardio_min

    hoje = now_local.date()
    dias_passados = (hoje - start).days + 1  # inclui hoje
    dias_restantes = max(0, 6 - (hoje - start).days)  # depois de hoje
    return {
        "inicio": start,
        "fim": end,
        "hoje": hoje,
        "dias_passados": dias_passados,
        "dias_restantes": dias_restantes,
        "dias_treinou": dias_treinou,
        "dias_descansou": 7 - dias_treinou,
        "por_grupo": por_grupo,
        "cardio_min_total": cardio_min_total,
        "por_dia": por_dia,
    }


def format_summary(summary: dict) -> str:
    inicio = summary["inicio"]
    fim = summary["fim"]
    hoje = summary.get("hoje")
    lines = [
        f"semana {inicio.strftime('%d/%m')} (dom) → {fim.strftime('%d/%m')} (sab) "
        f"— {summary['dias_treinou']} treinos, {summary['dias_descansou']} dias sem treino"
    ]
    if hoje is not None:
        lines.append(
            f"hoje: {_format_dia_label(hoje)} "
            f"(dia {summary['dias_passados']}/7 da semana — "
            f"{summary['dias_restantes']} dia(s) ainda por vir)"
        )
    for d, groups, cardio in summary["por_dia"]:
        if hoje is not None and d > hoje:
            marker = "futuro"
        elif hoje is not None and d == hoje:
            marker = "hoje"
        else:
            marker = "passado"
        if not groups:
            label = "sem treino" if marker != "futuro" else "—"
            lines.append(f"  • {_format_dia_label(d)} [{marker}]: {label}")
            continue
        label = " + ".join(groups)
        if cardio:
            label += f" ({cardio}min cardio)"
        lines.append(f"  • {_format_dia_label(d)} [{marker}]: {label}")
    pg = summary["por_grupo"]
    pg_items = [f"{k}:{v}" for k, v in pg.items() if v > 0]
    extras = []
    if summary["cardio_min_total"]:
        extras.append(f"total cardio: {summary['cardio_min_total']}min")
    if pg_items:
        extras.append(" | ".join(pg_items))
    if extras:
        lines.append(" | ".join(extras))
    return "\n".join(lines)


async def purge_old_weeks(session: AsyncSession, tz_name: str) -> int:
    """Deleta entradas com date < domingo da semana corrente."""
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    start = week_start(now_local)
    result = await session.execute(
        delete(WorkoutLog).where(WorkoutLog.date < start)
    )
    await session.commit()
    return result.rowcount or 0


async def delete_workouts_on_date(
    session: AsyncSession, user_id: int, workout_date: date,
) -> int:
    """Apaga TODAS as entradas do usuário em um dia específico. Retorna a
    quantidade removida (0 se não havia)."""
    result = await session.execute(
        delete(WorkoutLog).where(
            WorkoutLog.user_id == user_id,
            WorkoutLog.date == workout_date,
        )
    )
    await session.commit()
    return result.rowcount or 0
