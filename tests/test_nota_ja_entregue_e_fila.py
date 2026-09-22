"""Três correções do relato de 22/09/2026 (dono).

Sequência real: às 07h05 o proativo entregou a nota da MP 1.392. Depois, no
chat, o dono pediu de novo — o bot respondeu "📄 Gerando a nota técnica… te
aviso quando sair" e NUNCA mandou nada. Três vezes.

  "Ressalto que ele não entregou NENHUMA nota que não a já entregue hj no
   proativo das 7h05. quando dei o /mp_dou_agora, ele falou que iria gerar e
   não mandou nada."

A: o job descobria que a nota já fora entregue, dava baixa e saía por
   `return True` MUDO — com a promessa já feita. Silêncio depois de promessa
   é o pior modo de falha deste projeto.

B: a fila não drenava. Em 12/09, ao corrigir a perda da MP 1.391, eu travei a
   baixa de todo dia com edição extra e zero MP — e extra é quase diário
   (medido em 22/09: 4 de 6 dias úteis). Os dias 14, 15, 16 e 21/09 estavam
   presos, a caminho de disparar "desisti de checar" em cadeia. A saída já
   existia e eu não liguei: a sonda do Planalto dá evidência POSITIVA de que
   não há MP acima da última entregue.

C: o bot prometeu "nas próximas vezes já vou enviar a mensagem de WhatsApp
   automaticamente" — impossível, ele não altera o pipeline. Agravado por o
   dono ter pedido de antemão: "Não elabore se não conseguir mandar".
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.services import proactive


# ───────────── A: promessa feita exige resposta ─────────────

def test_saida_por_ja_entregue_nao_pode_ser_muda() -> None:
    """A porta que engoliu as três tentativas do dono."""
    src = inspect.getsource(proactive._tentar_nota_via_portal)
    trecho = src[src.index("if not pendentes and not faltam_no_portal"):]
    trecho = trecho[:trecho.index("return True") + len("return True")]
    assert "_avisar_ja_entregue" in trecho, (
        "voltou a dar baixa e sair sem dizer nada ao usuário que esperava"
    )
    assert "usuario_esperando" in trecho


def test_retentativa_de_fundo_segue_calada() -> None:
    """O default importa: na fila do proativo ninguém prometeu nada, e um
    aviso por dia re-checado viraria spam."""
    sig = inspect.signature(proactive._tentar_nota_via_portal)
    p = sig.parameters["usuario_esperando"]
    assert p.default is False
    assert p.kind is inspect.Parameter.KEYWORD_ONLY


def test_caminho_do_usuario_marca_que_esta_esperando() -> None:
    """/mp_dou_agora e o botão já disseram 'te aviso quando sair'."""
    from bot.handlers import dou_mp

    src = inspect.getsource(dou_mp._rodar_nota)
    assert "usuario_esperando=True" in src


def test_aviso_diz_qual_mp_e_quando_saiu(monkeypatch) -> None:
    """Sem a hora, o dono acha que o bot se enganou; com ela, ele lembra do
    aviso das 07h05."""
    enviadas: list[str] = []

    async def _send(_bot, _chat, texto, **kw):
        enviadas.append(texto)
        return True

    agora = datetime.now(timezone.utc)
    linha = SimpleNamespace(key="1392/2026", sent_at=agora - timedelta(hours=4))

    class _Sess:
        async def scalars(self, _stmt):
            return [linha]

    monkeypatch.setattr(proactive, "_send", _send)
    asyncio.run(proactive._avisar_ja_entregue(
        None, _Sess(), SimpleNamespace(id=1), ["1392"]))

    assert len(enviadas) == 1
    txt = enviadas[0]
    assert "já está com você" in txt
    assert "1.392" in txt, "não disse QUAL mp"
    assert "enviada" in txt, "não disse QUANDO"
    assert "gera de novo" in txt, "não disse como forçar uma nova"


# ───────────── B: a fila volta a drenar ─────────────

def test_extra_sem_mp_destrava_com_o_planalto_confirmando() -> None:
    src = inspect.getsource(proactive.collect_mp)
    assert "planalto_sem_novas" in src
    assert "not planalto_sem_novas" in src, (
        "o veredito do dia ignora a evidência do Planalto — a fila volta a travar"
    )


def test_flag_so_liga_com_a_sonda_respondendo() -> None:
    """Falha de rede ou usuário sem régua NÃO podem virar 'não há MP'."""
    src = inspect.getsource(proactive.collect_mp)
    i = src.index("planalto_sem_novas = True")
    antes = src[:i]
    # A atribuição tem de estar DEPOIS do except da sonda e sob `if not novas`.
    assert "sonda do Planalto falhou" in antes
    assert antes.rindex("if not novas:") > antes.rindex("return []")


def test_comando_manual_usa_o_mesmo_desempate() -> None:
    """Se só o proativo destravasse, /mp_dou_agora contradiria a fila."""
    from bot.handlers import dou_mp

    src = inspect.getsource(dou_mp._checar_via_portal)
    assert "confirma_sem_mp_nova" in src
    assert "baixa_checagem_manual" in src


def test_confirmacao_e_conservadora_sem_regua(monkeypatch) -> None:
    """Usuário sem nenhuma MP entregue não tem régua — 'não sei', não 'não há'."""
    from bot.services import dou_planalto

    async def _sem_regua(_s, _uid):
        return None

    monkeypatch.setattr(dou_planalto, "ultimo_numero_entregue", _sem_regua)
    assert asyncio.run(
        dou_planalto.confirma_sem_mp_nova(None, 1, 2026)) is False


def test_confirmacao_e_conservadora_quando_a_rede_cai(monkeypatch) -> None:
    from bot.services import dou_planalto

    async def _regua(_s, _uid):
        return (1392, 2026)

    async def _explode(*a, **kw):
        raise RuntimeError("planalto fora")

    monkeypatch.setattr(dou_planalto, "ultimo_numero_entregue", _regua)
    monkeypatch.setattr(dou_planalto, "sondar_novas", _explode)
    assert asyncio.run(
        dou_planalto.confirma_sem_mp_nova(None, 1, 2026)) is False


def test_confirmacao_positiva_quando_a_sonda_volta_vazia(monkeypatch) -> None:
    from bot.services import dou_planalto

    async def _regua(_s, _uid):
        return (1392, 2026)

    async def _vazio(*a, **kw):
        return []

    monkeypatch.setattr(dou_planalto, "ultimo_numero_entregue", _regua)
    monkeypatch.setattr(dou_planalto, "sondar_novas", _vazio)
    assert asyncio.run(
        dou_planalto.confirma_sem_mp_nova(None, 1, 2026)) is True


def test_mp_nova_achada_impede_a_baixa(monkeypatch) -> None:
    """Se a sonda ACHA MP nova, o dia não pode fechar de jeito nenhum."""
    from bot.services import dou_planalto

    async def _regua(_s, _uid):
        return (1391, 2026)

    async def _achou(*a, **kw):
        return [SimpleNamespace(numero="1392", ano=2026)]

    monkeypatch.setattr(dou_planalto, "ultimo_numero_entregue", _regua)
    monkeypatch.setattr(dou_planalto, "sondar_novas", _achou)
    assert asyncio.run(
        dou_planalto.confirma_sem_mp_nova(None, 1, 2026)) is False


# ───────────── C: nada de prometer automação ─────────────

def test_prompt_proibe_prometer_automacao_futura() -> None:
    import bot.handlers.chat as chat

    prompts = [v for v in vars(chat).values()
               if isinstance(v, str) and "NUNCA PROMETA COMPORTAMENTO" in v]
    assert prompts, "a regra sumiu do prompt"
    p = prompts[0]
    for frase in ("da próxima vez eu já faço", "a partir de agora",
                  "agendar_comando", "não altera o pipeline",
                  "não inventar" if False else "não invente"):
        assert frase.lower() in p.lower(), frase


def test_prompt_manda_respeitar_pedido_condicional() -> None:
    """O dono disse: 'Não elabore se não conseguir mandar ou perguntar'. O bot
    elaborou E prometeu automação — as duas coisas que ele havia vetado."""
    import bot.handlers.chat as chat

    p = next(v for v in vars(chat).values()
             if isinstance(v, str) and "NUNCA PROMETA COMPORTAMENTO" in v)
    assert "condicionar o pedido" in p
    assert "não é entregar outra coisa parecida" in p
