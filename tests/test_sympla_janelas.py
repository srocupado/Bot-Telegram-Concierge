"""Retirada da Sympla em duas janelas: quarta 18h e quinta 12h.

Pedido do dono (01/10/2026): na semana de 30/09 a Sympla abriu um 2º lote
("Ingresso Antecipado 12h (Quinta-feira)", vendas até 13h) e o bot só
tentava na quarta. Agora tenta também na quinta — só se a de quarta não deu
certo — e, na quinta, espera o lote abrir (às 11h55 o evento já existe com o
lote "Não iniciado"; sem esperar, o bot desistia antes das 12h).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.db.models import Base, User
from bot.services import jobs, scheduler
from bot.services import sympla as sy

BRT = sy.BRT


# ───────────── parse_janela ─────────────

@pytest.mark.parametrize("valor,esperado", [
    ("3@12", (3, 12)), (" 2@18 ", (2, 18)), ("", None), (None, None),
    ("quinta@12", None), ("7@12", None), ("3@24", None), ("3", None),
])
def test_parse_janela(valor, esperado) -> None:
    assert sy.parse_janela(valor) == esperado


# ───────────── agendador ─────────────

def _relogio(monkeypatch, quando: datetime) -> None:
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return quando.astimezone(tz) if tz else quando.replace(tzinfo=None)
    monkeypatch.setattr(scheduler, "datetime", _DT)


class _Bot:
    def __init__(self):
        self.textos: list[str] = []

    async def send_message(self, chat_id, texto, **kw):
        self.textos.append(texto)

    async def send_photo(self, *a, **kw):
        pass


@pytest.fixture
def ambiente(monkeypatch):
    monkeypatch.setattr(scheduler.settings, "owner_telegram_id", 1)
    monkeypatch.setattr(scheduler.settings, "sympla_weekday", 2)
    monkeypatch.setattr(scheduler.settings, "sympla_release_hour", 18)
    monkeypatch.setattr(scheduler.settings, "sympla_segunda_janela", "3@12")

    async def _creds(_s):
        return sy.SymplaCredenciais("x@x.com", "1234x", "X Y", None)
    monkeypatch.setattr(sy, "get_credenciais", _creds)

    disparos: list[tuple[str, object]] = []
    monkeypatch.setattr(jobs, "spawn", lambda chave, fab: disparos.append((chave, fab)) or True)

    chamadas: list[dict] = []
    resultado = {"sucesso": False}

    async def _retirar(creds, query, qty, **kw):
        chamadas.append(kw)
        return sy.SymplaResultado(resultado["sucesso"], "concluído" if resultado["sucesso"]
                                  else "selecionar ingressos e reservar", "detalhe")
    monkeypatch.setattr(sy, "retirar_ingresso", _retirar)

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    sm = async_sessionmaker(engine, expire_on_commit=False)

    async def _init():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sm() as s:
            s.add(User(id=1, chat_id=1, is_authorized=True))
            await s.commit()
    asyncio.run(_init())

    class _Amb:
        pass
    amb = _Amb()
    amb.sm, amb.disparos, amb.chamadas, amb.resultado = sm, disparos, chamadas, resultado
    amb.bot = _Bot()

    def tick(quando: datetime):
        _relogio(monkeypatch, quando)
        antes = len(disparos)
        asyncio.run(scheduler.run_sympla_pickup(sm, amb.bot))
        novos = disparos[antes:]
        for _chave, fab in novos:
            asyncio.run(fab())
        return [c for c, _ in novos]
    amb.tick = tick
    return amb


QUA = datetime(2026, 10, 7, tzinfo=BRT)   # quarta
QUI = datetime(2026, 10, 8, tzinfo=BRT)   # quinta


def test_quarta_dispara_e_espera_as_18h(ambiente) -> None:
    assert ambiente.tick(QUA.replace(hour=17, minute=56)) == ["sympla:2026-10-07"]
    assert ambiente.chamadas[0]["abre_em"] == QUA.replace(hour=18)
    assert "(quarta 18h)" in ambiente.bot.textos[0]


def test_quarta_depois_das_18h_ainda_dispara(ambiente) -> None:
    """Antes, às 18h05 a janela já apontava pra semana seguinte: um restart
    do Pi logo depois das 18h perdia a tentativa."""
    assert ambiente.tick(QUA.replace(hour=18, minute=20)) == ["sympla:2026-10-07"]


def test_mesma_janela_nao_dispara_duas_vezes(ambiente) -> None:
    ambiente.tick(QUA.replace(hour=17, minute=56))
    assert ambiente.tick(QUA.replace(hour=17, minute=57)) == []


def test_quinta_tenta_se_a_quarta_falhou(ambiente) -> None:
    ambiente.tick(QUA.replace(hour=17, minute=56))
    assert ambiente.tick(QUI.replace(hour=11, minute=56)) == ["sympla:2026-10-08"]
    assert ambiente.chamadas[-1]["abre_em"] == QUI.replace(hour=12)
    assert "(quinta 12h)" in ambiente.bot.textos[-1]


def test_quinta_nao_roda_se_a_quarta_retirou(ambiente) -> None:
    ambiente.resultado["sucesso"] = True
    ambiente.tick(QUA.replace(hour=17, minute=56))
    assert ambiente.tick(QUI.replace(hour=11, minute=56)) == []


def test_quinta_roda_mesmo_sem_tentativa_na_quarta(ambiente) -> None:
    """Bot fora do ar na quarta: a quinta é a chance que sobra."""
    assert ambiente.tick(QUI.replace(hour=11, minute=58)) == ["sympla:2026-10-08"]


def test_segunda_janela_vazia_desliga_a_quinta(ambiente, monkeypatch) -> None:
    monkeypatch.setattr(scheduler.settings, "sympla_segunda_janela", "")
    assert ambiente.tick(QUI.replace(hour=11, minute=56)) == []


def test_fora_das_janelas_nao_faz_nada(ambiente) -> None:
    assert ambiente.tick(QUI.replace(hour=13, minute=10)) == []
    assert ambiente.tick(QUA.replace(hour=17, minute=40)) == []


# ───────────── esperar o lote abrir ─────────────

class _PaginaLote:
    def __init__(self, abre_na_recarga: int):
        self.abre_na_recarga = abre_na_recarga
        self.recargas = 0
        self.esperas: list[int] = []

    async def wait_for_timeout(self, ms):
        self.esperas.append(ms)


def _rodar_espera(monkeypatch, pagina, abre_em):
    async def _ir(page, url, timeout_ms=60_000):
        page.recargas += 1

    async def _lote(page):
        if page.recargas >= page.abre_na_recarga:
            return object(), "Ingresso Antecipado 12h (Quinta-feira)"
        return None, ""
    monkeypatch.setattr(sy, "_ir", _ir)
    monkeypatch.setattr(sy, "_escolher_lote", _lote)
    asyncio.run(sy._esperar_lote_abrir(pagina, "https://x", abre_em))


def test_espera_a_hora_e_recarrega_ate_o_lote_abrir(monkeypatch) -> None:
    pagina = _PaginaLote(abre_na_recarga=3)
    abre_em = datetime.now(BRT) + timedelta(seconds=30)
    _rodar_espera(monkeypatch, pagina, abre_em)
    assert 29_000 <= pagina.esperas[0] <= 30_000, "espera parado até a abertura"
    assert pagina.recargas == 3
    assert all(e == sy.LOTE_RECARGA_S * 1000 for e in pagina.esperas[1:])


def test_lote_que_nunca_abre_desiste_no_prazo(monkeypatch) -> None:
    monkeypatch.setattr(sy, "LOTE_ESPERA_S", 0.0)
    pagina = _PaginaLote(abre_na_recarga=10**6)
    _rodar_espera(monkeypatch, pagina, datetime.now(BRT) - timedelta(minutes=1))
    assert pagina.recargas == 0, "abertura já passou: não espera, só checa"


def test_sem_hora_de_abertura_nao_espera(monkeypatch) -> None:
    """/sympla_testar: roda na hora, sem espera."""
    pagina = _PaginaLote(abre_na_recarga=10**6)
    _rodar_espera(monkeypatch, pagina, None)
    assert pagina.esperas == [] and pagina.recargas == 0


def test_fluxo_espera_o_lote_antes_de_selecionar(monkeypatch) -> None:
    import sys
    import types
    from tests.test_sympla import _FakePageScreenshot, _fake_playwright_module

    ordem: list[str] = []
    page = _FakePageScreenshot(ordem)
    monkeypatch.setitem(sys.modules, "playwright.async_api",
                        _fake_playwright_module(page, ordem))
    evento = sy.EventoCandidato("Orquestra", "https://x/evento/1", None, False)

    async def _nada(*a, **kw):
        return None

    async def _evento(*a, **kw):
        return evento

    async def _esperar(page, url, abre_em):
        ordem.append(f"esperar:{abre_em:%H:%M}")

    async def _selecionar(page, qty):
        ordem.append("selecionar")
        return "lote"
    monkeypatch.setattr(sy, "_login", _nada)
    monkeypatch.setattr(sy, "_localizar_evento", _evento)
    monkeypatch.setattr(sy, "_ir", _nada)
    monkeypatch.setattr(sy, "_esperar_lote_abrir", _esperar)
    monkeypatch.setattr(sy, "_selecionar_e_reservar", _selecionar)
    monkeypatch.setattr(sy, "_preencher_checkout", _nada)

    creds = sy.SymplaCredenciais("x@x.com", "1234x", "X Y", None)
    r = asyncio.run(sy.retirar_ingresso(
        creds, "q", 2, abre_em=QUI.replace(hour=12), poll_timeout_s=0))
    assert r.sucesso
    assert ordem.index("esperar:12:00") < ordem.index("selecionar")
