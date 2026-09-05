"""Tracker de academia. Guarda a semana corrente e a ANTERIOR — purge no
domingo apaga o que for mais velho que isso.

Convenção de semana: domingo 00:00 → sábado 23:59 (mesma do calendário
brasileiro padrão). Categorias canônicas: peito, costas, pernas, cardio.

A semana anterior fica no banco (escolha do dono, 04/09/2026) pra responder
"estou melhor ou pior que semana passada?". A comparação é sempre ATÉ O MESMO
DIA da semana: quinta contra quinta. Comparar uma semana pela metade com uma
semana cheia diria "pior" com 3 treinos contra 5 quando ainda faltam 3 dias —
número que engana é pior que número nenhum.
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
_ORDEM_GRUPOS = ["peito", "costas", "pernas", "cardio"]

# Semanas mantidas no banco: a corrente + a anterior. O custo de guardar tudo
# seria de dezenas de KB/ano — o recorte é escolha de desenho do dono, não
# limitação técnica.
SEMANAS_RETIDAS = 2


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


def _mapa_por_dia(rows) -> dict[date, dict]:
    """Agrupa as linhas por dia (várias sessões no mesmo dia se fundem)."""
    by_day: dict[date, dict] = {}
    for r in rows:
        d = by_day.setdefault(r.date, {"groups": [], "cardio_min": 0})
        for g in r.groups.split(","):
            g = g.strip()
            if g and g not in d["groups"]:
                d["groups"].append(g)
        if r.cardio_minutes:
            d["cardio_min"] += r.cardio_minutes
    return by_day


def _totais(by_day: dict[date, dict], start: date, dias: int) -> dict:
    """Totais dos `dias` PRIMEIROS dias da semana que começa em `start`.

    O recorte por `dias` é o que torna a comparação honesta: a semana corrente
    entra só até hoje, e a anterior entra até o MESMO dia."""
    por_grupo = {g: 0 for g in _ORDEM_GRUPOS}
    dias_treinou = 0
    cardio_total = 0
    for offset in range(dias):
        info = by_day.get(start + timedelta(days=offset))
        if not info or not info["groups"]:
            continue
        dias_treinou += 1
        for g in _ORDEM_GRUPOS:
            if g in info["groups"]:
                por_grupo[g] += 1
        cardio_total += info["cardio_min"] or 0
    return {"dias_treinou": dias_treinou, "por_grupo": por_grupo,
            "cardio_min_total": cardio_total}


async def summary_current_week(
    session: AsyncSession, user_id: int, tz_name: str, *, semanas_atras: int = 0,
    agora: datetime | None = None,
) -> dict:
    """Resumo de uma semana (0 = corrente, 1 = passada) com comparação contra
    a semana imediatamente anterior a ela, recortada no mesmo ponto.

    `agora` injetável (mesmo padrão de `_dia_encerrado`): o recorte parcial
    depende do DIA DA SEMANA, então testá-lo com o relógio real só exercitaria
    o caso de hoje — e passaria a falhar sozinho conforme a semana vira."""
    tz = ZoneInfo(tz_name)
    now_local = agora.astimezone(tz) if agora is not None else datetime.now(tz)
    start = week_start(now_local) - timedelta(days=7 * semanas_atras)
    end = start + timedelta(days=6)
    ant_start = start - timedelta(days=7)

    # Uma query só cobrindo as DUAS semanas (a alvo e a de comparação).
    stmt = select(WorkoutLog).where(
        WorkoutLog.user_id == user_id,
        WorkoutLog.date >= ant_start,
        WorkoutLog.date <= end,
    ).order_by(WorkoutLog.date, WorkoutLog.id)
    rows = list((await session.scalars(stmt)).all())
    by_day = _mapa_por_dia(rows)

    por_dia: list[tuple[date, list[str], int | None]] = []
    for offset in range(7):
        d = start + timedelta(days=offset)
        info = by_day.get(d)
        if info is None or not info["groups"]:
            por_dia.append((d, [], None))
            continue
        groups_norm = [g for g in _ORDEM_GRUPOS if g in info["groups"]]
        por_dia.append((d, groups_norm, info["cardio_min"] or None))

    cheia = _totais(by_day, start, 7)
    hoje = now_local.date()
    # Semana já encerrada compara cheia×cheia; a corrente, só até hoje.
    if hoje > end:
        dias_corte = 7
    else:
        dias_corte = max(1, min(7, (hoje - start).days + 1))
    dia_corte = start + timedelta(days=dias_corte - 1)

    # Sem NENHUM registro na semana anterior não dá pra comparar: "0 treinos"
    # ali pode ser "não treinou" OU "não temos o dado" (purge/primeira semana
    # do recurso). Afirmar seria inventar — a linha some e diz por quê.
    tem_anterior = any(ant_start <= r.date < start for r in rows)
    comparacao = {
        "dias": dias_corte,
        "parcial": dias_corte < 7,
        "rotulo": _DIAS_PT[dia_corte.weekday()],
        "tem_anterior": tem_anterior,
        "atual": _totais(by_day, start, dias_corte),
        "anterior": _totais(by_day, ant_start, dias_corte),
        "inicio_anterior": ant_start,
    }

    passada = semanas_atras > 0
    return {
        "inicio": start,
        "fim": end,
        # Semana passada não tem "hoje" dentro dela: sem isso o resumo
        # imprimia "📅 Hoje: <data fora do intervalo>" e marcava tudo errado.
        "hoje": None if passada else hoje,
        "passada": passada,
        "dias_passados": (hoje - start).days + 1,
        "dias_restantes": max(0, 6 - (hoje - start).days),
        "dias_treinou": cheia["dias_treinou"],
        "dias_descansou": 7 - cheia["dias_treinou"],
        "por_grupo": cheia["por_grupo"],
        "cardio_min_total": cheia["cardio_min_total"],
        "por_dia": por_dia,
        "comparacao": comparacao,
    }


_GROUP_EMOJI = {"peito": "💪", "costas": "🔙", "pernas": "🦵", "cardio": "🏃"}


def _groups_label(groups: list[str]) -> str:
    return " + ".join(f"{_GROUP_EMOJI.get(g, '')}{g}".strip() for g in groups)


def format_summary(summary: dict) -> str:
    inicio = summary["inicio"]
    fim = summary["fim"]
    hoje = summary.get("hoje")
    titulo = "Semana passada" if summary.get("passada") else "Semana"
    lines = [
        f"🏋️ {titulo} {inicio.strftime('%d/%m')} (dom) → {fim.strftime('%d/%m')} (sáb) "
        f"— {summary['dias_treinou']} treinos, {summary['dias_descansou']} dias sem treino"
    ]
    if hoje is not None:
        lines.append(
            f"📅 Hoje: {_format_dia_label(hoje)} "
            f"(dia {summary['dias_passados']}/7 da semana — "
            f"{summary['dias_restantes']} dia(s) ainda por vir)"
        )
    lines.append("")  # linha em branco antes da lista de dias
    for d, groups, cardio in summary["por_dia"]:
        if hoje is not None and d > hoje:
            marker = "futuro"
        elif hoje is not None and d == hoje:
            marker = "hoje"
        else:
            marker = "passado"
        if not groups:
            emoji = "▫️" if marker == "futuro" else "⬜"
            label = "sem treino" if marker != "futuro" else "—"
            lines.append(f"{emoji} {_format_dia_label(d)} [{marker}]: {label}")
            continue
        label = _groups_label(groups)
        if cardio:
            label += f" ({cardio}min)"
        lines.append(f"✅ {_format_dia_label(d)} [{marker}]: {label}")
    pg = summary["por_grupo"]
    pg_items = [f"{_GROUP_EMOJI.get(k, '')}{k}:{v}" for k, v in pg.items() if v > 0]
    extras = []
    if summary["cardio_min_total"]:
        extras.append(f"🔥 cardio total: {summary['cardio_min_total']}min")
    if pg_items:
        extras.append(" · ".join(pg_items))
    if extras:
        lines.append(" · ".join(extras))
    lines += _linhas_comparacao(summary.get("comparacao"))
    return "\n".join(lines)


def _linhas_comparacao(comp: dict | None) -> list[str]:
    """Comparação com a semana anterior, sempre no MESMO recorte de dias."""
    if not comp:
        return []
    if not comp["tem_anterior"]:
        return ["📊 Sem registro da semana anterior pra comparar."]
    atual, ant = comp["atual"], comp["anterior"]
    if comp["parcial"]:
        escopo, ref = f"Até {comp['rotulo']}", f"semana passada até {comp['rotulo']}"
    else:
        escopo, ref = "Na semana", "semana passada"
    delta = atual["dias_treinou"] - ant["dias_treinou"]
    if delta > 0:
        marca = f"↑ +{delta}"
    elif delta < 0:
        marca = f"↓ {delta}"
    else:
        marca = "→ igual"
    plural = "treino" if atual["dias_treinou"] == 1 else "treinos"
    out = [f"📊 {escopo}: {atual['dias_treinou']} {plural} "
           f"({ref}: {ant['dias_treinou']}) {marca}"]
    if atual["cardio_min_total"] or ant["cardio_min_total"]:
        out.append(f"🔥 Cardio: {atual['cardio_min_total']}min "
                   f"({ref}: {ant['cardio_min_total']}min)")
    return out


async def purge_old_weeks(session: AsyncSession, tz_name: str) -> int:
    """Deleta o que for mais velho que as SEMANAS_RETIDAS últimas semanas
    (corrente + anterior). A anterior FICA — é ela que sustenta o
    'melhor/pior que semana passada'."""
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    corte = week_start(now_local) - timedelta(days=7 * (SEMANAS_RETIDAS - 1))
    result = await session.execute(
        delete(WorkoutLog).where(WorkoutLog.date < corte)
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
