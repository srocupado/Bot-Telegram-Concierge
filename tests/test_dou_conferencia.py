"""Conferência cruzada DOU × Câmara.

Toda a detecção de MP depende do Inlabs, e o pior modo de falha dele é mudo:
404 em arquivo que existe, ZIP truncado servido como válido. O bot conclui
"não houve MP" e nada no estado dele denuncia o buraco. A API da Câmara é de
outro órgão e outra infraestrutura — é o que permite saber o que se perdeu.

Formato validado contra a API viva em 01/08/2026 (49 MPVs em 2026; a 1381
aparece com dataApresentacao 2026-07-31, batendo com a data do DOU).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from bot.services import camara, dou_monitor, proactive

HOJE = datetime.now(proactive.BRT).date()


class _FakeSession:
    """scalars é chamado 2x em mps_nao_recebidas: notas entregues
    (dou_seen_mps) e MPs AVISADAS no proativo (ProactiveNotice kind='mp')."""

    def __init__(self, recebidas=(), avisadas=()):
        self._respostas = [
            [SimpleNamespace(numero=n, ano=a) for n, a in recebidas],
            [SimpleNamespace(key=f"{n}/{a}") for n, a in avisadas],
        ]

    async def scalars(self, _stmt):
        return list(self._respostas.pop(0)) if self._respostas else []

    async def commit(self):
        return None


def _mp(numero: str, quando, ementa="Abre crédito extraordinário."):
    return {"numero": numero, "ano": quando.year, "ementa": ementa, "data": quando}


def _camara_com(monkeypatch, mps):
    async def _lista(ano):
        return [mp for mp in mps if mp["ano"] == ano]

    monkeypatch.setattr(camara, "mpvs_do_ano", _lista)


def _faltando(monkeypatch, mps, recebidas=(), avisadas=()):
    _camara_com(monkeypatch, mps)
    return asyncio.run(
        dou_monitor.mps_nao_recebidas(_FakeSession(recebidas, avisadas), 1, HOJE)
    )


def test_mp_que_o_inlabs_perdeu_aparece(monkeypatch) -> None:
    saiu = HOJE - timedelta(days=5)
    faltando = _faltando(monkeypatch, [_mp("1381", saiu)])
    assert [mp["numero"] for mp in faltando] == ["1381"]


def test_mp_ja_entregue_nao_aparece(monkeypatch) -> None:
    saiu = HOJE - timedelta(days=5)
    faltando = _faltando(
        monkeypatch, [_mp("1381", saiu)], recebidas=[("1381", saiu.year)],
    )
    assert faltando == []


def test_folga_evita_alarme_com_mp_recem_publicada(monkeypatch) -> None:
    """A Câmara leva até ~1 dia pra registrar. Sem folga, a MP de hoje —
    que o bot vai entregar daqui a pouco — viraria 'você perdeu isto'."""
    faltando = _faltando(monkeypatch, [_mp("1400", HOJE)])
    assert faltando == [], "MP de hoje não pode virar alarme"

    ontem = _faltando(monkeypatch, [_mp("1400", HOJE - timedelta(days=1))])
    assert ontem == [], "dentro da folga ainda não conta"


def test_mp_fora_da_janela_nao_entra(monkeypatch) -> None:
    """MP anterior à assinatura do monitor nunca foi 'perdida'; listar o
    histórico inteiro viraria enxurrada na primeira rodada."""
    velha = HOJE - timedelta(days=dou_monitor._JANELA_CONFERENCIA_DIAS + 5)
    assert _faltando(monkeypatch, [_mp("1300", velha)]) == []


def test_virada_de_ano_consulta_os_dois_anos(monkeypatch) -> None:
    """Em janeiro a janela de 30 dias cruza dezembro — se só o ano corrente
    fosse consultado, MP de dezembro sumiria da conferência."""
    anos: list[int] = []

    async def _lista(ano):
        anos.append(ano)
        return []

    monkeypatch.setattr(camara, "mpvs_do_ano", _lista)
    jan = datetime(2026, 1, 10, tzinfo=proactive.BRT).date()
    asyncio.run(dou_monitor.mps_nao_recebidas(_FakeSession(), 1, jan))
    assert sorted(anos) == [2025, 2026]


# ───────────────────────── integração com o proativo ─────────────────────────

def _prep_proactive(monkeypatch, marcadas, ja=None):
    ja = ja or set()

    async def _already(_s, _uid, kind, key):
        return f"{kind}:{key}" in ja

    async def _mark(_s, _uid, kind, key):
        marcadas.append(f"{kind}:{key}")

    monkeypatch.setattr(proactive, "already_notified", _already)
    monkeypatch.setattr(proactive, "mark_notified", _mark)


def _conferir(monkeypatch, mps, marcadas, recebidas=(), ja=None):
    _camara_com(monkeypatch, mps)
    _prep_proactive(monkeypatch, marcadas, ja)
    user = SimpleNamespace(id=9, dou_mp_subscribed=True, dou_ultimo_dia_ok=None)
    return asyncio.run(
        proactive._conferir_camara(_FakeSession(recebidas), user, HOJE)
    )


def test_mp_perdida_recuperavel_vira_pendencia(monkeypatch) -> None:
    """O dia entra na fila e a retroativa (que já existe) faz a entrega —
    nenhum caminho de entrega novo."""
    saiu = HOJE - timedelta(days=5)
    marcadas: list[str] = []

    facts = _conferir(monkeypatch, [_mp("1381", saiu)], marcadas)

    assert f"mp_pendente:{saiu.isoformat()}" in marcadas
    assert [f.kind for f in facts] == ["mp_conferencia"]
    assert "MP 1381" in facts[0].text and "não recebeu" in facts[0].text.lower()
    assert "fila" in facts[0].text


def test_mp_perdida_antiga_avisa_com_saida_manual(monkeypatch) -> None:
    """Fora da janela da retroativa não adianta enfileirar (expiraria na
    hora) — mas sumir calado é justamente o que não pode."""
    saiu = HOJE - timedelta(days=proactive._MP_RETRO_EXPIRA_DIAS + 3)
    marcadas: list[str] = []

    facts = _conferir(monkeypatch, [_mp("1370", saiu)], marcadas)

    assert marcadas == [], "enfileirou dia que expiraria na mesma janela"
    assert [f.kind for f in facts] == ["mp_conferencia"]
    assert "/mp_dou_agora" in facts[0].text


def test_mp_ja_avisada_nao_repete(monkeypatch) -> None:
    saiu = HOJE - timedelta(days=5)
    marcadas: list[str] = []

    facts = _conferir(
        monkeypatch, [_mp("1381", saiu)], marcadas,
        ja={f"mp_conferencia:conf:1381/{saiu.year}"},
    )
    assert facts == []


def test_api_da_camara_fora_e_reportada(monkeypatch) -> None:
    """Conferência que falha calada é pior que conferência nenhuma: passa
    sensação de cobertura que não existe."""
    async def _explode(_ano):
        raise camara.CamaraError("504 na Câmara")

    monkeypatch.setattr(camara, "mpvs_do_ano", _explode)
    marcadas: list[str] = []
    _prep_proactive(monkeypatch, marcadas)
    user = SimpleNamespace(id=9, dou_mp_subscribed=True, dou_ultimo_dia_ok=None)

    facts = asyncio.run(proactive._conferir_camara(_FakeSession(), user, HOJE))

    assert [f.kind for f in facts] == ["mp_conf_fail"]
    assert "Não consegui conferir" in facts[0].text
    assert "seguiu normal" in facts[0].text, (
        "precisa deixar claro que só a conferência caiu, não a checagem do DOU"
    )


def test_falha_da_camara_nao_repete_no_mesmo_dia(monkeypatch) -> None:
    async def _explode(_ano):
        raise camara.CamaraError("504")

    monkeypatch.setattr(camara, "mpvs_do_ano", _explode)
    marcadas: list[str] = []
    _prep_proactive(monkeypatch, marcadas,
                    ja={f"mp_conf_fail:conf_fail:{HOJE.isoformat()}"})
    user = SimpleNamespace(id=9, dou_mp_subscribed=True, dou_ultimo_dia_ok=None)

    facts = asyncio.run(proactive._conferir_camara(_FakeSession(), user, HOJE))
    assert facts == []


def test_mp_avisada_sem_nota_nao_e_acusada(monkeypatch) -> None:
    """dou_seen_mps só registra MP com NOTA entregue. A MP que o dono viu no
    briefing e dispensou ("Não" no botão) tem só ProactiveNotice(kind="mp") —
    e seria acusada de perdida, com dia enfileirado e ZIPs re-baixados à toa.
    A pergunta certa é "o dono ficou sabendo?", não "recebeu o DOCX?".
    """
    saiu = HOJE - timedelta(days=5)
    faltando = _faltando(
        monkeypatch, [_mp("1381", saiu)], recebidas=(), avisadas=[("1381", saiu.year)],
    )
    assert faltando == []


def test_chave_de_aviso_corrompida_nao_derruba(monkeypatch) -> None:
    """Chave fora do formato "numero/ano" não pode estourar a conferência —
    ela é a última rede contra perder MP."""
    saiu = HOJE - timedelta(days=5)
    _camara_com(monkeypatch, [_mp("1381", saiu)])
    sessao = _FakeSession()
    sessao._respostas = [[], [SimpleNamespace(key="lixo-sem-barra")]]
    faltando = asyncio.run(dou_monitor.mps_nao_recebidas(sessao, 1, HOJE))
    assert [mp["numero"] for mp in faltando] == ["1381"]
