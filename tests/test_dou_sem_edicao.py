"""Dia sem Diário publicado (domingo/feriado) tem que ser dito COMO TAL.

Bug do dono: num domingo (02/08/2026, sem DOU) o /proativo_agora mostrava
"📄 Nota técnica (todas as MPs de 02/08) — gerando agora", prometendo uma nota
que nunca viria. A raiz: fetch não distinguia "não houve edição" de "houve DOU
sem MP", e a fila resolvia em silêncio.

Agora fetch marca `sem_edicao`, deliver devolve o `motivo`, e tanto o
/mp_dou_agora quanto a fila respondem com precisão.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from bot.services import dou_monitor, proactive
from bot.services.dou_monitor import BRT, texto_sem_mp


# ───────────────────────── flag sem_edicao no fetch ─────────────────────────

class _Resp:
    def __init__(self, status, content):
        self.status_code = status
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.request = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(self.status_code)


def _listing(nomes):
    linhas = "".join(f'<a href="index.php?p=x&dl={n}">{n}</a> ' for n in nomes)
    return (f"<html><body>Ola<a href='sair.php'>Sair</a><table>"
            f"<th>Nome</th><th>Tamanho</th><th>Modificado</th>{linhas}"
            f"</table></body></html>").encode()


class _Inlabs:
    def __init__(self, nomes):
        self.nomes = nomes
        self.cookies = {"inlabs_session_cookie": "c"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **kw):
        return _Resp(200, b"ok")

    def get(self, url, **kw):
        if "&dl=" not in url:
            return _Resp(200, _listing(self.nomes))
        return _Resp(200, b"PK" + b"\0" * 200)


class _ZipVazio:
    def __init__(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def namelist(self): return []


def _fetch(monkeypatch, nomes, alvo):
    monkeypatch.setattr(dou_monitor.httpx, "Client", lambda **kw: _Inlabs(nomes))
    monkeypatch.setattr(dou_monitor.settings, "inlabs_email", "e@x")
    monkeypatch.setattr(dou_monitor.settings, "inlabs_password",
                        SimpleNamespace(get_secret_value=lambda: "s"))
    monkeypatch.setattr(dou_monitor.zipfile, "ZipFile", _ZipVazio)
    return dou_monitor._fetch_mps_sync(alvo)


def test_listagem_sem_secao1_marca_sem_edicao(monkeypatch) -> None:
    antigo = datetime.now(BRT).date() - timedelta(days=3)   # dia fechado
    out = _fetch(monkeypatch, [], antigo)                    # nada publicado
    assert out.sem_edicao is True
    assert out.provisorio is False   # fechado: não é "ainda pode sair"


def test_listagem_com_do1_nao_marca_sem_edicao(monkeypatch) -> None:
    antigo = datetime.now(BRT).date() - timedelta(days=3)
    out = _fetch(monkeypatch, [f"{antigo.isoformat()}-DO1.zip"], antigo)
    assert out.sem_edicao is False   # houve edição (mesmo com 0 MP no zip vazio)


# ───────────────────── motivo devolvido pelo deliver ─────────────────────

def _mplist(**flags):
    m = dou_monitor.MPList()
    for k, v in flags.items():
        setattr(m, k, v)
    return m


def _deliver_motivo(monkeypatch, mps):
    async def _f(_d):
        return mps
    monkeypatch.setattr(dou_monitor, "fetch_mps", _f)
    return asyncio.run(dou_monitor.deliver_to_user(
        None, None, SimpleNamespace(id=1), date(2026, 8, 2), force=True,
    ))


def test_motivo_sem_edicao(monkeypatch) -> None:
    assert _deliver_motivo(monkeypatch, _mplist(sem_edicao=True)) == (0, [], "sem_edicao")


def test_motivo_sem_mp(monkeypatch) -> None:
    assert _deliver_motivo(monkeypatch, _mplist()) == (0, [], "sem_mp")


def test_motivo_provisorio_sem_nenhuma_edicao(monkeypatch) -> None:
    """Dia em aberto e NADA de Seção 1 ainda: 'provisorio' (pode sair)."""
    out = _deliver_motivo(monkeypatch, _mplist(sem_edicao=True, provisorio=True))
    assert out == (0, [], "provisorio")


def test_motivo_sem_mp_extra_quando_edicao_saiu_mas_dia_aberto(monkeypatch) -> None:
    """Bug do 03/08: a edição NORMAL saiu sem MP (sem_edicao=False) e o dia
    segue aberto (DO1E ainda não saiu → provisorio). NÃO é 'ainda pode sair'
    (o DOU já está no ar) — é 'saiu sem MP, de olho na extra'."""
    out = _deliver_motivo(monkeypatch, _mplist(sem_edicao=False, provisorio=True))
    assert out == (0, [], "sem_mp_extra")


def test_motivo_incompleto_vence(monkeypatch) -> None:
    """Fonte que falhou não deixa afirmar nada — 'incompleto' tem prioridade."""
    out = _deliver_motivo(monkeypatch, _mplist(incompleto=True, provisorio=True))
    assert out == (0, [], "incompleto")


# ───────────────────────── texto de desfecho ─────────────────────────

def test_texto_sem_mp_distingue_os_casos() -> None:
    d = date(2026, 8, 2)
    assert "Não houve edição" in texto_sem_mp("sem_edicao", d)
    assert "ainda pode sair" in texto_sem_mp("provisorio", d)
    assert "sem nenhuma MP nova" in texto_sem_mp("sem_mp", d)
    assert "não consegui confirmar" in texto_sem_mp("incompleto", d).lower()
    assert "02/08/2026" in texto_sem_mp("sem_edicao", d)
    # sem_mp_extra: DOU JÁ saiu (não pode dizer "ainda pode sair") mas segue de
    # olho na extra.
    extra = texto_sem_mp("sem_mp_extra", d)
    assert "Saiu o Diário Oficial" in extra
    assert "edição extra" in extra
    assert "ainda pode sair" not in extra, "o DOU já saiu — não pode dizer isso"


# ──────────────── fila: resolve o dia sem edição com aviso ────────────────

D = date(2026, 8, 2)
KEY = f"{D.isoformat()}:all"


class _Sessao:
    async def get(self, _m, _id):
        return SimpleNamespace(id=_id, is_authorized=True, dou_mp_subscribed=True)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self):
        return None


@pytest.fixture
def reg(monkeypatch):
    estado = {"marcas": set(), "log": [], "enviado": []}

    async def _already(_s, _uid, kind, key):
        return (kind, key) in estado["marcas"]

    async def _mark(_s, _uid, kind, key):
        estado["marcas"].add((kind, key))

    async def _unmark(_s, _uid, kind, key):
        estado["marcas"].discard((kind, key))
        estado["log"].append(("unmark", kind, key))

    async def _send(_bot, _uid, texto, **kw):
        estado["enviado"].append(texto)

    from bot.db import session as db_session
    monkeypatch.setattr(db_session, "SessionLocal", lambda: _Sessao())
    monkeypatch.setattr(proactive, "already_notified", _already)
    monkeypatch.setattr(proactive, "mark_notified", _mark)
    monkeypatch.setattr(proactive, "unmark_notified", _unmark)
    monkeypatch.setattr(proactive, "_send", _send)
    return estado


def _rodar_fila(monkeypatch, motivo):
    async def _deliver(bot, session, user, d, *, force, only_numeros):
        return (0, [], motivo)
    monkeypatch.setattr(dou_monitor, "deliver_to_user", _deliver)
    asyncio.run(proactive._entregar_nota_pendente(None, 42, D, None, KEY))


def test_fila_sem_edicao_tira_da_fila_e_avisa(reg, monkeypatch) -> None:
    _rodar_fila(monkeypatch, "sem_edicao")
    assert ("unmark", "nota_pendente", KEY) in reg["log"], "dia sem DOU tem que sair da fila"
    assert reg["enviado"], "sumiu em silêncio — o dono não fica sabendo"
    assert "Não houve edição" in reg["enviado"][0]


def test_fila_provisorio_mantem_e_cala(reg, monkeypatch) -> None:
    """Dia ainda em aberto: NÃO tira da fila (edição pode sair) e não promete
    nada — silêncio até o dia fechar."""
    _rodar_fila(monkeypatch, "provisorio")
    assert ("unmark", "nota_pendente", KEY) not in reg["log"], "dia aberto não pode sair da fila"
    assert reg["enviado"] == []


def test_fila_sem_mp_extra_mantem_e_cala(reg, monkeypatch) -> None:
    """DOU saiu sem MP mas dia aberto (extra pode vir): mantém na fila, calado —
    igual ao provisório. Só ao FECHAR vira 'sem_mp' e sai da fila."""
    _rodar_fila(monkeypatch, "sem_mp_extra")
    assert ("unmark", "nota_pendente", KEY) not in reg["log"]
    assert reg["enviado"] == []


def test_fila_sem_mp_tira_e_avisa(reg, monkeypatch) -> None:
    _rodar_fila(monkeypatch, "sem_mp")
    assert ("unmark", "nota_pendente", KEY) in reg["log"]
    assert "sem nenhuma MP nova" in reg["enviado"][0]
