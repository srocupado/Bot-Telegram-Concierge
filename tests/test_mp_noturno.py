"""Última checagem de MP no fechamento do dia (~21h30).

Pedido do dono (26/09/2026): as janelas de MP param às 19h05, e uma MP de
edição extra publicada depois disso só chegava no briefing das 7h05. O
fechamento do dia passa a rodar o MESMO collect_mp das janelas e a trazer
SEMPRE uma linha de status — checagem extra que some da mensagem é
indistinguível de "não rodou".
"""
from __future__ import annotations

import asyncio
import types
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.db.models import Base, ProactiveNotice, User
from bot.services import proactive, scheduler
from bot.services.proactive import ProactiveFact

_USER = types.SimpleNamespace(id=1, dou_mp_subscribed=True)


def _rodar(monkeypatch, facts, apurado=None, explode=False):
    async def _collect(session, user, dates, *, apurado=None, **kw):
        if explode:
            raise RuntimeError("portal caiu")
        apurado.update(_apurado)
        return list(facts)
    _apurado = apurado or {}
    monkeypatch.setattr(proactive, "collect_mp", _collect)
    return asyncio.run(proactive.checagem_mp_noturna(None, _USER))


def _textos(facts):
    return "\n".join(f.text for f in facts)


def test_sem_assinatura_nao_checa(monkeypatch) -> None:
    u = types.SimpleNamespace(id=1, dou_mp_subscribed=False)
    assert asyncio.run(proactive.checagem_mp_noturna(None, u)) == []


def test_mp_nova_fala_por_si(monkeypatch) -> None:
    mp = ProactiveFact("mp", "mp", "1400/2026", "📜 MP 1.400/2026: x",
                       date_iso="2026-09-26")
    out = _rodar(monkeypatch, [mp], {"completo": True, "mps_no_dia": 1})
    assert out == [mp]


def test_sem_mp_usa_a_linha_de_fechamento_do_collect(monkeypatch) -> None:
    fecha = ProactiveFact("mp", "mp_checagem", "2026-09-26:fecha",
                          "📄 DOU de hoje: sem MP na checagem das 21h30")
    out = _rodar(monkeypatch, [fecha], {"completo": True, "mps_no_dia": 0})
    assert out == [fecha]


def test_falha_ja_avisada_mais_cedo_ainda_aparece(monkeypatch) -> None:
    """O aviso de falha tem dedup por dia: se saiu às 19h05, o collect_mp
    não o repete às 21h30. Sem a linha própria, a noite ficaria MUDA sobre
    uma checagem que falhou — o pior modo de falha do projeto."""
    out = _rodar(monkeypatch, [], {"falhou": True})
    txt = _textos(out)
    assert "não consegui checar" in txt
    assert "NÃO assuma" in txt


def test_falha_com_aviso_do_collect_nao_duplica(monkeypatch) -> None:
    aviso = ProactiveFact("mp", "mp_fail", "fail:2026-09-26",
                          "⚠️ Não consegui checar o DOU")
    out = _rodar(monkeypatch, [aviso], {"falhou": True})
    assert out == [aviso]


def test_mp_do_dia_ja_enviada_diz_que_nao_ha_nova(monkeypatch) -> None:
    out = _rodar(monkeypatch, [], {"completo": True, "mps_no_dia": 2})
    assert "nenhuma MP nova além da(s) 2" in _textos(out)


def test_dia_aberto_nao_afirma_sem_mp(monkeypatch) -> None:
    out = _rodar(monkeypatch, [], {"pendente": True})
    txt = _textos(out)
    assert "não tem veredito" in txt
    assert "sem MP" not in txt


def test_estado_desconhecido_nunca_vira_sem_mp(monkeypatch) -> None:
    out = _rodar(monkeypatch, [], {})
    txt = _textos(out)
    assert "sem veredito claro" in txt
    assert "sem MP" not in txt and "nenhuma MP" not in txt


def test_checagem_que_quebra_avisa(monkeypatch) -> None:
    out = _rodar(monkeypatch, [], explode=True)
    txt = _textos(out)
    assert "quebrou" in txt and "NÃO assuma" in txt


# ───────────── integração com o fechamento do dia ─────────────

class _Bot:
    def __init__(self, falhas: int = 0):
        self.falhas = falhas
        self.enviadas: list[tuple[str, object]] = []

    async def send_message(self, chat_id, text, **kw):
        if self.falhas > 0:
            self.falhas -= 1
            raise RuntimeError("rede fora")
        self.enviadas.append((text, kw.get("reply_markup")))


class _DT(datetime):
    """21h40 BRT de 26/09/2026."""

    @classmethod
    def now(cls, tz=None):
        base = datetime(2026, 9, 27, 0, 40, tzinfo=timezone.utc)
        return base.astimezone(tz) if tz else base.replace(tzinfo=None)


def test_fechamento_traz_a_mp_e_so_marca_depois_de_entregar(monkeypatch) -> None:
    monkeypatch.setattr(scheduler, "datetime", _DT)
    monkeypatch.setattr(scheduler.settings, "night_summary_enabled", True)
    monkeypatch.setattr(scheduler.settings, "proactive_enabled", True)
    scheduler._MP_NOTURNO_PENDENTE.clear()

    async def _montar(session, user, now_local):
        return "🌙 <b>Fechando o dia</b>"
    monkeypatch.setattr(proactive, "montar_resumo_noturno", _montar)

    checagens = {"n": 0}
    mp = ProactiveFact("mp", "mp", "1400/2026", "📜 MP 1.400/2026: teste",
                       date_iso="2026-09-26")

    async def _checar(session, user):
        checagens["n"] += 1
        return [mp]
    monkeypatch.setattr(proactive, "checagem_mp_noturna", _checar)

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    sm = async_sessionmaker(engine, expire_on_commit=False)
    bot = _Bot(falhas=2)  # 1º tick: HTML e texto puro falham

    async def _kinds():
        async with sm() as s:
            rows = await s.scalars(select(ProactiveNotice))
            return {(r.kind, r.key) for r in rows}

    async def _main():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sm() as s:
            s.add(User(id=1, chat_id=1, is_authorized=True,
                       proactive_enabled=True, dou_mp_subscribed=True))
            await s.commit()

        await scheduler.run_resumo_noturno(sm, bot)
        assert bot.enviadas == []
        # Envio falhou: nada de baixa/dedup — a MP tem que voltar.
        assert ("mp", "1400/2026") not in await _kinds()

        await scheduler.run_resumo_noturno(sm, bot)
        assert len(bot.enviadas) == 1
        texto, teclado = bot.enviadas[0]
        assert "Fechando o dia" in texto and "MP 1.400/2026" in texto
        assert teclado is not None, "sem o botão de gerar a nota"
        kinds = await _kinds()
        assert ("mp", "1400/2026") in kinds
        assert ("noturno", "2026-09-26") in kinds

        await scheduler.run_resumo_noturno(sm, bot)
        assert len(bot.enviadas) == 1, "fechamento saiu 2x"

    asyncio.run(_main())
    # A re-tentativa de envio NÃO refaz portal/Inlabs/Planalto.
    assert checagens["n"] == 1


# ───────────── collect_mp de verdade, com a fonte fora ─────────────

class _FakeSession:
    def __init__(self, *respostas):
        self._respostas = list(respostas)

    async def scalars(self, _stmt):
        return list(self._respostas.pop(0)) if self._respostas else []

    async def commit(self):
        return None


def test_collect_mp_real_apura_falha_e_a_noite_diz_mesmo_com_aviso_ja_dado(
    monkeypatch,
) -> None:
    """Fonte fora e o aviso de falha do dia JÁ dado (already_notified=True
    pra tudo): o collect_mp não repete o aviso, mas o fechamento das 21h30
    tem que dizer que a checagem de agora falhou."""
    from datetime import timedelta
    from bot.services import dou_monitor

    dou_monitor._ultima_ok.clear()
    hoje = datetime.now(proactive.BRT).date()

    async def _fetch(_d):
        raise dou_monitor.DouError("Inlabs recusou a sessão")

    async def _true(*a, **kw):
        return True

    async def _none(*a, **kw):
        return None

    monkeypatch.setattr(dou_monitor, "fetch_mps", _fetch)
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", False)
    monkeypatch.setattr(proactive.settings, "dou_planalto_enabled", False)
    monkeypatch.setattr(proactive, "already_notified", _true)
    monkeypatch.setattr(proactive, "mark_notified", _none)
    monkeypatch.setattr(proactive, "unmark_notified", _none)

    user = types.SimpleNamespace(id=99, dou_mp_subscribed=True,
                                 dou_ultimo_dia_ok=hoje - timedelta(days=1))
    apurado: dict = {}
    facts = asyncio.run(proactive.collect_mp(
        _FakeSession([], [], []), user, [hoje], apurado=apurado))
    assert apurado["falhou"] is True
    assert not any(f.kind == "mp_fail" for f in facts), "dedup deveria calar o aviso"

    out = asyncio.run(proactive.checagem_mp_noturna(
        _FakeSession([], [], []), user))
    assert "não consegui checar" in _textos(out)
    dou_monitor._ultima_ok.clear()
