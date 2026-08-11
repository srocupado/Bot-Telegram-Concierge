"""Batimento da checagem do DOU: confirmação POSITIVA duas vezes por dia.

Pedido do dono (03/08/2026): "na janela de briefing eu quero saber se foi
checado com sucesso, silêncio não é legal" — silêncio é indistinguível de
"não checou". O batimento fala:

- na ABERTURA (briefing/força): "DOU de hoje: {ainda sem edição publicada |
  sem MP até o momento} — re-checo às 13h e às 19h" (horas da config,
  nunca fixas no texto);
- no FECHAMENTO (última janela do dia): "sem MP na checagem das 19h — extra
  tardia (se houver) chega no briefing de amanhã" (o dia só fecha às 6h,
  não se afirma veredito);
- nas janelas do meio: NADA (apurado o estado, repetir é ruído).

E só afirma o que foi APURADO: fetch completo e 0 MP no dia. Com MP, as
linhas de MP são a evidência; com falha/incompleto, quem fala é o aviso de
2 estágios — o batimento não pode virar falso "checado OK".
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from bot.services import dou_monitor, proactive
from bot.services.dou_monitor import MPList


class _FakeSession:
    def __init__(self, *respostas):
        self._respostas = list(respostas)

    async def scalars(self, _stmt):
        return list(self._respostas.pop(0)) if self._respostas else []

    async def commit(self):
        return None


@pytest.fixture(autouse=True)
def _ultima_ok_limpa():
    dou_monitor._ultima_ok.clear()
    yield
    dou_monitor._ultima_ok.clear()


def _facts(monkeypatch, resultado, *, conferir=True, restantes=(13, 19)):
    """Roda collect_mp de HOJE com o fetch devolvendo `resultado` (MPList ou
    exceção) e as janelas restantes controladas."""
    hoje = datetime.now(proactive.BRT).date()

    async def _fetch(_d):
        if isinstance(resultado, Exception):
            raise resultado
        return resultado

    async def _false(*a, **kw):
        return False

    async def _none(*a, **kw):
        return None

    async def _sem_conferencia(*a, **kw):
        return []

    monkeypatch.setattr(dou_monitor, "fetch_mps", _fetch)
    monkeypatch.setattr(proactive, "already_notified", _false)
    monkeypatch.setattr(proactive, "mark_notified", _none)
    monkeypatch.setattr(proactive, "unmark_notified", _none)
    monkeypatch.setattr(proactive, "_conferir_camara", _sem_conferencia)
    # Estes cenários exercitam o CAMINHO DO INLABS: portal desligado, senão
    # o portal-primeiro responderia com o site real e o mock nem rodava.
    monkeypatch.setattr(proactive.settings, "dou_portal_fallback", False)
    monkeypatch.setattr(proactive, "_janelas_restantes", lambda *_a: list(restantes))

    user = SimpleNamespace(id=7, dou_mp_subscribed=True,
                           dou_ultimo_dia_ok=hoje - timedelta(days=1))
    session = _FakeSession([], [], [])
    return asyncio.run(proactive.collect_mp(session, user, [hoje], conferir=conferir))


def _batimentos(facts):
    return [f for f in facts if f.kind == "mp_checagem"]


def test_abertura_sem_edicao_publicada(monkeypatch) -> None:
    """Às 7h o DOU de hoje costuma ainda não estar no Inlabs: o batimento diz
    ISSO — afirmar 'sem MP' aí seria mais do que se sabe."""
    ml = MPList()
    ml.sem_edicao = True
    ml.provisorio = True
    bat = _batimentos(_facts(monkeypatch, ml))
    assert len(bat) == 1
    assert bat[0].text == ("📄 DOU de hoje: ainda sem edição publicada — "
                           "re-checo às 13h05 e às 19h05.")


def test_abertura_edicao_sem_mp(monkeypatch) -> None:
    bat = _batimentos(_facts(monkeypatch, MPList()))
    assert len(bat) == 1
    assert bat[0].text == ("📄 DOU de hoje: sem MP até o momento — "
                           "re-checo às 13h05 e às 19h05.")
    assert bat[0].key.endswith(":abre")


def test_fechamento_na_ultima_janela(monkeypatch) -> None:
    """19h (sem janelas restantes): a palavra final do dia, com a ressalva —
    o dia só fecha às 6h e a extra tardia chega no briefing."""
    facts = _facts(monkeypatch, MPList(), conferir=False, restantes=())
    bat = _batimentos(facts)
    assert len(bat) == 1
    assert "sem MP na checagem das" in bat[0].text
    assert "extra tardia (se houver) chega no briefing de amanhã" in bat[0].text
    assert bat[0].key.endswith(":fecha")


def test_fechamento_domingo_sem_edicao(monkeypatch) -> None:
    """Dia sem Diário nenhum (domingo/feriado): o fechamento não fala em
    'extra tardia' de uma edição que nunca existiu."""
    ml = MPList()
    ml.sem_edicao = True
    ml.provisorio = True
    bat = _batimentos(_facts(monkeypatch, ml, conferir=False, restantes=()))
    assert len(bat) == 1
    assert "sem edição publicada até as" in bat[0].text
    assert "chega no briefing de amanhã" in bat[0].text


def test_janela_do_meio_fica_em_silencio(monkeypatch) -> None:
    """13h (não é briefing, ainda há janela): nada — repetir é ruído."""
    facts = _facts(monkeypatch, MPList(), conferir=False, restantes=(19,))
    assert _batimentos(facts) == []


def test_falha_nao_vira_batimento(monkeypatch) -> None:
    """Fetch falhou: NÃO pode sair 'checado OK' — quem fala é o mp_fail."""
    facts = _facts(monkeypatch, dou_monitor.DouError("Inlabs fora"))
    assert _batimentos(facts) == []
    assert any(f.kind == "mp_fail" for f in facts)


def test_incompleto_nao_vira_batimento(monkeypatch) -> None:
    """Seção falhou: afirmar 'checado, sem MP' esconderia a MP que pode
    estar na seção que faltou."""
    ml = MPList()
    ml.incompleto = True
    ml.secoes_falhas = ("DO1E",)
    facts = _facts(monkeypatch, ml)
    assert _batimentos(facts) == []


def test_com_mp_no_dia_nao_ha_batimento(monkeypatch) -> None:
    """MP publicada: a própria linha da MP é a evidência da checagem —
    'sem MP até o momento' junto dela seria contradição."""
    ml = MPList([{"numero": "1381", "ano": 2026, "ementa": "Dispõe sobre teste."}])
    facts = _facts(monkeypatch, ml)
    assert _batimentos(facts) == []
    assert any(f.kind == "mp" for f in facts)
