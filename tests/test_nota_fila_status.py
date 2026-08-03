"""Linha de status da fila de notas: diz o observado, não a causa suposta.

O texto era fixo — "Inlabs instável; tento gerar a cada janela" — para toda
entrada. O motivo da falha que criou a entrada não fica registrado em lugar
nenhum, então a frase era chute. Em 01/08/2026 ela apareceu com o Inlabs de
pé, para uma nota que estava sendo gerada NAQUELA MESMA rodada (log:
`job nota:…:2026-07-31 iniciado` 14:07:52 → `entregue` 14:09:00).

Agora o texto sai do que o bot consegue observar: job vivo para aquela data,
ou posição na fila contra o teto da janela.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from bot.services import jobs, proactive
from bot.services.dou_monitor import chave_job_nota

USER_ID = 4321
D1 = date(2026, 7, 30)
D2 = date(2026, 7, 31)
D3 = date(2026, 8, 1)


class _FakeSession:
    def __init__(self, *respostas):
        self._respostas = list(respostas)

    async def scalars(self, _stmt):
        return list(self._respostas.pop(0)) if self._respostas else []

    async def commit(self):
        return None


@pytest.fixture(autouse=True)
def _sem_jobs():
    for k in list(jobs.jobs_ativos()):
        jobs._jobs.pop(k, None)
    yield
    for k in list(jobs.jobs_ativos()):
        jobs._jobs.pop(k, None)


def _linhas(monkeypatch, datas, com_job=None):
    """Roda collect_mp com a fila dada e devolve as linhas de nota_fila."""
    async def _false(*a, **kw):
        return False

    async def _none(*a, **kw):
        return None

    async def _fetch(_d):
        from bot.services.dou_monitor import MPList
        return MPList()

    from bot.services import dou_monitor
    monkeypatch.setattr(dou_monitor, "fetch_mps", _fetch)
    monkeypatch.setattr(proactive, "already_notified", _false)
    monkeypatch.setattr(proactive, "mark_notified", _none)
    monkeypatch.setattr(proactive, "unmark_notified", _none)
    # Não dispara entrega de verdade: o alvo aqui é só o TEXTO.
    monkeypatch.setattr(proactive.jobs, "spawn", lambda *a, **kw: True)

    rows = [SimpleNamespace(key=f"{d.isoformat()}:all") for d in datas]
    user = SimpleNamespace(id=USER_ID, dou_mp_subscribed=True,
                           dou_ultimo_dia_ok=date.today() - timedelta(days=1))

    async def _main():
        # scalars é chamado 2x: pendências da retroativa e, depois, a fila
        # de notas. Ordem errada aqui devolvia lista vazia e os testes
        # falhavam sem relação com o que testam.
        session = _FakeSession([], rows)
        facts = await proactive.collect_mp(session, user, [])
        return [f.text for f in facts if f.kind == "nota_fila"]

    return asyncio.run(_main())


def test_job_vivo_checagem_nao_promete_prazo(monkeypatch) -> None:
    """O caso que expôs a mentira: o job estava rodando e a linha culpava o
    Inlabs. Entrada 'all' (pedido que não confirmou MP) é CHECAGEM em andamento
    — não 'gerando a nota das MPs', que prometeria MP que pode não existir (num
    domingo sem DOU). E não pode prometer PRAZO ("em minutos") — o desfecho pode
    só vir quando o dia fechar; o silêncio no meio parecia bug pro dono. Só com
    os números conhecidos é que vira 'gerando agora, chega em alguns minutos'
    (ver test_linha_vira_gerando_agora_apos_o_disparo, que usa chave :1381)."""
    # `spawn` exige loop rodando, então marca o job direto no registro — é o
    # mesmo estado que `job_em_andamento` consulta.
    jobs._jobs[chave_job_nota(USER_ID, D2)] = _TarefaViva()

    linhas = _linhas(monkeypatch, [D2])
    assert linhas[0].startswith("📄 DOU de "), "prefixo neutro (sem 'Checagem…checando')"
    assert "checando agora" in linhas[0]
    assert "sem precisar acompanhar" in linhas[0]
    assert "em minutos" not in linhas[0], "checagem não pode prometer prazo"
    assert "Inlabs" not in linhas[0]


class _TarefaViva:
    """Task que nunca termina, pro job_em_andamento enxergar."""

    def done(self):
        return False


def test_fora_do_teto_diz_aguardando_a_vez(monkeypatch) -> None:
    """Com teto 2, a terceira entrada está esperando a vez — e isso é
    verdade verificável, não suposição sobre o Inlabs."""
    linhas = _linhas(monkeypatch, [D1, D2, D3])
    assert len(linhas) == 3
    assert "aguardando a vez" in linhas[2]
    assert "aguardando a vez" not in linhas[0]
    assert "aguardando a vez" not in linhas[1]


def test_dentro_do_teto_promete_a_proxima_janela(monkeypatch) -> None:
    linhas = _linhas(monkeypatch, [D2])
    assert "próxima janela" in linhas[0]
    assert "Inlabs" not in linhas[0], "voltou a alegar causa não apurada"


def test_nenhuma_linha_alega_causa(monkeypatch) -> None:
    """Regra do projeto: não afirmar o que não foi apurado."""
    for linha in _linhas(monkeypatch, [D1, D2, D3]):
        assert "instável" not in linha.lower()
        assert "envio assim que sair" in linha or "chega em alguns minutos" in linha


def test_ordem_e_por_data(monkeypatch) -> None:
    """A vez na fila segue a data (mais antiga primeiro) — a mesma ordem que
    o disparo usa, senão a linha diria uma coisa e o bot faria outra."""
    linhas = _linhas(monkeypatch, [D3, D1, D2])
    assert "30/07" in linhas[0] and "31/07" in linhas[1] and "01/08" in linhas[2]


# ─────────────── correção pós-disparo (sem corrida) ───────────────

def test_linha_vira_gerando_agora_apos_o_disparo() -> None:
    """A linha nasce em collect_mp, ANTES do disparo, então diria "tento na
    próxima janela" pra uma nota que começou a ser gerada na MESMA execução.
    A correção acontece depois, com as datas que o disparo devolveu."""
    fatos = [
        proactive.ProactiveFact("mp", "nota_fila", f"{D2.isoformat()}:1381",
                                "📄 Nota técnica (MP 1381 de 31/07) — tento na próxima janela."),
        proactive.ProactiveFact("mp", "nota_fila", f"{D3.isoformat()}:all",
                                "📄 Nota técnica (todas as MPs de 01/08) — aguardando a vez."),
        proactive.ProactiveFact("venc", "card_due", "x", "💳 Fatura vence."),
    ]

    proactive._marcar_geradas_agora(fatos, [D2])

    assert "gerando agora" in fatos[0].text
    assert "MP 1381" in fatos[0].text, "o alvo tem que sobreviver à reescrita"
    assert "31/07" in fatos[0].text
    assert "gerando agora" not in fatos[1].text, "só a data disparada muda"
    assert fatos[2].text == "💳 Fatura vence.", "não pode tocar em outros fatos"


def test_sem_disparo_nada_muda() -> None:
    original = "📄 Nota técnica (MP 1381 de 31/07) — tento na próxima janela."
    fatos = [proactive.ProactiveFact("mp", "nota_fila", f"{D2.isoformat()}:1381", original)]
    proactive._marcar_geradas_agora(fatos, [])
    assert fatos[0].text == original


def test_texto_e_montado_num_lugar_so() -> None:
    """Montagem e reescrita compartilham `_texto_fila` — texto duplicado nos
    dois lugares sairia do ar um dia sem ninguém notar."""
    linha = proactive._texto_fila(f"{D2.isoformat()}:1381,1382", "qualquer estado")
    assert linha == "📄 Nota técnica (MP 1381, 1382 de 31/07) — qualquer estado."


# ─────────────── estado APURADO: "sem MP até o momento" ───────────────
#
# Pedido do dono (03/08/2026): com o dia já checado COMPLETO minutos antes
# (cache/última checagem OK), a linha "checando agora; aviso o resultado"
# descrevia PROCESSO onde dava pra descrever ESTADO. Agora:
# - há janela ainda hoje → "sem MP até o momento; re-checo às Xh", com
#   "(edição extra)" só quando a próxima janela é ≥18h (regra do dono: a
#   extra, quando há, sai depois das 18h);
# - última janela do dia → "sem MP na checagem das Xh" + ressalva da extra
#   tardia (o dia só fecha às 6h — não se afirma "sem MP" seco);
# - sem checagem OK / MP encontrada / dia fechado / entrada com números →
#   None (mantém os estados de processo — não se afirma o que não se apurou).

from datetime import datetime
from zoneinfo import ZoneInfo

from bot.services import dou_monitor

_BRT = ZoneInfo("America/Sao_Paulo")
_D = date(2026, 8, 3)
_KEY_ALL = f"{_D.isoformat()}:all"


@pytest.fixture(autouse=True)
def _ultima_ok_limpa():
    dou_monitor._ultima_ok.clear()
    yield
    dou_monitor._ultima_ok.clear()


def _as_13h05() -> datetime:
    return datetime(2026, 8, 3, 13, 5, tzinfo=_BRT)


def test_apurado_meio_do_dia_aponta_proxima_janela_com_extra() -> None:
    dou_monitor._ultima_ok[_D] = (_as_13h05(), 0)
    estado = proactive._estado_checado_sem_mp(_KEY_ALL, _as_13h05())
    assert estado == "sem MP até o momento; re-checo às 19h (edição extra)"


def test_apurado_manha_nao_promete_extra_na_janela_das_13h() -> None:
    """Às 10h a próxima janela é 13h — antes das 18h não há extra pra pegar."""
    agora = datetime(2026, 8, 3, 10, 10, tzinfo=_BRT)
    dou_monitor._ultima_ok[_D] = (agora, 0)
    estado = proactive._estado_checado_sem_mp(_KEY_ALL, agora)
    assert estado == "sem MP até o momento; re-checo às 13h"
    assert "extra" not in estado


def test_apurado_ultima_janela_ressalva_extra_tardia() -> None:
    """Às 19h NÃO se afirma 'sem MP' seco: o dia só fecha às 6h e extra
    tardia existe — a ressalva aponta o briefing, que dá o veredito."""
    agora = datetime(2026, 8, 3, 19, 4, tzinfo=_BRT)
    dou_monitor._ultima_ok[_D] = (agora, 0)
    estado = proactive._estado_checado_sem_mp(_KEY_ALL, agora)
    assert estado == ("sem MP na checagem das 19h; extra tardia (se houver) "
                      "chega no briefing de amanhã")


def test_apurado_exige_checagem_ok_registrada() -> None:
    """Restart/Inlabs fora → sem registro → None (não inventa apuração)."""
    assert proactive._estado_checado_sem_mp(_KEY_ALL, _as_13h05()) is None


def test_apurado_com_mp_encontrada_nao_se_aplica() -> None:
    dou_monitor._ultima_ok[_D] = (_as_13h05(), 2)
    assert proactive._estado_checado_sem_mp(_KEY_ALL, _as_13h05()) is None


def test_apurado_dia_fechado_nao_se_aplica() -> None:
    """Dia encerrado (6h do dia seguinte) resolve pela fila ('Tirei da
    fila'), não por esta linha."""
    depois = datetime(2026, 8, 4, 7, 0, tzinfo=_BRT)
    dou_monitor._ultima_ok[_D] = (depois, 0)
    assert proactive._estado_checado_sem_mp(_KEY_ALL, depois) is None


def test_apurado_entrada_com_numeros_nao_se_aplica() -> None:
    """Entrada ':1382' é NOTA em geração (MP confirmada) — 'sem MP até o
    momento' seria contradição."""
    dou_monitor._ultima_ok[_D] = (_as_13h05(), 0)
    assert proactive._estado_checado_sem_mp(f"{_D.isoformat()}:1382",
                                            _as_13h05()) is None


def test_collect_usa_o_estado_apurado(monkeypatch) -> None:
    """Integração: com checagem OK de HOJE registrada (0 MP), a linha da
    janela diz 'sem MP', não 'checando agora'. Hora real varia → só o
    prefixo do estado é travado aqui; as variantes exatas, acima."""
    hoje = date.today()
    dou_monitor._ultima_ok[hoje] = (datetime.now(_BRT), 0)
    linhas = _linhas(monkeypatch, [hoje])
    assert len(linhas) == 1
    assert "sem MP" in linhas[0]
    assert "checando agora" not in linhas[0]
    assert "próxima janela" not in linhas[0]


def test_marcar_geradas_nao_rebaixa_o_estado_apurado() -> None:
    """O disparo do job da fila NÃO pode trocar a linha informativa por
    'checando agora' — era exatamente o texto que o dono pediu pra tirar."""
    hoje = date.today()
    dou_monitor._ultima_ok[hoje] = (datetime.now(_BRT), 0)
    key = f"{hoje.isoformat()}:all"
    fatos = [proactive.ProactiveFact("mp", "nota_fila", key,
                                     proactive._texto_fila(key, "qualquer"))]
    proactive._marcar_geradas_agora(fatos, [hoje])
    assert "sem MP" in fatos[0].text
    assert "checando agora" not in fatos[0].text
