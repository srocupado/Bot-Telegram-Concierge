"""Lembrete recorrente não pode disparar em rajada depois de o bot ficar fora.

O scheduler reagenda com `sent=False` (scheduler.py:332). Se `next_due_from`
devolve instante no PASSADO, o lembrete vence de novo no tick seguinte (60s):
bot fora 5 dias = 5 mensagens em 5 minutos.

O ramo `cron:` já tinha a guarda (`base = max(after, agora)`), com comentário
explícito sobre rajada; os recorrentes fixos ficaram de fora dela.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from bot.services.reminders import _passo_recorrencia, next_due_from

TZ = "America/Sao_Paulo"
BRT = ZoneInfo(TZ)
RRULES = ["daily", "weekday", "weekend", "monthly", "weekly:seg,qua,sex"]


@pytest.mark.parametrize("rrule", RRULES)
@pytest.mark.parametrize("atraso", [1, 5, 40, 400])
def test_nunca_reagenda_pro_passado(rrule, atraso) -> None:
    agora = datetime.now(timezone.utc)
    prox = next_due_from(rrule, agora - timedelta(days=atraso), TZ)
    assert prox > agora, (
        f"{rrule} atrasado {atraso}d reagendou pro passado ({prox}) — "
        "o tick seguinte dispara de novo"
    )


@pytest.mark.parametrize("rrule", RRULES)
def test_em_dia_mantem_o_comportamento_antigo(rrule) -> None:
    """A guarda só age em atraso: quem está em dia segue com o mesmo passo."""
    agora = datetime.now(timezone.utc)
    esperado = _passo_recorrencia(rrule, agora.astimezone(BRT))
    assert next_due_from(rrule, agora, TZ) == esperado.astimezone(timezone.utc)


def test_horario_local_e_preservado_no_catch_up() -> None:
    """O valor do recorrente é o HH:MM. Avançar vários dias não pode
    escorregar o horário — senão o lembrete das 7h vira das 7h05."""
    base = datetime(2026, 6, 1, 7, 30, tzinfo=BRT).astimezone(timezone.utc)
    prox = next_due_from("daily", base, TZ).astimezone(BRT)
    assert (prox.hour, prox.minute) == (7, 30)


def test_weekday_cai_em_dia_util() -> None:
    prox = next_due_from("weekday", datetime.now(timezone.utc) - timedelta(days=90), TZ)
    assert prox.astimezone(BRT).weekday() <= 4


def test_weekend_cai_em_fim_de_semana() -> None:
    prox = next_due_from("weekend", datetime.now(timezone.utc) - timedelta(days=90), TZ)
    assert prox.astimezone(BRT).weekday() >= 5


def test_weekly_cai_num_dos_dias_pedidos() -> None:
    prox = next_due_from("weekly:ter,qui", datetime.now(timezone.utc) - timedelta(days=90), TZ)
    assert prox.astimezone(BRT).weekday() in (1, 3)


def test_monthly_preserva_o_dia_do_mes() -> None:
    base = datetime(2026, 1, 15, 9, 0, tzinfo=BRT).astimezone(timezone.utc)
    prox = next_due_from("monthly", base, TZ).astimezone(BRT)
    assert prox.day == 15 and (prox.hour, prox.minute) == (9, 0)


def test_monthly_dia_31_nao_estoura_em_mes_curto() -> None:
    """31/01 → fevereiro não tem 31: cai no último dia, sem ValueError."""
    passo = _passo_recorrencia("monthly", datetime(2026, 1, 31, 9, 0, tzinfo=BRT))
    assert (passo.month, passo.day) == (2, 28)


def test_rrule_desconhecido_nao_trava_o_laco() -> None:
    """Fallback de 1 dia + teto de saltos: rrule estranho não pode pendurar o
    tick do scheduler num laço infinito."""
    prox = next_due_from("formato:inexistente", datetime.now(timezone.utc) - timedelta(days=30), TZ)
    assert prox > datetime.now(timezone.utc)
