"""Quebra de mensagens longas (teto de 4096 do Telegram).

Bloco que passa do teto com tag ou cerca de código ABERTA é recusado pelo
Telegram: no chat cai pro texto puro (tags cruas à vista) e nos envios do
scheduler/proativo falha nas DUAS tentativas — a mensagem simplesmente não
chega.
"""
from __future__ import annotations

from bot.utils.text import _tags_abertas, chunk_text

TETO = 4096


def test_texto_curto_nao_e_quebrado() -> None:
    assert chunk_text("oi") == ["oi"]
    assert chunk_text("") == []


def test_linha_gigante_com_tag_nao_deixa_tag_aberta() -> None:
    texto = "<b>" + "x" * 9000 + "</b>"
    blocos = chunk_text(texto, mode="html")
    assert len(blocos) > 1
    assert all(len(b) <= TETO for b in blocos)
    assert all(not _tags_abertas(b) for b in blocos), "tag ficou aberta num bloco"
    assert sum(b.count("x") for b in blocos) == 9000, "conteúdo perdido"


def test_tag_aninhada_reabre_no_bloco_seguinte() -> None:
    texto = "<blockquote><b>" + "y" * 8000 + "</b></blockquote>"
    blocos = chunk_text(texto, mode="html")
    assert all(not _tags_abertas(b) for b in blocos)
    assert blocos[1].startswith("<blockquote>")


def test_corte_nao_cai_dentro_de_tag_nem_de_entidade() -> None:
    linha = "<i>" + "z" * 3900 + "&amp;" + "w" * 500 + "</i>"
    for b in chunk_text(linha, mode="html"):
        assert not b.rstrip().endswith("<"), "cortou dentro de uma tag"
        assert "&" not in b.split(">")[-1][-6:] or ";" in b[-8:], "cortou entidade"


def test_cerca_de_codigo_nao_fica_aberta_entre_blocos() -> None:
    md = "antes\n```python\n" + ("linha de codigo\n" * 1200) + "```\ndepois"
    blocos = chunk_text(md, mode="markdown")
    assert len(blocos) > 1
    assert all(b.count("```") % 2 == 0 for b in blocos), "cerca aberta num bloco"
    assert all(len(b) <= TETO for b in blocos)


def test_modo_plain_nao_costura_nada() -> None:
    """Saída do agente vai com parse_mode=None: tag costurada viraria lixo."""
    blocos = chunk_text("<b>" + "x" * 9000 + "</b>")
    assert "".join(blocos).count("<b>") == 1
