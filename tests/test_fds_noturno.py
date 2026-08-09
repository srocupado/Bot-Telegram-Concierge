"""Resumo de fim de semana (sexta) + rotina noturna (dono, 09/08/2026).

Sexta, última janela: clima de sáb/dom + lembretes do fds + em cartaz no
Cinemark configurado (FDS_CINEMA). Noite (~21h30): gastos lançados hoje,
lembretes e previsão de amanhã, e o gancho "ficou gasto sem lançar?".
Regra da casa nos dois: fonte que falha é DITA, nunca vira "sem nada".
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bot.services import proactive
from bot.services.weather import DayWeather

BRT = ZoneInfo("America/Sao_Paulo")
# 14/08/2026 é SEXTA (09/08 é domingo): sáb=15, dom=16.
SEXTA = datetime(2026, 8, 14, 19, 5, tzinfo=BRT)


def _user():
    return SimpleNamespace(
        id=7, timezone="America/Sao_Paulo",
        viagem_inicio=None, viagem_fim=None, viagem_tz=None,
        viagem_coords=None, viagem_destino=None,
    )


def _dia(iso: str, *, prob: int = 10) -> DayWeather:
    return DayWeather(date_iso=iso, temp_min_c=16.0, temp_max_c=28.0,
                      precip_prob_pct=prob, precip_mm=0.0,
                      condition_emoji="☀️", condition_label="céu limpo")


def _lembrete(id_: int, due_local: datetime, texto: str = "churrasco"):
    return SimpleNamespace(id=id_, text=texto, due_at=due_local.astimezone(ZoneInfo("UTC")),
                           recurrence=None, command_kind=None)


def _prep_fds(monkeypatch, *, cinema="", forecast=None, forecast_exc=None,
              cinema_exc=None, lembretes=()):
    from bot.services import cinema as cin_mod
    from bot.services import reminders as rem_mod
    from bot.services import weather as w_mod

    monkeypatch.setattr(proactive.settings, "home_coords", "-15.79,-47.88")
    monkeypatch.setattr(proactive.settings, "fds_cinema", cinema)

    async def _forecast(client, coords, *, tz, days):
        if forecast_exc:
            raise forecast_exc
        return forecast or []

    async def _pend(session, user_id):
        return list(lembretes)

    async def _cartaz(nome):
        if cinema_exc:
            raise cinema_exc
        return "Cinemark Iguatemi (Brasília/DF)", ["Filme A", "Filme B"]

    monkeypatch.setattr(w_mod, "fetch_forecast", _forecast)
    monkeypatch.setattr(rem_mod, "list_pending", _pend)
    monkeypatch.setattr(cin_mod, "filmes_em_cartaz", _cartaz)


def test_fds_feliz_clima_agenda_cinema(monkeypatch) -> None:
    sab = datetime(2026, 8, 15, 11, 0, tzinfo=BRT)
    seg = datetime(2026, 8, 17, 9, 0, tzinfo=BRT)
    _prep_fds(
        monkeypatch, cinema="Iguatemi Brasília",
        forecast=[_dia("2026-08-14"), _dia("2026-08-15"), _dia("2026-08-16", prob=70)],
        lembretes=[_lembrete(1, sab), _lembrete(2, seg, "reunião")],
    )
    facts = asyncio.run(proactive.collect_fds(None, _user(), SEXTA))
    kinds = [f.kind for f in facts]
    assert kinds == ["fds_clima", "fds_agenda", "fds_cinema"]
    # clima: só sáb e dom (a sexta do forecast fica de fora)
    assert "15/08" in facts[0].text and "16/08" in facts[0].text
    assert "14/08" not in facts[0].text
    assert "70% chuva" in facts[0].text
    # agenda: lembrete de sábado entra, o de segunda não
    assert "churrasco" in facts[1].text and "reunião" not in facts[1].text
    assert "Filme A" in facts[2].text
    assert all(f.category == "fds" and f.key == "2026-08-15" for f in facts)


def test_fds_sem_cinema_configurado(monkeypatch) -> None:
    _prep_fds(monkeypatch, forecast=[_dia("2026-08-15"), _dia("2026-08-16")])
    facts = asyncio.run(proactive.collect_fds(None, _user(), SEXTA))
    assert [f.kind for f in facts] == ["fds_clima", "fds_agenda"]
    assert "Nenhum lembrete" in facts[1].text


def test_fds_clima_falhou_e_dito(monkeypatch) -> None:
    _prep_fds(monkeypatch, forecast_exc=RuntimeError("api fora"))
    facts = asyncio.run(proactive.collect_fds(None, _user(), SEXTA))
    assert facts[0].kind == "fds_clima_falhou"
    assert "NÃO assuma tempo firme" in facts[0].text


def test_fds_cinema_falhou_nao_vira_sem_filmes(monkeypatch) -> None:
    from bot.services.cinema import CinemaError
    _prep_fds(monkeypatch, cinema="Iguatemi",
              forecast=[_dia("2026-08-15"), _dia("2026-08-16")],
              cinema_exc=CinemaError("sem programação retornada"))
    facts = asyncio.run(proactive.collect_fds(None, _user(), SEXTA))
    falha = [f for f in facts if f.kind == "fds_cinema_falhou"]
    assert falha and "NÃO significa que não há sessões" in falha[0].text
    assert "sem programação retornada" in falha[0].text


def test_fds_em_outro_dia_olha_proximo_sabado(monkeypatch) -> None:
    """force numa quarta (12/08) → mesmo fds 15-16/08 (próximo sábado)."""
    _prep_fds(monkeypatch, forecast=[_dia("2026-08-15"), _dia("2026-08-16")])
    quarta = datetime(2026, 8, 12, 10, 0, tzinfo=BRT)
    facts = asyncio.run(proactive.collect_fds(None, _user(), quarta, force=True))
    assert facts[0].key == "2026-08-15"


# ───────────────────────── rotina noturna ─────────────────────────

def _prep_noturno(monkeypatch, *, gastos=([], 0.0), gastos_exc=None,
                  lembretes=(), forecast=None, forecast_exc=None):
    from bot.services import financeiro as fin_mod
    from bot.services import reminders as rem_mod
    from bot.services import weather as w_mod

    monkeypatch.setattr(proactive.settings, "home_coords", "-15.79,-47.88")

    async def _gastos(session, user, today_iso):
        if gastos_exc:
            raise gastos_exc
        return gastos

    async def _pend(session, user_id):
        return list(lembretes)

    async def _forecast(client, coords, *, tz, days):
        if forecast_exc:
            raise forecast_exc
        return forecast or []

    monkeypatch.setattr(fin_mod, "gastos_do_dia", _gastos)
    monkeypatch.setattr(rem_mod, "list_pending", _pend)
    monkeypatch.setattr(w_mod, "fetch_forecast", _forecast)


NOITE = datetime(2026, 8, 14, 21, 30, tzinfo=BRT)


def test_noturno_com_gastos_e_amanha(monkeypatch) -> None:
    amanha = datetime(2026, 8, 15, 9, 0, tzinfo=BRT)
    depois = datetime(2026, 8, 16, 9, 0, tzinfo=BRT)
    _prep_noturno(
        monkeypatch,
        gastos=(["• mercado · R$ 120,00 (cartão)"], 120.0),
        lembretes=[_lembrete(1, amanha, "dentista"), _lembrete(2, depois, "cinema")],
        forecast=[_dia("2026-08-14"), _dia("2026-08-15")],
    )
    txt = asyncio.run(proactive.montar_resumo_noturno(None, _user(), NOITE))
    assert "Fechando o dia" in txt
    assert "mercado" in txt and "Total gasto: R$ 120,00" in txt
    assert "dentista" in txt and "cinema" not in txt        # só amanhã
    assert "15/08" in txt and "14/08" not in txt            # previsão de amanhã
    assert "Nenhum lançamento" not in txt


def test_noturno_sem_gasto_pergunta(monkeypatch) -> None:
    _prep_noturno(monkeypatch, forecast=[_dia("2026-08-15")])
    txt = asyncio.run(proactive.montar_resumo_noturno(None, _user(), NOITE))
    assert "Nenhum lançamento hoje" in txt and "me manda agora" in txt
    assert "nenhum lembrete marcado" in txt.lower()


def test_noturno_falha_financeiro_e_dita_sem_nudge(monkeypatch) -> None:
    """Firestore fora ≠ 'dia sem gastos': a falha é dita e o nudge de lançar
    NÃO sai (cobrança baseada em dado que não veio seria falso negativo)."""
    _prep_noturno(monkeypatch, gastos_exc=RuntimeError("Firestore fora"),
                  forecast=[_dia("2026-08-15")])
    txt = asyncio.run(proactive.montar_resumo_noturno(None, _user(), NOITE))
    assert "Não consegui checar" in txt and "RuntimeError" in txt
    assert "Nenhum lançamento hoje" not in txt


def test_noturno_financeiro_nao_configurado_omite(monkeypatch) -> None:
    from bot.services.financeiro import NotConfiguredError
    _prep_noturno(monkeypatch, gastos_exc=NotConfiguredError("sem uid"),
                  forecast=[_dia("2026-08-15")])
    txt = asyncio.run(proactive.montar_resumo_noturno(None, _user(), NOITE))
    assert "gasto" not in txt.lower() and "lançamento" not in txt.lower()


def test_noturno_clima_falhou_e_dito(monkeypatch) -> None:
    _prep_noturno(monkeypatch, forecast_exc=RuntimeError("api fora"))
    txt = asyncio.run(proactive.montar_resumo_noturno(None, _user(), NOITE))
    assert "Não consegui a previsão de amanhã" in txt


# ─────────────────────── gate de horário do noturno ───────────────────────

def test_noturno_devido_janela() -> None:
    from bot.services.scheduler import _noturno_devido
    d = datetime(2026, 8, 14, 0, 0, tzinfo=BRT)
    assert not _noturno_devido(d.replace(hour=21, minute=29))
    assert _noturno_devido(d.replace(hour=21, minute=30))
    assert _noturno_devido(d.replace(hour=23, minute=59))
    assert not _noturno_devido(d.replace(hour=9, minute=0))    # manhã não
    assert not _noturno_devido(d.replace(hour=0, minute=5))    # virou o dia


# ───────────────────── gastos_do_dia (financeiro) ─────────────────────

def test_gastos_do_dia_filtra_e_soma(monkeypatch) -> None:
    from bot.services import financeiro as fin

    state = {
        "bankTransactions": [
            {"date": "2026-08-14", "desc": "padaria", "amount": -50.0},
            {"date": "2026-08-14", "desc": "pix recebido", "amount": 100.0},
            {"date": "2026-08-13", "desc": "ontem", "amount": -30.0},
        ],
        "cardEntries": [
            {"date": "2026-08-14", "desc": "tênis", "amount": 300.0, "installments": 3},
            {"date": "2026-08-10", "desc": "antigo", "amount": 99.0},
        ],
    }

    async def _db(session):
        return None

    async def _state(db, uid):
        return state

    monkeypatch.setattr(fin, "_get_db", _db)
    monkeypatch.setattr(fin, "_read_state", _state)
    user = SimpleNamespace(firebase_uid="u1")
    linhas, total = asyncio.run(fin.gastos_do_dia(None, user, "2026-08-14"))
    assert len(linhas) == 3                       # padaria, pix, tênis
    assert total == 350.0                         # 50 + 300; entrada não soma
    assert any("em 3x" in ln for ln in linhas)
    assert not any("ontem" in ln or "antigo" in ln for ln in linhas)
    assert any("➕" in ln for ln in linhas)        # entrada aparece, marcada


# ─────────────────────────── help casa as frases ───────────────────────────

def test_help_fds_e_noturno() -> None:
    from bot.handlers.start import find_help_sections

    for frase in (
        "como funciona o resumo de fim de semana?",
        "o que vem no resumo de sexta?",
        "que horas sai a rotina noturna?",
        "como o bot fecha o dia à noite?",
    ):
        blocos = find_help_sections(frase)
        assert any("Resumo de fim de semana" in b or "Rotina noturna" in b
                   for b in blocos), frase

    # estreias/em cartaz → seção Cinema
    blocos = find_help_sections("quais as estreias em cartaz no cinema?")
    assert any("Cinema" in b for b in blocos)

    # "esqueci de lançar um gasto" → financeiro
    blocos = find_help_sections("esqueci de lançar um gasto ontem")
    assert any("financeiro" in b.lower() for b in blocos)
