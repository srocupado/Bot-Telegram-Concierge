"""Revisão geral de 11/11/2026, lote C — dinheiro e lembretes."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.db.models import ActionLog, Base, ProactiveNotice, User
from bot.services import actions, financeiro as fin, proactive, reminders as rem, tools
from bot.services.llm.base import ToolContext

BRT = ZoneInfo("America/Sao_Paulo")


# ───────────── /lembrar: "18h" é hora do dia, não duração ─────────────

@pytest.mark.parametrize("texto,esperado_txt,hora,minuto", [
    ("pegar encomenda sexta 18h", "pegar encomenda", 18, 0),
    ("dentista amanhã às 15h", "dentista", 15, 0),
    ("dentista sexta às 18h30", "dentista", 18, 30),
    ("pagar 2 boletos amanhã 9h", "pagar 2 boletos", 9, 0),
    ("comprar 2h de estacionamento amanhã 10h", "comprar 2h de estacionamento", 10, 0),
    ("ligar pro banco amanhã 09:00", "ligar pro banco", 9, 0),
])
def test_hora_com_h_e_hora_do_dia(texto, esperado_txt, hora, minuto) -> None:
    txt, quando = rem.parse_reminder(texto, "America/Sao_Paulo")
    local = quando.astimezone(BRT)
    assert txt == esperado_txt
    assert (local.hour, local.minute) == (hora, minuto), local
    if "sexta" in texto:
        assert local.weekday() == 4
    if "amanhã" in texto:
        assert local.date() == (datetime.now(BRT) + timedelta(days=1)).date()


def test_hora_solta_vale_hoje_ou_amanha() -> None:
    txt, quando = rem.parse_reminder("remédio às 22h", "America/Sao_Paulo")
    local = quando.astimezone(BRT)
    assert txt == "remédio" and (local.hour, local.minute) == (22, 0)
    assert quando > datetime.now(timezone.utc)
    assert local.date() - datetime.now(BRT).date() <= timedelta(days=1)


def test_duracao_continua_sendo_duracao() -> None:
    for texto in ("tirar o lixo em 2h", "regar plantas daqui a 2h"):
        txt, quando = rem.parse_reminder(texto, "America/Sao_Paulo")
        delta = quando - datetime.now(timezone.utc)
        assert timedelta(hours=1, minutes=58) < delta < timedelta(hours=2, minutes=2), texto
        assert txt in ("tirar o lixo", "regar plantas")


# ───────────── atalho de LISTAR lembretes não engole criação ─────────────

@pytest.mark.parametrize("texto", [
    "coloca um lembrete que eu tenho médico amanhã às 10h",
    "bota um lembrete pra quinta 15h, tenho dentista",
    "anota nos meus lembretes: ligar pro João amanhã 9h",
    "cancela o lembrete da lista",
    "quero um lembrete, tenho reunião na sexta",
])
def test_pedido_de_criar_ou_cancelar_nao_vira_listagem(texto) -> None:
    assert rem.is_list_reminders_request(texto) is False


@pytest.mark.parametrize("texto", [
    "quais são meus lembretes?", "que lembretes eu tenho?", "lista meus lembretes",
    "tenho algum lembrete pra hoje?", "mostra os lembretes",
])
def test_pedido_de_listar_continua_no_atalho(texto) -> None:
    assert rem.is_list_reminders_request(texto) is True


# ───────────── trava banco→cartão ─────────────

@pytest.mark.parametrize("texto", [
    "gastei 50 no mercado no cartão de débito",
    "paguei o cartão de crédito, 2300",
    "paguei a luz 180 no débito e comprei um tênis de 300 no cartão",
    "paguei 200 de pix pro cartão",
    "pagamento do cartão nubank 1500",
])
def test_saida_da_conta_nao_vira_compra_no_credito(texto) -> None:
    assert tools._looks_like_card_purchase(texto, "debito") is False


@pytest.mark.parametrize("texto", [
    "comprei uma TV no crédito em 3x", "passei 80 no cartão no posto",
])
def test_compra_no_credito_continua_redirecionada(texto) -> None:
    assert tools._looks_like_card_purchase(texto, "debito") is True


# ───────────── extrato "últimos N dias" com parcelada ─────────────

def test_extrato_ultimos_dias_com_compra_parcelada_nao_quebra(monkeypatch) -> None:
    st = {"settings": {"cardClosingDay": 20, "cardDueDay": 1}, "cardEntries": [
        {"id": "a", "date": "2026-11-05", "desc": "mercado", "amount": 100.0,
         "installments": 1, "category": "outros", "source": "bot"},
        {"id": "b", "date": "2026-11-08", "desc": "TV", "amount": 3000.0,
         "installments": 10, "currentInstallment": 1, "category": "compras",
         "source": "bot"}]}

    async def _db(_s):
        return object()

    async def _state(_d, _u):
        return st
    monkeypatch.setattr(fin, "_get_db", _db)
    monkeypatch.setattr(fin, "_read_state", _state)
    out = asyncio.run(fin.consultar_lancamentos(
        None, SimpleNamespace(firebase_uid="u"), "cartao", 30, "2026-11-11",
        escopo_cartao="ultimos_dias"))
    texto = out if isinstance(out, str) else str(out)
    assert "TV" in texto and "10x" in texto


# ───────────── desfazer só o que é recente ─────────────

def _banco():
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                              connect_args={"check_same_thread": False})
    return eng, async_sessionmaker(eng, expire_on_commit=False)


def test_desfazer_nao_pega_acao_antiga() -> None:
    async def _run():
        eng, sm = _banco()
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        async with sm() as s:
            s.add(User(id=1, chat_id=1, is_authorized=True))
            s.add(ActionLog(user_id=1, kind="tarefa", summary="compra de ontem",
                            undo_data="{}",
                            created_at=datetime.now(timezone.utc) - timedelta(hours=20)))
            await s.commit()
            msg = await actions.undo_last(s, SimpleNamespace(id=1))
            linha = (await s.scalars(__import__("sqlalchemy").select(ActionLog))).first()
        await eng.dispose()
        return msg, linha.undone
    msg, undone = asyncio.run(_run())
    assert msg.startswith("nada:") and "compra de ontem" in msg
    assert undone is False


def test_resultado_do_desfazer_vai_verbatim(monkeypatch) -> None:
    async def _undo(_s, _u):
        return "ok: desfiz o lembrete 'pagar IPTU'"
    monkeypatch.setattr(tools, "undo_last", _undo)
    ctx = ToolContext(user=SimpleNamespace(id=1), session=None,
                      tz="America/Sao_Paulo", user_text="desfaz")
    asyncio.run(tools._h_desfazer_ultima_acao({}, ctx))
    assert ctx.short_circuit and "pagar IPTU" in ctx.direct_html


# ───────────── modelo falha DEPOIS da escrita ─────────────

class _Msg:
    def __init__(self):
        self.chat = SimpleNamespace(id=991)
        self.enviadas: list[str] = []

    async def answer(self, texto, **kw):
        self.enviadas.append(texto)


def test_falha_depois_de_escrita_avisa_que_a_acao_foi_feita() -> None:
    from bot.handlers import chat
    ctx = ToolContext(user=SimpleNamespace(id=1), session=None,
                      tz="America/Sao_Paulo", user_text="me lembra do IPTU")
    ctx.tools_chamadas.append("criar_lembrete")
    msg = _Msg()
    assert asyncio.run(chat.entregar_apos_falha(msg, ctx, user_text="x")) is True
    assert "FOI executada" in msg.enviadas[-1] and "criar_lembrete" in msg.enviadas[-1]


def test_falha_sem_escrita_segue_o_erro_normal() -> None:
    from bot.handlers import chat
    ctx = ToolContext(user=SimpleNamespace(id=1), session=None,
                      tz="America/Sao_Paulo", user_text="clima?")
    ctx.tools_chamadas.append("consultar_clima")
    msg = _Msg()
    assert asyncio.run(chat.entregar_apos_falha(msg, ctx, user_text="x")) is False
    assert asyncio.run(chat.entregar_apos_falha(msg, None, user_text="x")) is False
    assert msg.enviadas == []


# ───────────── briefing: marca só depois de entregue ─────────────

class _Bot:
    def __init__(self):
        self.falha, self.enviadas = True, []

    async def send_message(self, chat_id, text, **kw):
        if self.falha:
            raise RuntimeError("TelegramNetworkError")
        self.enviadas.append(text)


def _rodar_briefing(monkeypatch, coletor_quebra=False):
    async def _nada(*a, **k):
        return []

    async def _fato(*a, **k):
        return [proactive.ProactiveFact("tarefas", "tarefa", "1", "• pagar IPTU")]

    async def _boom(*a, **k):
        raise RuntimeError("bug no coletor")
    for c in ("collect_vencimentos", "collect_nudges", "collect_carteira",
              "collect_clima", "collect_transito", "collect_moeda_viagem", "collect_mp"):
        monkeypatch.setattr(proactive, c, _nada)
    monkeypatch.setattr(proactive, "collect_tarefas", _fato)
    proactive._RUN_TENTATIVAS.clear()
    bot = _Bot()
    resultados = []

    async def _run():
        eng, sm = _banco()
        async with eng.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        async with sm() as s:
            s.add(User(id=1, chat_id=1, is_authorized=True, proactive_enabled=True))
            await s.commit()
        for i, minuto in enumerate((5, 6, 7)):
            if coletor_quebra:
                monkeypatch.setattr(proactive, "collect_nudges", _boom if i == 0 else _nada)
            t = datetime(2026, 11, 11, 7, minuto, tzinfo=BRT)
            async with sm() as s:
                u = await s.get(User, 1)
                try:
                    resultados.append(await proactive.run_for_user(bot, s, u, t, window="briefing"))
                except Exception:
                    resultados.append("EXC")
            bot.falha = False
        async with sm() as s:
            from sqlalchemy import select
            marcas = [n.key for n in await s.scalars(
                select(ProactiveNotice).where(ProactiveNotice.kind == "proactive_run"))]
        await eng.dispose()
        return marcas
    marcas = asyncio.run(_run())
    return resultados, bot.enviadas, marcas


def test_envio_que_falha_nao_queima_o_briefing_do_dia(monkeypatch) -> None:
    resultados, enviadas, marcas = _rodar_briefing(monkeypatch)
    assert resultados == [False, True, False], "2ª tentativa entrega; 3ª já está marcada"
    assert len(enviadas) == 1 and "pagar IPTU" in enviadas[0]
    assert marcas == ["briefing:2026-11-11"]


def test_coletor_que_estoura_nao_queima_o_briefing_do_dia(monkeypatch) -> None:
    resultados, enviadas, _ = _rodar_briefing(monkeypatch, coletor_quebra=True)
    assert resultados[0] == "EXC" and resultados[1] is True
    assert len(enviadas) == 1


def test_janela_desiste_depois_do_teto_de_tentativas(monkeypatch) -> None:
    monkeypatch.setattr(proactive, "_RUN_MAX_TENTATIVAS", 1)
    resultados, enviadas, marcas = _rodar_briefing(monkeypatch)
    assert resultados[0] is False and enviadas == []
    assert marcas == ["briefing:2026-11-11"], "desistiu e marcou — sem re-coleta infinita"
