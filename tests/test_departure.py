"""Testes da lógica pura do 'melhor horário pra sair' (sem rede/API)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot.services.departure import (
    DepartureOption,
    candidate_times,
    choose_best,
    format_departure_message,
    parse_arrive_by,
)

TZ = ZoneInfo("America/Sao_Paulo")


def _now():
    return datetime(2026, 7, 6, 8, 0, tzinfo=TZ)  # segunda, 08:00


def _opt(minute_offset: int, travel: int) -> DepartureOption:
    return DepartureOption(
        depart_at=_now() + timedelta(minutes=minute_offset), travel_minutes=travel,
    )


# ---------- candidate_times ----------

def test_candidate_times_leave_soon():
    ts = candidate_times(_now(), 60, 15)
    assert [t.minute for t in ts] == [0, 15, 30, 45, 0]  # 08:00..09:00
    assert len(ts) == 5
    assert ts[-1] == _now() + timedelta(minutes=60)


def test_candidate_times_respects_max_probes():
    ts = candidate_times(_now(), 300, 15, max_probes=8)
    assert len(ts) == 8  # não estoura o orçamento de chamadas


def test_candidate_times_arrive_by_stretches_step():
    end = _now() + timedelta(minutes=120)
    ts = candidate_times(_now(), 60, 15, max_probes=8, end=end)
    assert len(ts) <= 8
    assert ts[0] == _now()
    # passo esticado pra caber na janela sem passar de max_probes
    assert all(ts[i] < ts[i + 1] for i in range(len(ts) - 1))
    assert ts[-1] <= end


def test_candidate_times_arrive_by_in_past_is_empty():
    end = _now() - timedelta(minutes=5)
    assert candidate_times(_now(), 60, 15, end=end) == []


# ---------- choose_best (modo 'sair em breve') ----------

def test_choose_best_min_travel():
    opts = [_opt(0, 30), _opt(15, 25), _opt(30, 40)]
    best, feasible = choose_best(opts)
    assert feasible is True
    assert best.travel_minutes == 25
    assert best.depart_at == _now() + timedelta(minutes=15)


def test_choose_best_tie_breaks_to_earliest_departure():
    opts = [_opt(30, 25), _opt(0, 25)]
    best, _ = choose_best(opts)
    assert best.depart_at == _now()  # empate no tempo → sai mais cedo


def test_choose_best_empty():
    assert choose_best([]) == (None, False)


# ---------- choose_best (modo 'chegar até') ----------

def test_choose_best_arrive_by_picks_latest_on_time():
    arrive_by = _now() + timedelta(minutes=90)  # 09:30
    # sair 08:00→chega 08:40; 08:30→09:20; 09:00→10:00 (atrasa)
    opts = [_opt(0, 40), _opt(30, 50), _opt(60, 60)]
    best, feasible = choose_best(opts, arrive_by=arrive_by)
    assert feasible is True
    # a saída MAIS TARDE que ainda chega até 09:30 é a das 08:30 (chega 09:20)
    assert best.depart_at == _now() + timedelta(minutes=30)
    assert best.arrive_at <= arrive_by


def test_choose_best_arrive_by_infeasible_returns_earliest_arrival():
    arrive_by = _now() + timedelta(minutes=20)  # 08:20, impossível
    opts = [_opt(0, 40), _opt(15, 35), _opt(30, 50)]
    best, feasible = choose_best(opts, arrive_by=arrive_by)
    assert feasible is False
    # nenhuma chega a tempo → a de chegada mais cedo: 08:00+40=08:40 vence
    # 08:15+35=08:50 e 08:30+50=09:20
    assert best.depart_at == _now()


# ---------- parse_arrive_by ----------

def test_parse_arrive_by_hh_only():
    assert parse_arrive_by("9h", _now()) == _now().replace(hour=9, minute=0)


def test_parse_arrive_by_hhmm_variants():
    for raw in ("9:30", "09h30", "09:30"):
        assert parse_arrive_by(raw, _now()) == _now().replace(hour=9, minute=30)


def test_parse_arrive_by_iso_local():
    got = parse_arrive_by("2026-07-06T09:15", _now())
    assert got == _now().replace(hour=9, minute=15)
    assert got.tzinfo is not None  # herdou o fuso do `now`


def test_parse_arrive_by_past_rolls_to_tomorrow():
    got = parse_arrive_by("7h", _now())  # 07:00 < 08:00 agora
    assert got == _now().replace(hour=7, minute=0) + timedelta(days=1)


def test_parse_arrive_by_invalid():
    assert parse_arrive_by("qualquer coisa", _now()) is None
    assert parse_arrive_by("25h", _now()) is None
    assert parse_arrive_by("", _now()) is None


# ---------- format (smoke) ----------

def test_format_leave_soon_marks_best_with_star():
    opts = [_opt(0, 30), _opt(15, 25)]
    best, _ = choose_best(opts)
    msg = format_departure_message("casa", "trabalho", best, opts, arrive_by=None, feasible=True)
    assert "Melhor sair" in msg
    assert "⭐" in msg
    assert "casa → trabalho" in msg


def test_format_arrive_by_infeasible_warns():
    arrive_by = _now() + timedelta(minutes=20)
    opts = [_opt(0, 40), _opt(15, 35)]
    best, feasible = choose_best(opts, arrive_by=arrive_by)
    msg = format_departure_message(
        "casa", "aeroporto", best, opts, arrive_by=arrive_by, feasible=feasible,
    )
    assert "⚠️" in msg
