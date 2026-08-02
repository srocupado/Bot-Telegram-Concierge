"""Regressões da auditoria do repositório (fixes de severidade alta/média).

Cada teste nomeia o bug que o motivou; se voltar, o teste diz onde olhar.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest


# ── deliver_to_user: dia sem MP devolve o MESMO formato do caminho cheio ──────
# Bug: `return 0` num caminho e tupla no outro → TypeError em todo dia sem MP
# (/mp_dou_agora dizia "Erro ao gerar a nota" e a fila nunca dava baixa).

def test_deliver_to_user_dia_vazio_devolve_tupla(monkeypatch) -> None:
    from bot.services import dou_monitor

    async def _fetch(_d):
        return dou_monitor.MPList()

    monkeypatch.setattr(dou_monitor, "fetch_mps", _fetch)
    n, falhas, lista = asyncio.run(
        dou_monitor.deliver_to_user(None, None, SimpleNamespace(id=1),
                                    date(2026, 8, 1), force=True)
    )
    assert (n, falhas) == (0, [])
    assert isinstance(lista, dou_monitor.MPList)


# ── deliver_to_user: fila "all" exige fetch completo ─────────────────────────
# Bug: entrega parcial + baixa escondia a MP da seção que falhou.

def test_deliver_apenas_completo_recusa_fetch_parcial(monkeypatch) -> None:
    from bot.services import dou_monitor

    async def _fetch(_d):
        lista = dou_monitor.MPList([{"numero": "1381", "ano": 2026}])
        lista.incompleto = True
        lista.secoes_falhas = ("DO1E",)
        return lista

    monkeypatch.setattr(dou_monitor, "fetch_mps", _fetch)
    with pytest.raises(dou_monitor.DouError):
        asyncio.run(dou_monitor.deliver_to_user(
            None, None, SimpleNamespace(id=1), date(2026, 8, 1),
            force=True, apenas_completo=True,
        ))


# ── analisar_gastos: compra à vista com fatura ≠ mês da compra ───────────────
# Bug: com fechamento dia 10, compra à vista de 15/01 (fatura de fevereiro)
# sumia TANTO de "janeiro" quanto de "fevereiro" (só aparecia em jan+fev).

def _estado_cartao(entries):
    return {
        "cardEntries": entries,
        "bankTransactions": [],
        "categories": [],
        "settings": {"cardClosingDay": 10},
    }


def _rodar_analise(monkeypatch, state, inicio, fim, agrupar="categoria"):
    from bot.services import financeiro

    async def _db(_s):
        return object()

    async def _read(_db_, _uid):
        return state

    monkeypatch.setattr(financeiro, "_get_db", _db)
    monkeypatch.setattr(financeiro, "_read_state", _read)
    monkeypatch.setattr(financeiro, "_require_uid", lambda _u: "uid")
    monkeypatch.setattr(financeiro, "_get_card_closing_day", lambda _s: 10)
    return asyncio.run(financeiro.analisar_gastos(
        None, SimpleNamespace(), inicio, fim, agrupar_por=agrupar, fonte="cartao",
    ))


def test_avista_pos_fechamento_aparece_no_mes_da_fatura(monkeypatch) -> None:
    state = _estado_cartao([{
        "date": "2026-01-15", "amount": 500.0, "category": "mercado",
        "installments": 1, "currentInstallment": 1,
    }])
    fev = _rodar_analise(monkeypatch, state, "2026-02-01", "2026-02-28")
    assert "500,00" in fev, "compra à vista (fatura de fev) sumiu da consulta de fevereiro"
    jan_fev = _rodar_analise(monkeypatch, state, "2026-01-01", "2026-02-28")
    assert "500,00" in jan_fev
    # e NÃO duplica: total continua 500
    assert "1.000,00" not in jan_fev


def test_parcelas_ancoradas_no_mes_de_cada_fatura(monkeypatch) -> None:
    # Bug: 3x de 15/01 aparecia como 300 em janeiro e 0 em fev/mar no
    # agrupamento mensal (todas as parcelas ancoradas na data da compra).
    state = _estado_cartao([{
        "date": "2026-01-05", "amount": 300.0, "category": "eletro",
        "installments": 3, "currentInstallment": 1,
    }])
    out = _rodar_analise(monkeypatch, state, "2026-01-01", "2026-03-31",
                         agrupar="mes")
    assert "2026-01: R$ 100,00" in out
    assert "2026-02: R$ 100,00" in out
    assert "2026-03: R$ 100,00" in out


# ── finance_guard: "Despesa gravada!" alucinada é bloqueada ──────────────────
# Bug: typo "gravd" (cue morto) e "gravado" não casavam o feminino.

def test_guard_bloqueia_gravada_feminino() -> None:
    from bot.services.finance_guard import guard_financial_reply

    out = guard_financial_reply(
        "lança 40 no débito, mercado", False, "Despesa gravada com sucesso!"
    )
    assert "gravada com sucesso" not in out.lower()


# ── trava banco→cartão: tipos acentuados/sinônimos ───────────────────────────
# Bug: "débito"/"gasto"/"pagamento" passavam pela trava e caíam no saldo.

@pytest.mark.parametrize("tipo", ["debito", "débito", "gasto", "pagamento", "despesa"])
def test_trava_cartao_cobre_todos_os_tipos_de_saida(tipo) -> None:
    from bot.services.tools import _looks_like_card_purchase

    assert _looks_like_card_purchase("comprei 300 no cartão em 3x", tipo)


# ── recorrência: weekly sem dias é inválida; monthly guarda âncora ───────────

def test_weekly_sem_dias_e_invalida() -> None:
    from bot.services.reminders import is_valid_recurrence

    assert not is_valid_recurrence("weekly:")
    assert is_valid_recurrence("weekly:mon,fri")
    assert is_valid_recurrence("monthly:31")
    assert not is_valid_recurrence("monthly:0")


def test_monthly_com_ancora_nao_drifta() -> None:
    # Bug: 31/jan → 28/fev → 28 pra sempre. Com âncora, volta pro 31.
    from bot.services.reminders import _passo_recorrencia

    tz = ZoneInfo("America/Sao_Paulo")
    base = datetime(2026, 1, 31, 9, 0, tzinfo=tz)
    fev = _passo_recorrencia("monthly:31", base)
    assert (fev.month, fev.day) == (2, 28)
    mar = _passo_recorrencia("monthly:31", fev)
    assert (mar.month, mar.day) == (3, 31), "âncora perdida após o clamp de fevereiro"


def test_create_reminder_normaliza_monthly(monkeypatch) -> None:
    # A âncora nasce na criação: "monthly" vira "monthly:<dia local>".
    import bot.services.reminders as rem

    gravado = {}

    class _Sessao:
        def add(self, r):
            gravado["rec"] = r.recurrence

        async def commit(self):
            return None

        async def refresh(self, _r):
            return None

    class _Rem(SimpleNamespace):
        pass

    monkeypatch.setattr(rem, "Reminder", _Rem)
    due = datetime(2026, 1, 31, 12, 0, tzinfo=ZoneInfo("UTC"))
    asyncio.run(rem.create_reminder(
        _Sessao(), 1, "pagar", due, recurrence="monthly",
        tz_name="America/Sao_Paulo",
    ))
    assert gravado["rec"] == "monthly:31"


# ── jobs: done-callback não remove o job NOVO da mesma chave ─────────────────

def test_done_callback_nao_apaga_job_novo() -> None:
    from bot.services import jobs

    async def _main():
        async def _rapido():
            return None

        assert jobs.spawn("t:x", _rapido)
        antigo = jobs._jobs["t:x"]
        await antigo  # terminou; o done-callback ainda está na fila do loop
        evento = asyncio.Event()

        async def _lento():
            await evento.wait()

        jobs._jobs.pop("t:x", None)  # simula o que job_em_andamento permite
        assert jobs.spawn("t:x", _lento)
        novo = jobs._jobs["t:x"]
        await asyncio.sleep(0)  # roda o done-callback do job antigo
        assert jobs._jobs.get("t:x") is novo, (
            "o done-callback do job antigo removeu o registro do job novo"
        )
        evento.set()
        await novo

    asyncio.run(_main())


# ── viagens: matcher de nome de hotel com tokens curtos ──────────────────────

def test_hotel_nome_curto_casa() -> None:
    from bot.services.travels.serpapi_client import hotel_name_matches

    assert hotel_name_matches("Yoo2", "Yoo2 Rio de Janeiro by Intercity")
    assert not hotel_name_matches("Yoo2", "Hostel Copacabana")


# ── cotação: dado monetário vai verbatim (direct_html + short_circuit) ───────

def test_cotacao_vai_verbatim(monkeypatch) -> None:
    from bot.services import tools

    async def _cot(_a, _t=None):
        return "USD/BRL: R$ 5,43"

    import bot.services.cotacao as cot
    monkeypatch.setattr(cot, "consultar_cotacao", _cot)
    ctx = SimpleNamespace(direct_html=None, short_circuit=False, fallback_text=None)
    out = asyncio.run(tools._h_consultar_cotacao({"ativo": "dólar"}, ctx))
    assert ctx.short_circuit and "5,43" in (ctx.direct_html or "")
    assert out.startswith("ok")
