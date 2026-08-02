"""Vigia de preço que não consegue checar precisa DIZER isso — e o aviso não
pode se perder se o Telegram cair no marco.

Falha marca `last_checked_at` e volta — certo, senão o watch quebrado
re-executa a cada tick (60s) e queima cota do SerpAPI. Mas ia só pro log:
uma vigia com parâmetro inválido (aeroporto que mudou, data que passou)
falhava todo dia, para sempre, e o dono seguia achando que estava vigiado.

Bugs #2 e #6 da auditoria:
- #2: o contador era commitado ANTES do envio; se o Telegram estava fora no
  tick do marco (n==3), o contador já gravava 3, os dias 4–9 não avisavam e o
  próximo aviso só saía no dia 10 — 7 dias de silêncio numa vigia morta.
- #6: o reset do contador rodava ANTES do `best is None`, então vigia que
  devolvia preço vazio todo dia zerava o contador e nunca avisava.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot.services.travels import watches as w


class _Sessao:
    def __init__(self, user=None):
        self.commits = 0
        self._user = user

    async def commit(self):
        self.commits += 1

    async def get(self, _modelo, _id):
        return self._user


class _Bot:
    """Envia com sucesso (registra o texto)."""

    def __init__(self):
        self.enviadas: list[str] = []

    async def send_message(self, chat_id, text, **kw):
        self.enviadas.append(text)


class _BotForaDoAr:
    """Telegram indisponível: send_message sempre estoura."""

    async def send_message(self, *a, **kw):
        raise RuntimeError("telegram fora")


def _watch(falhas=0, avisado=0):
    return SimpleNamespace(
        id=7, user_id=42, kind="flight", summary="GRU→LIS jan/27",
        consecutive_failures=falhas, alerted_at_failures=avisado,
        last_error=None, last_checked_at=None,
    )


def _falhar(watch, motivo="SerpAPI: 429", user=SimpleNamespace(id=42), bot=None):
    bot = bot or _Bot()
    asyncio.run(w._registrar_falha(_Sessao(user), bot, watch, motivo))
    return getattr(bot, "enviadas", [])


# ───────────────────────── cadência do aviso ─────────────────────────

def test_primeiras_falhas_nao_incomodam() -> None:
    for esperado in (1, 2):
        watch = _watch(falhas=esperado - 1)
        assert _falhar(watch) == []
        assert watch.consecutive_failures == esperado


def test_terceira_falha_avisa_e_marca() -> None:
    watch = _watch(falhas=2, avisado=0)
    enviadas = _falhar(watch, "SerpAPI: 400 invalid departure_id")
    assert len(enviadas) == 1
    msg = enviadas[0]
    assert "GRU→LIS jan/27" in msg and "invalid departure_id" in msg
    assert "não conte com alerta" in msg
    assert watch.alerted_at_failures == 3, "marco tem que avançar no envio OK"


def test_nao_repete_todo_dia() -> None:
    """Já avisado no 3: dias 4–9 não repetem."""
    for falhas in (3, 4, 5, 8):
        assert _falhar(_watch(falhas=falhas, avisado=3)) == []


def test_repete_semanalmente_enquanto_durar() -> None:
    assert len(_falhar(_watch(falhas=9, avisado=3))) == 1    # n=10, 10-3=7
    assert len(_falhar(_watch(falhas=16, avisado=10))) == 1  # n=17, 17-10=7


# ───────────────────────── #2: marco não se perde ─────────────────────────

def test_marco_perdido_no_telegram_fora_e_retentado() -> None:
    """Telegram fora no tick do marco (n==3): o contador avança, mas
    `alerted_at_failures` NÃO — então o tick seguinte re-tenta e avisa, em vez
    de 7 dias de silêncio."""
    watch = _watch(falhas=2, avisado=0)
    _falhar(watch, bot=_BotForaDoAr())            # n=3, envio FALHA
    assert watch.consecutive_failures == 3
    assert watch.alerted_at_failures == 0, "marco não pode avançar sem envio"

    enviadas = _falhar(watch, bot=_Bot())         # n=4, re-tenta
    assert len(enviadas) == 1, "o aviso perdido tem que sair no tick seguinte"
    assert watch.alerted_at_failures == 4


# ───────────────────────── robustez ─────────────────────────

def test_motivo_longo_e_truncado() -> None:
    watch = _watch()
    _falhar(watch, "x" * 5000)
    assert len(watch.last_error) <= 300


def test_falha_sem_usuario_nao_estoura() -> None:
    assert _falhar(_watch(falhas=2), user=None) == []


# ───────────────────────── #6: preço vazio conta como falha ──────────────

def test_best_none_conta_como_falha(monkeypatch) -> None:
    """Vigia que devolve preço vazio todo dia tem que ACUMULAR falha (e não
    zerar o contador), senão "não consigo checar" vira silêncio."""
    chamou = {"n": 0, "reset": 0}

    async def _spy_falha(session, bot, watch, motivo):
        chamou["n"] += 1
        chamou["motivo"] = motivo

    monkeypatch.setattr(w, "_registrar_falha", _spy_falha)

    async def _busca_vazia(**kw):
        return {}

    monkeypatch.setattr(w, "extract_best_flight", lambda _raw: None)
    monkeypatch.setattr(w, "extract_price_insights", lambda _raw: None)

    serpapi = SimpleNamespace(search_flights=_busca_vazia)
    watch = _watch(falhas=0)
    watch.params = {"origin_iata": "GRU", "depart_date": "2027-01-10"}
    watch.currency = "BRL"

    asyncio.run(w.check_watch(_Sessao(), serpapi, _Bot(), watch))
    assert chamou["n"] == 1, "best is None deveria ter contado como falha"
    assert "sem preço" in chamou["motivo"]


def test_sucesso_zera_contador_e_marco() -> None:
    """Preço de fato reseta AMBOS os campos (senão pane nova nunca avisaria)."""
    import inspect
    fonte = inspect.getsource(w.check_watch)
    # O reset tem que estar DEPOIS do best-is-None, e incluir o marco.
    assert "watch.consecutive_failures = 0" in fonte
    assert "watch.alerted_at_failures = 0" in fonte
