"""Mensagem pro grupo logo após a nota técnica (pedido do dono, 26/09/2026).

Texto do dono, com o negrito do WhatsApp (*...*), enviado como texto puro pra
copiar e colar. Ementa OFICIAL (a do DOU), prazo de emendas do mesmo
compute_prazos do card e do DOCX.
"""
from __future__ import annotations

import asyncio

from bot.services import dou_monitor

_MP = {"numero": "1394", "ano": 2026, "data_publicacao": "2026-09-25",
       "ementa": "Proíbe a exploração de loterias de apostas de quota fixa.",
       "url_planalto": "https://x"}


def test_formato_igual_ao_modelo_do_dono() -> None:
    assert dou_monitor.mensagem_grupo(_MP, None) == (
        "Senhoras Deputadas e Senhores Deputados,\n\n"
        "A Assessoria desta Liderança encaminha Nota Técnica elaborada a "
        "respeito da *Medida Provisória nº 1.394/2026 (Proíbe a exploração de "
        "loterias de apostas de quota fixa).*\n\n"
        "O prazo para apresentação de emenda a essa Medida Provisória será do "
        "dia *25/09/2026 até 01/10/2026.*\n\n"
        "Respeitosamente,\n"
        "LIDERANÇA DO PODEMOS"
    )


def test_prazo_vem_do_mesmo_calculo_do_docx() -> None:
    mp = {**_MP, "data_publicacao": "2026-12-28"}
    msg = dou_monitor.mensagem_grupo(mp, None)
    fim = dou_monitor.compute_prazos(dou_monitor.date(2026, 12, 28))["emendas_fim"]
    assert f"*28/12/2026 até {fim.strftime('%d/%m/%Y')}.*" in msg


def test_sem_ementa_no_dict_usa_a_da_nota() -> None:
    mp = {**_MP, "ementa": ""}
    msg = dou_monitor.mensagem_grupo(mp, {"ementa": "Abre crédito extraordinário."})
    assert "(Abre crédito extraordinário).*" in msg


class _Bot:
    def __init__(self, falha_msg: bool = False):
        self.falha_msg = falha_msg
        self.eventos: list[tuple] = []

    async def send_document(self, chat_id, doc, **kw):
        self.eventos.append(("docx",))

    async def send_message(self, chat_id, texto, **kw):
        if self.falha_msg:
            raise RuntimeError("Telegram fora")
        self.eventos.append(("msg", texto, kw.get("parse_mode", "?")))


def _rodar(monkeypatch, bot):
    import types

    async def _nota(mp, **kw):
        return {"ementa": "x", "p1_contexto": "y", "p2_dispositivos": "z"}
    monkeypatch.setattr(dou_monitor, "generate_nota_tecnica", _nota)
    monkeypatch.setattr(dou_monitor, "build_docx", lambda mp, nota: b"DOCX")
    user = types.SimpleNamespace(id=1, dou_mp_provider=None, dou_mp_model=None,
                                 dou_mp_effort=None)
    asyncio.run(dou_monitor.gerar_e_enviar_nota(bot, user, dict(_MP)))


def test_mensagem_sai_depois_da_nota_como_texto_puro(monkeypatch) -> None:
    bot = _Bot()
    _rodar(monkeypatch, bot)
    assert [e[0] for e in bot.eventos] == ["docx", "msg"]
    assert "LIDERANÇA DO PODEMOS" in bot.eventos[1][1]
    assert bot.eventos[1][2] is None, "com HTML/Markdown os * sumiriam"


def test_falha_da_mensagem_nao_faz_a_nota_parecer_falha(monkeypatch) -> None:
    """A nota já foi entregue: exceção aqui faria o caller gerar de novo."""
    bot = _Bot(falha_msg=True)
    _rodar(monkeypatch, bot)
    assert bot.eventos == [("docx",)]
