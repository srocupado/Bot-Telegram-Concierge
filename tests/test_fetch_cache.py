"""Cache single-flight do fetch_mps.

Incidente real (03/08/2026): o /mp_dou_agora checou o dia com sucesso
("nenhuma MP") e, minutos depois, o /proativo_agora re-baixou o MESMO dia,
caiu na recusa de sessão do Inlabs e alarmou "NÃO assuma que não houve MP" —
contradição gratuita e ~100-200MB re-baixados à toa no Orange Pi.

Regras que estes testes travam (erram pro lado de "não perder MP"):
- resultado COMPLETO recente é reusado (TTL curto);
- resultado INCOMPLETO nunca entra no cache (re-checagem vai à fonte);
- FALHA nunca entra no cache nem apaga o registro de última checagem OK;
- chamadas concorrentes da mesma data fazem UM download (single-flight).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest

from bot.services import dou_monitor
from bot.services.dou_monitor import BRT, DouError, MPList

D = date(2026, 8, 3)


def _limpar() -> None:
    dou_monitor._fetch_cache.clear()
    dou_monitor._fetch_locks.clear()
    dou_monitor._ultima_ok.clear()


@pytest.fixture(autouse=True)
def _estado_limpo():
    """O cache é estado de módulo — zera antes/depois pra um teste não
    enxergar resultado semeado por outro."""
    _limpar()
    yield
    _limpar()


def _sync_contando(resultado_por_chamada):
    """_fetch_mps_sync fake que conta chamadas. `resultado_por_chamada` é uma
    função (nº da chamada → MPList | exceção)."""
    chamadas: list[date] = []

    def _sync(d: date):
        chamadas.append(d)
        out = resultado_por_chamada(len(chamadas))
        if isinstance(out, Exception):
            raise out
        return out

    return _sync, chamadas


def test_resultado_completo_e_reusado(monkeypatch) -> None:
    _sync, chamadas = _sync_contando(lambda _n: MPList())
    monkeypatch.setattr(dou_monitor, "_fetch_mps_sync", _sync)

    async def _main():
        r1 = await dou_monitor.fetch_mps(D)
        r2 = await dou_monitor.fetch_mps(D)
        return r1, r2

    r1, r2 = asyncio.run(_main())
    assert len(chamadas) == 1, "segunda chamada devia vir do cache"
    assert r2 is r1


def test_incompleto_nao_entra_no_cache(monkeypatch) -> None:
    """Seção falhou → o próximo interessado tem que ir à FONTE (o cache de um
    resultado parcial daria 'baixa' num dia que pode ter MP na edição Extra)."""
    def _parcial(_n):
        ml = MPList()
        ml.incompleto = True
        ml.secoes_falhas = ("DO1E",)
        return ml

    _sync, chamadas = _sync_contando(_parcial)
    monkeypatch.setattr(dou_monitor, "_fetch_mps_sync", _sync)

    async def _main():
        await dou_monitor.fetch_mps(D)
        await dou_monitor.fetch_mps(D)

    asyncio.run(_main())
    assert len(chamadas) == 2, "resultado incompleto foi cacheado"
    assert dou_monitor.ultima_checagem_ok(D) is None, (
        "checagem incompleta não pode contar como 'checagem OK'"
    )


def test_ttl_expirado_rebusca(monkeypatch) -> None:
    _sync, chamadas = _sync_contando(lambda _n: MPList())
    monkeypatch.setattr(dou_monitor, "_fetch_mps_sync", _sync)

    async def _main():
        await dou_monitor.fetch_mps(D)
        t, r = dou_monitor._fetch_cache[D]
        dou_monitor._fetch_cache[D] = (t - dou_monitor._FETCH_TTL_S - 1, r)
        await dou_monitor.fetch_mps(D)

    asyncio.run(_main())
    assert len(chamadas) == 2, "cache vencido devia re-buscar"


def test_single_flight_concorrentes_um_download(monkeypatch) -> None:
    """Coletor da janela e job da nota pedindo a MESMA data ao mesmo tempo:
    um download só; o segundo espera e recebe o resultado do primeiro."""
    import time as _time

    def _lento(_n):
        _time.sleep(0.05)
        return MPList()

    _sync, chamadas = _sync_contando(_lento)
    monkeypatch.setattr(dou_monitor, "_fetch_mps_sync", _sync)

    async def _main():
        return await asyncio.gather(
            dou_monitor.fetch_mps(D), dou_monitor.fetch_mps(D),
        )

    r1, r2 = asyncio.run(_main())
    assert len(chamadas) == 1, "duas buscas concorrentes baixaram em dobro"
    assert r2 is r1


def test_falha_nao_cacheia_nem_apaga_ultima_ok(monkeypatch) -> None:
    """Erro do Inlabs não pode virar cache (negativo) nem apagar o registro
    da checagem OK anterior — é ele que rebaixa o alarme pra linha informativa."""
    ok_antes = (datetime.now(BRT) - timedelta(minutes=10), 1)
    dou_monitor._ultima_ok[D] = ok_antes

    _sync, chamadas = _sync_contando(
        lambda n: DouError("Inlabs fora") if n == 1 else MPList()
    )
    monkeypatch.setattr(dou_monitor, "_fetch_mps_sync", _sync)

    async def _main():
        with pytest.raises(DouError):
            await dou_monitor.fetch_mps(D)
        assert dou_monitor.ultima_checagem_ok(D) == ok_antes
        await dou_monitor.fetch_mps(D)   # a fonte voltou: busca de verdade

    asyncio.run(_main())
    assert len(chamadas) == 2


def test_sucesso_registra_ultima_checagem_ok(monkeypatch) -> None:
    duas = MPList([{"numero": "1381"}, {"numero": "1382"}])
    _sync, _ = _sync_contando(lambda _n: duas)
    monkeypatch.setattr(dou_monitor, "_fetch_mps_sync", _sync)

    asyncio.run(dou_monitor.fetch_mps(D))
    ok = dou_monitor.ultima_checagem_ok(D)
    assert ok is not None
    quando, n_mps = ok
    assert n_mps == 2
    assert abs((datetime.now(BRT) - quando).total_seconds()) < 60
