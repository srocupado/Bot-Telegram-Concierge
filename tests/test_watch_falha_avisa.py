"""Vigia de preço que não consegue checar precisa DIZER isso.

Falha marca `last_checked_at` e volta — certo, senão o watch quebrado
re-executa a cada tick (60s) e queima cota do SerpAPI. Mas ia só pro log:
uma vigia com parâmetro inválido (aeroporto que mudou, data que passou)
falhava todo dia, para sempre, e o dono seguia achando que estava vigiado.

"Checou e o preço não caiu" e "não consigo checar desde março" eram silêncio
idêntico — o falso negativo que o CLAUDE.md proíbe.
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
    def __init__(self):
        self.enviadas: list[str] = []

    async def send_message(self, chat_id, text, **kw):
        self.enviadas.append(text)


def _watch(falhas=0):
    return SimpleNamespace(
        id=7, user_id=42, kind="flight", summary="GRU→LIS jan/27",
        consecutive_failures=falhas, last_error=None, last_checked_at=None,
    )


def _falhar(watch, motivo="SerpAPI: 429", user=SimpleNamespace(id=42)):
    bot = _Bot()
    asyncio.run(w._registrar_falha(_Sessao(user), bot, watch, motivo))
    return bot.enviadas


def test_primeiras_falhas_nao_incomodam() -> None:
    """Pane passageira do SerpAPI não vira mensagem."""
    for esperado in (1, 2):
        watch = _watch(falhas=esperado - 1)
        assert _falhar(watch) == []
        assert watch.consecutive_failures == esperado


def test_terceira_falha_avisa_com_motivo_e_saida() -> None:
    watch = _watch(falhas=2)
    enviadas = _falhar(watch, "SerpAPI: 400 invalid departure_id")

    assert len(enviadas) == 1
    msg = enviadas[0]
    assert "GRU→LIS jan/27" in msg, "sem identificar a vigia, o aviso é inútil"
    assert "invalid departure_id" in msg, "o motivo é o que permite consertar"
    assert "não conte com alerta" in msg, (
        "precisa deixar claro que a vigia NÃO está vigiando — é o ponto todo"
    )


def test_nao_repete_todo_dia() -> None:
    """Aviso diário viraria ruído e treinaria o dono a ignorar."""
    for falhas in (3, 4, 5, 8):
        assert _falhar(_watch(falhas=falhas)) == [], f"repetiu na falha {falhas + 1}"


def test_repete_semanalmente_enquanto_durar() -> None:
    """Uma vez por semana o lembrete volta: pane de um mês não pode virar
    silêncio de novo depois do primeiro aviso."""
    assert len(_falhar(_watch(falhas=9))) == 1     # 10ª falha = 3 + 7
    assert len(_falhar(_watch(falhas=16))) == 1    # 17ª = 3 + 14


def test_motivo_longo_e_truncado() -> None:
    """A coluna tem 300 chars; texto gigante do provider não pode estourar."""
    watch = _watch()
    _falhar(watch, "x" * 5000)
    assert len(watch.last_error) <= 300


def test_sucesso_zera_o_contador() -> None:
    """Sem zerar, uma pane nova nunca mais avisaria (o contador já teria
    passado do limiar) — e o histórico velho contaminaria o novo."""
    import inspect
    fonte = inspect.getsource(w.check_watch)
    assert "watch.consecutive_failures = 0" in fonte
    assert "watch.last_error = None" in fonte


def test_falha_sem_usuario_nao_estoura() -> None:
    """Usuário removido não pode derrubar o tick de viagens."""
    assert _falhar(_watch(falhas=2), user=None) == []
