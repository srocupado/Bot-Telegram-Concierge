"""Marca d'água do DOU: dias que o bot NUNCA olhou.

A pendência retroativa só nascia de uma tentativa que falhou. Dia em que o bot
sequer rodou — container fora, queda de luz no Orange Pi, deploy longo — não
deixava rastro nenhum: na volta ele checava hoje (+ontem no briefing) e o
resto sumia em silêncio. `users.dou_ultimo_dia_ok` fecha o buraco.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from bot.services import proactive


class _FakeSession:
    def __init__(self):
        self.commits = 0

    async def scalars(self, _stmt):
        return []

    async def commit(self):
        self.commits += 1


def _prep(monkeypatch, marcadas: list[str], ja: set[str] | None = None):
    ja = ja or set()

    async def _already(_s, _uid, kind, key):
        return f"{kind}:{key}" in ja

    async def _mark(_s, _uid, kind, key):
        marcadas.append(f"{kind}:{key}")

    monkeypatch.setattr(proactive, "already_notified", _already)
    monkeypatch.setattr(proactive, "mark_notified", _mark)


def _rodar(user, hoje, session=None):
    return asyncio.run(
        proactive._cobrir_lacuna(session or _FakeSession(), user, hoje)
    )


def _user(marca):
    return SimpleNamespace(id=5, dou_mp_subscribed=True, dou_ultimo_dia_ok=marca)


HOJE = datetime.now(proactive.BRT).date()


def test_primeira_vez_nao_varre_o_passado(monkeypatch) -> None:
    """Marca nula = monitor recém-ativado. Enfileirar 14 dias de uma vez
    custaria um fetch de ~6s cada e inundaria o dono de aviso sobre dia que
    ele nunca esperou que fosse checado."""
    marcadas: list[str] = []
    _prep(monkeypatch, marcadas)
    user = _user(None)

    assert _rodar(user, HOJE) == []
    assert marcadas == []
    assert user.dou_ultimo_dia_ok == HOJE - timedelta(days=1)


def test_sem_lacuna_nao_faz_nada(monkeypatch) -> None:
    """Rodando todo dia, a marca é ontem e não há o que enfileirar."""
    marcadas: list[str] = []
    _prep(monkeypatch, marcadas)

    assert _rodar(_user(HOJE - timedelta(days=1)), HOJE) == []
    assert marcadas == []


def test_fim_de_semana_fora_do_ar_vira_pendencia(monkeypatch) -> None:
    """Orange Pi cai na sexta, volta na segunda: sábado e domingo não podem
    sumir — edição extra sai em fim de semana também."""
    marcadas: list[str] = []
    _prep(monkeypatch, marcadas)
    user = _user(HOJE - timedelta(days=4))

    facts = _rodar(user, HOJE)

    esperado = [
        f"mp_pendente:{(HOJE - timedelta(days=i)).isoformat()}"
        for i in (3, 2, 1)
    ]
    assert marcadas == esperado, "dias fora do ar não entraram na fila"
    assert facts == [], "lacuna curta não deve alarmar — a retroativa resolve"
    assert user.dou_ultimo_dia_ok == HOJE - timedelta(days=1)


def test_hoje_nunca_entra_na_lacuna(monkeypatch) -> None:
    """Hoje está sendo checado NESTA janela; enfileirar seria fetch duplicado."""
    marcadas: list[str] = []
    _prep(monkeypatch, marcadas)

    _rodar(_user(HOJE - timedelta(days=2)), HOJE)

    assert marcadas == [f"mp_pendente:{(HOJE - timedelta(days=1)).isoformat()}"]


def test_nao_reenfileira_dia_ja_pendente(monkeypatch) -> None:
    """A pendência pode ter vindo de uma falha anterior."""
    ontem = HOJE - timedelta(days=1)
    marcadas: list[str] = []
    _prep(monkeypatch, marcadas, ja={f"mp_pendente:{ontem.isoformat()}"})

    _rodar(_user(HOJE - timedelta(days=2)), HOJE)

    assert marcadas == []


def test_ausencia_longa_avisa_o_que_ficou_de_fora(monkeypatch) -> None:
    """Passou da janela da retroativa: o dia não é recuperável sozinho, então
    tem que ser dito. Sumir calado aqui é o pior caso — é justamente quando o
    bot ficou fora tempo demais e mais provavelmente perdeu MP."""
    marcadas: list[str] = []
    _prep(monkeypatch, marcadas)
    fora = 20
    user = _user(HOJE - timedelta(days=fora + 1))

    facts = _rodar(user, HOJE)

    assert [f.kind for f in facts] == ["mp_lacuna"]
    texto = facts[0].text
    assert "Fiquei sem checar o DOU" in texto
    assert "/mp_dou_agora" in texto, "aviso sem saída de ação não serve"
    # Os últimos 14 dias entram na fila; os mais velhos viram o aviso.
    assert len(marcadas) == proactive._MP_RETRO_EXPIRA_DIAS
    velhos = fora - proactive._MP_RETRO_EXPIRA_DIAS
    assert f"({velhos} dia(s))" in texto


def test_aviso_de_lacuna_longa_nao_repete(monkeypatch) -> None:
    """Dedup por período: sem isso o alarme voltaria em toda janela."""
    fora = 20
    primeiro = HOJE - timedelta(days=fora)
    ultimo = HOJE - timedelta(days=proactive._MP_RETRO_EXPIRA_DIAS + 1)
    chave = f"mp_lacuna:lacuna:{primeiro.isoformat()}:{ultimo.isoformat()}"
    marcadas: list[str] = []
    _prep(monkeypatch, marcadas, ja={chave})

    facts = _rodar(_user(HOJE - timedelta(days=fora + 1)), HOJE)

    assert facts == []
