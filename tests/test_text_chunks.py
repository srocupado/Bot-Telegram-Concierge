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


# ─────────── regressões da auditoria de 03/08/2026 ───────────

from bot.utils.text import _len16


def _sem_tag_partida(bloco: str) -> bool:
    """Nenhum '<' sem '>' no fim, nem resto de tag no início."""
    import re
    return (re.search(r"<[^>]*$", bloco) is None
            and re.match(r"^[^<]*>", bloco) is None)


def test_link_longo_nao_estoura_o_teto_na_costura() -> None:
    """Repro do bug: blockquote+link com href de 300+ chars. A reserva fixa
    de 80 não cobria a REABERTURA da tag com atributo — saía bloco de 4327
    chars, o Telegram recusava e o fallback reenviava o MESMO bloco grande:
    briefing/digest sumiam inteiros."""
    url = "https://www.in.gov.br/web/dou/-/medida-provisoria-" + "a" * 300
    texto = (f'<blockquote expandable><b><i><a href="{url}">'
             + "palavra " * 1200 + "</a></i></b></blockquote>")
    blocos = chunk_text(texto, mode="html")
    assert all(_len16(b) <= 4000 for b in blocos), [_len16(b) for b in blocos]
    assert all(not _tags_abertas(b) for b in blocos)


def test_corte_com_espaco_nao_entra_na_tag_com_atributo() -> None:
    """O rfind(' ') rodava DEPOIS da proteção de tag e voltava o corte pra
    DENTRO de '<a href=...>': o bloco terminava em '…<a' e o seguinte começava
    ' href="…">' — Telegram recusava e o texto puro mostrava o href cru."""
    url = "https://www.in.gov.br/web/dou/-/" + "b" * 200
    linha = ("x" * 3980 + " fim do parágrafo "
             + f'<a href="{url}">texto do link</a>' + " y" * 400)
    blocos = chunk_text(linha, mode="html")
    for b in blocos:
        assert _sem_tag_partida(b), f"tag partida entre blocos: …{b[-40:]!r}"
        assert not _tags_abertas(b)


def test_teto_e_medido_em_utf16() -> None:
    """O Telegram conta 4096 em unidades UTF-16: emoji fora do BMP vale 2.
    Um texto de 3500 emojis tem len()=3500 mas 7000 unidades — tinha que
    ser quebrado, e cada bloco tem que caber no teto em UTF-16."""
    texto = "📄" * 3500
    blocos = chunk_text(texto, limit=4000, mode="html")
    assert len(blocos) > 1, "não quebrou um texto de 7000 unidades UTF-16"
    assert all(_len16(b) <= 4000 for b in blocos)
