"""Portal fora ≠ portal inconclusivo (bug real de 25/10/2026).

Portal com 502 e Inlabs com "_ssl.c:993: The handshake operation timed out";
a mensagem dizia "O portal público também não foi conclusivo agora" — como
se a fonte primária tivesse respondido. Medido no mesmo horário: a busca do
portal devolvia 502 Bad Gateway.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bot.handlers import dou_mp
from bot.services import proactive
from bot.services.dou_monitor import DouError


class _Bot:
    def __init__(self):
        self.textos: list[str] = []

    async def send_message(self, _uid, texto, **kw):
        self.textos.append(texto)


def _rodar(monkeypatch, portal, inlabs=True) -> str:
    from bot.db import session as dbs

    class _SL:
        async def __aenter__(self):
            return SimpleNamespace(get=_get)

        async def __aexit__(self, *a):
            return False

    async def _get(_m, _uid):
        return SimpleNamespace(id=1, is_authorized=True)

    async def _deliver(*a, **kw):
        raise DouError("falha ao autenticar no Inlabs: _ssl.c:993: The handshake operation timed out")

    async def _nao(*a, **kw):
        return False

    async def _nada(*a, **kw):
        return None
    monkeypatch.setattr(dbs, "SessionLocal", lambda: _SL())
    monkeypatch.setattr(dou_mp, "_checar_via_portal", portal)
    monkeypatch.setattr(dou_mp, "deliver_to_user", _deliver)
    monkeypatch.setattr(dou_mp, "inlabs_configurado", lambda: inlabs)
    monkeypatch.setattr(dou_mp.settings, "dou_portal_fallback", True)
    monkeypatch.setattr(proactive, "already_notified", _nao)
    monkeypatch.setattr(proactive, "mark_notified", _nada)
    bot = _Bot()
    from datetime import date
    asyncio.run(dou_mp._rodar_nota(bot, 1, date(2026, 10, 25), None))
    return bot.textos[-1]


def test_portal_com_502_e_dito_fora_do_ar(monkeypatch) -> None:
    async def _portal(*a, **kw):
        raise RuntimeError(
            "busca no portal falhou (3x): HTTPStatusError: Server error '502 "
            "Bad Gateway' for url 'https://www.in.gov.br/consulta/-/buscar/dou?q=x'\n"
            "For more information check: https://developer.mozilla.org/")
    txt = _rodar(monkeypatch, _portal)
    assert "fora do ar" in txt and "502 Bad Gateway" in txt
    assert "não foi conclusivo" not in txt
    assert "https://www.in.gov.br" not in txt, "URL enorme do httpx fora da mensagem"
    assert "Inlabs também falhou" in txt and "Deixei na fila" in txt


def test_portal_que_respondeu_sem_concluir_segue_inconclusivo(monkeypatch) -> None:
    async def _portal(*a, **kw):
        return False
    txt = _rodar(monkeypatch, _portal)
    assert "não foi conclusivo" in txt and "fora do ar" not in txt


def test_sem_inlabs_configurado_nao_cita_o_inlabs(monkeypatch) -> None:
    async def _portal(*a, **kw):
        raise RuntimeError("busca no portal falhou (3x): ReadTimeout")
    txt = _rodar(monkeypatch, _portal, inlabs=False)
    assert "Inlabs" not in txt and "fora do ar" in txt
