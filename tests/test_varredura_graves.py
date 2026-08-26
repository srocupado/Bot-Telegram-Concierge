"""Regressões dos achados GRAVES da varredura de 26/08/2026.

Todos compartilham o mesmo tema: falha parcial não pode virar perda silenciosa
(MP sem aviso, aporte gravado 100× maior, semana do Congresso engolida, comando
agendado em loop mudo). Cada teste registra o cenário concreto que motivou o
conserto.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.services import dou_monitor as dm
from bot.services import proactive

D = date(2026, 8, 20)


# ───────────────── outbox cobre por NÚMERO, não por data ─────────────────

class _Sessao:
    def __init__(self, pendencias=()):
        self.rows = [SimpleNamespace(key=k) for k in pendencias]

    async def scalars(self, _stmt):
        return list(self.rows)

    async def commit(self):
        return None


@pytest.fixture
def espiao(monkeypatch):
    reg = {"mark": [], "unmark": []}

    async def _mark(_s, _uid, kind, key):
        reg["mark"].append((kind, key))

    async def _unmark(_s, _uid, kind, key):
        reg["unmark"].append((kind, key))

    monkeypatch.setattr(proactive, "mark_notified", _mark)
    monkeypatch.setattr(proactive, "unmark_notified", _unmark)
    return reg


def test_outbox_abre_para_mp_descoberta(espiao) -> None:
    """O caso da extra das 18h: 'D:1400' na fila NÃO cobre a 1401 recém-
    publicada — sem outbox próprio, um restart no meio da geração matava a
    nota da 1401 sem re-tentativa (ela já estava vista e a Câmara satisfeita)."""
    sessao = _Sessao([f"{D.isoformat()}:1400"])
    chave = asyncio.run(dm._abrir_outbox(
        sessao, 1, D, [{"numero": "1400"}, {"numero": "1401"}]))
    assert chave == f"{D.isoformat()}:1401", "só a descoberta entra na chave nova"
    assert espiao["mark"] == [("nota_pendente", chave)]


def test_outbox_nao_duplica_cobertura_existente(espiao) -> None:
    sessao = _Sessao([f"{D.isoformat()}:1400,1401"])
    chave = asyncio.run(dm._abrir_outbox(
        sessao, 1, D, [{"numero": "1401"}]))
    assert chave is None and espiao["mark"] == []


def test_outbox_entrada_all_cobre_o_dia(espiao) -> None:
    sessao = _Sessao([f"{D.isoformat()}:all"])
    chave = asyncio.run(dm._abrir_outbox(sessao, 1, D, [{"numero": "1401"}]))
    assert chave is None and espiao["mark"] == []


# ───────────────── chave do job de nota carrega o ALVO ─────────────────

def test_chave_job_por_alvo_distingue_botoes() -> None:
    """Dois botões (MPs diferentes do mesmo dia) têm chaves diferentes —
    antes o segundo clique era recusado com 'te mando assim que sair' e a
    nota nunca nascia."""
    a = dm.chave_job_nota(7, D, ["1385"])
    b = dm.chave_job_nota(7, D, ["1386"])
    dia = dm.chave_job_nota(7, D)
    assert a != b != dia
    assert a == dm.chave_job_nota(7, D, ["1.385"]), "alvo canônico (ponto ignorado)"
    prefixo = dm.chave_nota_prefixo(7, D)
    assert all(k.startswith(prefixo) for k in (a, b, dia))


# ─────────────── aviso falho entra em `falhas` do deliver ───────────────

class _SessaoDeliver:
    async def scalars(self, _stmt):
        return []

    async def commit(self):
        return None


class _BotFalhaNo1401:
    def __init__(self):
        self.enviadas = []

    async def send_message(self, _chat, text, **kw):
        if "1401-MARCA" in text:
            raise RuntimeError("rede caiu no meio do lote")
        self.enviadas.append(text)


def test_aviso_falho_vira_falha_do_lote(monkeypatch) -> None:
    """MP cujo AVISO falhou não pode sumir do retorno: o caller da fila via
    falhas=[] + entregues>0 e baixava a chave inteira — a MP perdia aviso E
    nota sem rastro."""
    mps = [
        {"numero": "1400", "ano": 2026, "titulo": "t", "ementa": "e",
         "data": D, "marca": "1400-MARCA"},
        {"numero": "1401", "ano": 2026, "titulo": "t", "ementa": "e",
         "data": D, "marca": "1401-MARCA"},
    ]

    class _Lista(list):
        provisorio = incompleto = sem_edicao = False

    async def _fake_fetch(_d):
        return _Lista(mps)

    async def _nada(*a, **kw):
        return None

    monkeypatch.setattr(dm, "fetch_mps", _fake_fetch)
    monkeypatch.setattr(dm, "format_telegram_message",
                        lambda mp, _n: mp["marca"])
    monkeypatch.setattr(dm, "mark_seen", _nada)
    monkeypatch.setattr(dm, "gerar_e_enviar_nota", _nada)
    monkeypatch.setattr(proactive, "marcar_nota_entregue", _nada)
    monkeypatch.setattr(proactive, "mark_notified", _nada)
    monkeypatch.setattr(proactive, "unmark_notified", _nada)

    user = SimpleNamespace(id=1, is_authorized=True)
    entregues, falhas, motivo = asyncio.run(dm.deliver_to_user(
        _BotFalhaNo1401(), _SessaoDeliver(), user, D,
        force=True, only_numeros=["1400", "1401"],
    ))
    assert entregues == 1
    assert "1401" in falhas, "aviso falho sumiu do lote — baixa indevida na fila"


# ──────────── desistências só dão baixa com o aviso entregue ────────────

def test_dia_expirado_fica_marcado_ate_o_aviso_sair(espiao) -> None:
    """_mp_dias_pendentes não desmarca mais o expirado: a baixa é do pós-envio
    do run (kind mp_desisti). Envio falho = pendência viva + aviso re-tentado."""
    velho = date.today() - timedelta(days=proactive._MP_RETRO_EXPIRA_DIAS + 3)
    sessao = _Sessao([velho.isoformat()])
    desistidos: list[date] = []
    out = asyncio.run(proactive._mp_dias_pendentes(
        sessao, 1, date.today(), desistidos))
    assert out == [] and desistidos == [velho]
    assert espiao["unmark"] == [], "baixou pendência sem envio confirmado"


def test_nota_expirada_so_baixa_com_aviso_entregue(monkeypatch, espiao) -> None:
    velho = date.today() - timedelta(days=proactive._NOTA_PENDENTE_EXPIRA_DIAS + 2)
    rows = _Sessao([f"{velho.isoformat()}:1390"])
    envios = {"n": 0, "ok": False}

    async def _send(_bot, _uid, _texto, reply_markup=None):
        envios["n"] += 1
        return envios["ok"]

    monkeypatch.setattr(proactive, "_send", _send)
    monkeypatch.setattr(proactive.jobs, "job_em_andamento", lambda _k: False)
    monkeypatch.setattr(proactive.jobs, "spawn", lambda _k, _f: False)

    user = SimpleNamespace(id=1)
    # 1ª janela: envio do aviso FALHA → entrada fica.
    asyncio.run(proactive._processar_notas_pendentes(None, rows, user))
    assert envios["n"] == 1 and espiao["unmark"] == []
    # 2ª janela: envio OK → baixa.
    envios["ok"] = True
    asyncio.run(proactive._processar_notas_pendentes(None, rows, user))
    assert ("nota_pendente", f"{velho.isoformat()}:1390") in espiao["unmark"]


# ───────────────────────── financeiro ─────────────────────────

def test_taxa_sempre_percentual() -> None:
    """'aportei no Selic a 0,15%' gravava rate=0.15 = 15% a.a. no Firestore
    que o app lê — patrimônio projetado ~100× maior. O contrato da tool é
    % a.a. SEMPRE."""
    from bot.services.financeiro import FinanceiroError, _taxa_para_fracao
    assert _taxa_para_fracao(6) == pytest.approx(0.06)
    assert _taxa_para_fracao(0.15) == pytest.approx(0.0015)
    with pytest.raises(FinanceiroError):
        _taxa_para_fracao(200)
    with pytest.raises(FinanceiroError):
        _taxa_para_fracao(0)


def test_filter_by_days_sobrevive_a_date_null() -> None:
    """Um item com date:null (escrito pelo app) derrubava o extrato INTEIRO
    (TypeError que o except ValueError não pegava)."""
    from bot.services.financeiro import _filter_by_days
    hoje = "2026-08-26"
    itens = [
        {"date": None, "amount": 10},
        {"date": "2026-08-25", "amount": 20},
        {"date": "2026-08-25 14:00", "amount": 30},
    ]
    out = _filter_by_days(itens, 30, hoje)
    assert [i["amount"] for i in out] == [20, 30], (
        "item sujo deve ser pulado (e data com hora, aceita) — nunca derrubar tudo")


def test_operacao_ativo_nao_duplica(monkeypatch) -> None:
    """tool_use duplicado no mesmo turno gravava a compra DUAS vezes — qty e
    P&L dobrados. Mesma guarda de idempotência dos outros lançamentos."""
    from bot.services import tools

    gravadas = {"n": 0}

    async def _fake_reg(*a, **kw):
        gravadas["n"] += 1
        return {"operation": {"id": "op1", "type": "buy"}, "ticker": "HGLG11"}

    import bot.services.financeiro as fin
    monkeypatch.setattr(fin, "registrar_operacao_ativo", _fake_reg)
    monkeypatch.setattr(fin, "confirm_operacao_ativo", lambda *a: "ok compra")
    monkeypatch.setattr(tools, "_lancamentos_recentes", {}, raising=True)

    async def _rec(*a, **kw):
        return None

    monkeypatch.setattr(tools, "record_action", _rec)
    ctx = SimpleNamespace(user=SimpleNamespace(id=1), session=None, tz="America/Sao_Paulo",
                          fallback_text=None, direct_html=None, short_circuit=False,
                          financial_logged_ok=False)
    args = {"ticker": "HGLG11", "classe": "fii", "op_type": "buy",
            "qty": 10, "price": 168.5, "data_iso": "2026-08-26"}
    r1 = asyncio.run(tools._h_registrar_operacao_ativo(dict(args), ctx))
    r2 = asyncio.run(tools._h_registrar_operacao_ativo(dict(args), ctx))
    assert gravadas["n"] == 1, "segunda chamada idêntica gravou de novo"
    assert "REPETIDO" in r2


# ───────────────── comando agendado não entra em loop ─────────────────

def test_comando_agendado_que_estoura_e_consumido_com_aviso(monkeypatch) -> None:
    """Handler que levanta exceção: antes ela subia até o run_reminders, o
    Reminder não era consumido e o tick re-executava (com header) a cada 60s
    pra sempre. Agora: ocorrência consumida + aviso explícito."""
    from bot.handlers import voice
    from bot.services import scheduled_actions as sa

    async def _boom(message):
        raise RuntimeError("handler quebrado")

    monkeypatch.setitem(voice._DISPATCH, "boom_teste", _boom)

    class _Bot:
        def __init__(self):
            self.enviadas = []

        async def send_message(self, _chat, text, **kw):
            self.enviadas.append(text)

    bot = _Bot()
    user = SimpleNamespace(id=1)
    consumido = asyncio.run(sa.run_action(bot, None, user, "chat", "/boom_teste"))
    assert consumido is True, "exceção do handler não pode manter a ocorrência viva"
    assert any("falhou ao executar" in t for t in bot.enviadas), \
        "consumir sem aviso é desistência muda"
