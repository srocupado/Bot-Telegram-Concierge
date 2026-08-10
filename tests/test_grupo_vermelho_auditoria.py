"""Regressões do GRUPO VERMELHO — bugs reais recuperados do commit revertido
0cc8c29, confirmados ainda quebrados no main e reaplicados limpo.

Cada teste nomeia o bug; se voltar, o teste diz onde olhar.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest


# (Os testes #1 — âncora de fatura na análise de gastos — saíram junto com a
# tool analisar_gastos, removida a pedido do dono em 10/08/2026: nunca usou.
# A lógica de fatura que SEGUE viva — _entry_in_bill/_bill_month_for_date no
# consultar_lancamentos — continua coberta em test_fatura_cartao.py e
# test_fatura_fechamento_dia1.py.)

# ═══ #6 cotação: dado monetário vai VERBATIM ═════════════════════════════════

def test_cotacao_vai_verbatim(monkeypatch) -> None:
    from bot.services import tools
    import bot.services.cotacao as cot

    async def _cot(_a, _t=None):
        return "USD/BRL: R$ 5,43"

    monkeypatch.setattr(cot, "consultar_cotacao", _cot)
    ctx = SimpleNamespace(direct_html=None, short_circuit=False, fallback_text=None)
    out = asyncio.run(tools._h_consultar_cotacao({"ativo": "dólar"}, ctx))
    assert ctx.short_circuit and "5,43" in (ctx.direct_html or "")
    assert out.startswith("ok")


# ═══ #7 finance_guard: "Despesa gravada!" alucinada é bloqueada ══════════════

def test_guard_bloqueia_gravada_feminino() -> None:
    from bot.services.finance_guard import guard_financial_reply

    out = guard_financial_reply(
        "lança 40 no débito, mercado", False, "Despesa gravada com sucesso!"
    )
    assert "gravada com sucesso" not in out.lower()


# ═══ #8 jobs: done-callback não remove o job NOVO da mesma chave ═════════════

def test_done_callback_nao_apaga_job_novo() -> None:
    from bot.services import jobs

    async def _main():
        async def _rapido():
            return None

        assert jobs.spawn("t:x", lambda: _rapido())
        antigo = jobs._jobs["t:x"]
        await antigo
        evento = asyncio.Event()

        async def _lento():
            await evento.wait()

        jobs._jobs.pop("t:x", None)
        assert jobs.spawn("t:x", lambda: _lento())
        novo = jobs._jobs["t:x"]
        await asyncio.sleep(0)   # roda o done-callback do job antigo
        assert jobs._jobs.get("t:x") is novo, (
            "done-callback do job antigo removeu o registro do job novo"
        )
        evento.set()
        await novo

    asyncio.run(_main())


# ═══ #9 reminders: weekly vazio inválido; monthly com âncora não drifta ══════

def test_weekly_sem_dias_e_invalida() -> None:
    from bot.services.reminders import is_valid_recurrence

    assert not is_valid_recurrence("weekly:")
    assert is_valid_recurrence("weekly:mon,fri")
    assert is_valid_recurrence("monthly:31")
    assert not is_valid_recurrence("monthly:0")


def test_monthly_com_ancora_nao_drifta() -> None:
    from bot.services.reminders import _passo_recorrencia

    tz = ZoneInfo("America/Sao_Paulo")
    fev = _passo_recorrencia("monthly:31", datetime(2026, 1, 31, 9, 0, tzinfo=tz))
    assert (fev.month, fev.day) == (2, 28)
    mar = _passo_recorrencia("monthly:31", fev)
    assert (mar.month, mar.day) == (3, 31), "âncora perdida após o clamp de fevereiro"


def test_create_reminder_normaliza_monthly(monkeypatch) -> None:
    import bot.services.reminders as rem

    gravado = {}

    class _Sessao:
        def add(self, r):
            gravado["rec"] = r.recurrence

        async def commit(self):
            return None

        async def refresh(self, _r):
            return None

    monkeypatch.setattr(rem, "Reminder", SimpleNamespace)
    asyncio.run(rem.create_reminder(
        _Sessao(), 1, "pagar", datetime(2026, 1, 31, 12, 0, tzinfo=ZoneInfo("UTC")),
        recurrence="monthly", tz_name="America/Sao_Paulo",
    ))
    assert gravado["rec"] == "monthly:31"


# ═══ #4 anthropic: pause_turn não é tratado como resposta final ══════════════

def test_pause_turn_continua_o_turno() -> None:
    from bot.services.llm import anthropic_impl as ai

    class _Blk:
        def __init__(self, t):
            self.type = "text"
            self.text = t

        def model_dump(self):
            return {"type": "text", "text": self.text}

    class _Resp:
        def __init__(self, stop, txt):
            self.stop_reason = stop
            self.content = [_Blk(txt)]
            self.usage = SimpleNamespace(input_tokens=1, output_tokens=1)

    respostas = [_Resp("pause_turn", "parcial"), _Resp("end_turn", "final completo")]

    class _Msgs:
        def create(self, **kw):
            return respostas.pop(0)

    class _Cli:
        def __init__(self):
            self.messages = _Msgs()

        def with_options(self, **kw):
            return self

    prov = ai.AnthropicProvider.__new__(ai.AnthropicProvider)
    prov.client = _Cli()
    prov.model = "claude-x"
    out = asyncio.run(prov.chat_with_tools(
        [{"role": "user", "content": "oi"}], tools=[], ctx=SimpleNamespace(
            direct_html=None, short_circuit=False, fallback_text=None),
        system="s", max_tokens=100,
    ))
    assert out == "final completo", "pause_turn foi tratado como final (texto parcial)"


def test_max_tokens_sinaliza_corte() -> None:
    from bot.services.llm import anthropic_impl as ai

    class _Blk:
        type = "text"
        text = "resposta longa cortada"

        def model_dump(self):
            return {"type": "text", "text": self.text}

    class _Resp:
        stop_reason = "max_tokens"
        content = [_Blk()]
        usage = SimpleNamespace(input_tokens=1, output_tokens=1)

    class _Cli:
        class messages:
            @staticmethod
            def create(**kw):
                return _Resp()

        def with_options(self, **kw):
            return self

    prov = ai.AnthropicProvider.__new__(ai.AnthropicProvider)
    prov.client = _Cli()
    prov.model = "claude-x"
    out = asyncio.run(prov.chat_with_tools(
        [{"role": "user", "content": "oi"}], tools=[], ctx=SimpleNamespace(
            direct_html=None, short_circuit=False, fallback_text=None),
        system="s", max_tokens=10,
    ))
    assert "cortada pelo limite" in out


# ═══ #5 memória: compactações concorrentes serializadas ══════════════════════

def test_compactacao_e_serializada_por_usuario(monkeypatch) -> None:
    """Duas compactações do MESMO usuário não podem rodar sobrepostas —
    senão a segunda sobrescreve o resumo da primeira (perda silenciosa)."""
    import bot.services.memoria as mem

    mem._compact_locks.clear()
    # _compact só exige que o sessionmaker exista (o corpo real é mockado).
    monkeypatch.setattr(mem, "_sessionmaker", object())
    pico = {"atual": 0, "max": 0}

    async def _serial(_uid, _msgs):
        pico["atual"] += 1
        pico["max"] = max(pico["max"], pico["atual"])
        await asyncio.sleep(0.02)
        pico["atual"] -= 1

    monkeypatch.setattr(mem, "_compact_serializado", _serial)

    async def _main():
        await asyncio.gather(_c(mem), _c(mem), _c(mem))

    async def _c(m):
        await m._compact(7, [])

    asyncio.run(_main())
    assert pico["max"] == 1, "compactações do mesmo usuário rodaram concorrentes"


# ═══ #1b deliver_to_user: dia sem MP devolve TUPLA (não int) ═════════════════
# Regressão do fix da tupla: `if not novas: return 0` cru fazia o caller
# `n, _falhas = await deliver_to_user(...)` estourar TypeError em todo dia sem
# MP → "Erro ao gerar a nota técnica" no /mp_dou_agora.

def test_deliver_dia_sem_mp_devolve_tupla(monkeypatch) -> None:
    from bot.services import dou_monitor

    async def _fetch(_d):
        return dou_monitor.MPList()   # nenhuma MP

    async def _filter(_s, _u, mps):
        return []

    monkeypatch.setattr(dou_monitor, "fetch_mps", _fetch)
    monkeypatch.setattr(dou_monitor, "filter_unseen", _filter)
    resultado = asyncio.run(dou_monitor.deliver_to_user(
        None, SimpleNamespace(), SimpleNamespace(id=1), date(2026, 8, 2),
    ))
    n, falhas, motivo = resultado   # 3-tupla; NÃO pode estourar (era int)
    assert (n, falhas) == (0, [])
    assert motivo == "sem_mp"   # MPList() sem flags → houve DOU, 0 MP


# ═══ #3 congresso: usa data BRT, não o relógio UTC do container ══════════════

def test_congresso_usa_data_brt() -> None:
    """Não dá pra testar o relógio, mas garante que a correção está no código
    (datetime.now(UTC).date() num domingo à noite pulava a semana da pauta)."""
    import inspect
    from bot.services import scheduled_actions
    fonte = inspect.getsource(scheduled_actions._run_congresso)
    assert "America/Sao_Paulo" in fonte
    assert "datetime.now().date()" not in fonte
