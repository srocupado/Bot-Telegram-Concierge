"""Checagem RETROATIVA do DOU — o mecanismo que evita perder MP quando o
Inlabs cai.

Regressão real (viva em produção desde 2026-07-13, achada em 2026-08-01): o
laço da retroativa fazia `facts += await _colher(d)`, mas `_colher` passou a
devolver `(facts, completo)` na revisão de 31/07. O `+=` estendia a lista com
a TUPLA — um `list` e um `bool` entravam em `facts` — e o laço seguinte
estourava `AttributeError: 'list' object has no attribute 'kind'`.

Efeito: a exceção subia até o scheduler, que só loga por usuário. A janela
proativa INTEIRA morria — MPs do dia, lembretes, briefing. E como a pendência
só recebe baixa após o envio, ela sobrevivia e a janela seguinte quebrava
igual, por até 14 dias. O gatilho era o Inlabs VOLTAR: enquanto ele estava
fora, a retroativa falhava no `except` e não havia crash.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from bot.services import dou_monitor, proactive
from bot.services.dou_monitor import MPList


class _FakeSession:
    """Devolve uma fila de resultados, um por chamada de scalars()."""

    def __init__(self, *respostas):
        self._respostas = list(respostas)

    async def scalars(self, _stmt):
        return list(self._respostas.pop(0)) if self._respostas else []

    async def commit(self):
        return None


def _sem_dedup(monkeypatch) -> None:
    async def _false(*a, **kw):
        return False

    async def _none(*a, **kw):
        return None

    monkeypatch.setattr(proactive, "already_notified", _false)
    monkeypatch.setattr(proactive, "mark_notified", _none)
    monkeypatch.setattr(proactive, "unmark_notified", _none)


def _cenario(monkeypatch, resultado):
    """Ontem pendente (Inlabs caiu) + hoje OK. `resultado` é o que o Inlabs
    devolve na re-checagem de ontem — o momento em que ele VOLTA."""
    hoje = datetime.now(proactive.BRT).date()
    ontem = hoje - timedelta(days=1)

    async def _fetch(d):
        return resultado if d == ontem else MPList()

    monkeypatch.setattr(dou_monitor, "fetch_mps", _fetch)
    _sem_dedup(monkeypatch)
    # 3 chamadas de scalars: mp_lacuna_pend (avisos de lacuna pendentes),
    # mp_pendente (retroativa) e a fila de notas.
    session = _FakeSession([], [SimpleNamespace(key=ontem.isoformat())], [])
    user = SimpleNamespace(id=4242, dou_mp_subscribed=True, is_authorized=True,
                            dou_ultimo_dia_ok=None)
    return session, user, hoje, ontem


def test_retroativa_bem_sucedida_nao_derruba_a_janela(monkeypatch) -> None:
    """O Inlabs volta e a re-checagem entrega — sem estourar a janela."""
    session, user, hoje, ontem = _cenario(monkeypatch, MPList())

    facts = asyncio.run(proactive.collect_mp(session, user, [hoje]))

    assert all(isinstance(f, proactive.ProactiveFact) for f in facts), (
        "facts contaminado por não-fatos (a tupla de _colher vazou pra lista)"
    )
    assert [f.kind for f in facts] == ["mp_retro"]
    assert ontem.strftime("%d/%m") in facts[0].text


def test_retroativa_incompleta_nao_da_baixa_no_dia(monkeypatch) -> None:
    """Seção faltando na re-checagem = dia continua pendente.

    Dar baixa aqui perderia em silêncio a MP publicada só na edição Extra —
    exatamente o falso negativo que a retroativa existe pra evitar.
    """
    parcial = MPList()
    parcial.incompleto = True
    parcial.secoes_falhas = ("DO1E",)
    session, user, hoje, _ = _cenario(monkeypatch, parcial)

    facts = asyncio.run(proactive.collect_mp(session, user, [hoje]))

    assert [f.kind for f in facts] == [], (
        "dia incompleto recebeu baixa (✅ retroativa concluída) — a pendência "
        "some e a edição Extra nunca mais é checada"
    )
