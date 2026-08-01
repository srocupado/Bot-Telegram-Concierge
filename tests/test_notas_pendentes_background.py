"""Fila de notas técnicas do DOU re-tentada FORA do tick proativo.

Regressão que originou a mudança: `_processar_notas_pendentes` chamava
`deliver_to_user` inline, e o pipeline da nota mede ~71s medidos no Orange Pi
(6s de Inlabs + 65s de pesquisa/redação). O tick inteiro ficava parado nesse
tempo, segurando a sessão do banco — lembrete que vencesse no meio saía
atrasado. O contrato aqui é: o tick só AGENDA, e a mesma nota nunca roda duas
vezes em paralelo (tick × botão manual).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from bot.services import jobs, proactive
from bot.services.dou_monitor import chave_job_nota


def _hoje():
    return datetime.now(proactive.BRT).date()


class _FakeSession:
    """Só o que `_processar_notas_pendentes` usa: ler as linhas da fila."""

    def __init__(self, rows):
        self._rows = rows

    async def scalars(self, _stmt):
        return list(self._rows)


def _fila(*keys):
    return [SimpleNamespace(key=k) for k in keys]


def _user(uid: int):
    return SimpleNamespace(id=uid, is_authorized=True, dou_mp_subscribed=True)


def test_tick_nao_espera_a_geracao_da_nota(monkeypatch) -> None:
    """O tick devolve na hora mesmo com a entrega levando ~1 minuto."""
    d = _hoje()
    entregues: list[str] = []

    async def _lenta(bot, user_id, dia, numeros, key):
        await asyncio.sleep(0.3)
        entregues.append(key)

    monkeypatch.setattr(proactive, "_entregar_nota_pendente", _lenta)

    async def main() -> float:
        session = _FakeSession(_fila(f"{d.isoformat()}:1381"))
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await proactive._processar_notas_pendentes(None, session, _user(9001))
        gasto = loop.time() - t0
        assert entregues == [], "o tick esperou a nota terminar"
        await asyncio.sleep(0.4)
        assert entregues == [f"{d.isoformat()}:1381"], "a nota não foi entregue"
        return gasto

    assert asyncio.run(main()) < 0.05


def test_nao_duplica_nota_que_o_dono_ja_pediu_no_botao(monkeypatch) -> None:
    """Botão manual e tick compartilham a chave: um pipeline só."""
    d = _hoje()
    entregues: list[str] = []

    async def _entrega(bot, user_id, dia, numeros, key):
        entregues.append(key)

    monkeypatch.setattr(proactive, "_entregar_nota_pendente", _entrega)

    async def main() -> None:
        async def _manual() -> None:          # o job do /mp_dou_agora, em curso
            await asyncio.sleep(0.2)

        assert jobs.spawn(chave_job_nota(9002, d), lambda: _manual()) is True
        await proactive._processar_notas_pendentes(
            None, _FakeSession(_fila(f"{d.isoformat()}:all")), _user(9002),
        )
        await asyncio.sleep(0.3)
        assert entregues == [], "o tick disparou a nota que o dono já pediu"

    asyncio.run(main())


def test_fila_anda_mesmo_com_uma_nota_em_andamento(monkeypatch) -> None:
    """Data já em andamento não gasta o teto da janela: a próxima da fila anda.

    Sem o filtro, `sorted(fila)[:1]` escolheria sempre a data mais antiga —
    justo a que está rodando — e a fila ficaria parada janela após janela.
    """
    hoje = _hoje()
    antiga, nova = hoje - timedelta(days=2), hoje - timedelta(days=1)
    entregues: list[str] = []

    async def _entrega(bot, user_id, dia, numeros, key):
        entregues.append(key)

    monkeypatch.setattr(proactive, "_entregar_nota_pendente", _entrega)

    async def main() -> None:
        async def _em_curso() -> None:
            await asyncio.sleep(0.2)

        assert jobs.spawn(chave_job_nota(9003, antiga), lambda: _em_curso()) is True
        await proactive._processar_notas_pendentes(
            None,
            _FakeSession(_fila(f"{antiga.isoformat()}:all", f"{nova.isoformat()}:1400")),
            _user(9003),
        )
        await asyncio.sleep(0.3)
        assert entregues == [f"{nova.isoformat()}:1400"]

    asyncio.run(main())


def test_chave_do_job_e_a_mesma_do_comando_manual() -> None:
    """O dedup acima depende disso; se alguém mudar um lado, quebra aqui."""
    from bot.handlers.dou_mp import _chave_nota

    d = _hoje()
    assert _chave_nota(7, d) == chave_job_nota(7, d)
