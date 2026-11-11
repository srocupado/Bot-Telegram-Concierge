"""Rede de captura pelo Planalto + detector do despacho no portal.

Caso que originou tudo (12/09/2026, dono): "Tivemos uma medida provisória
publicada, a 1.391/26. Bot não pegou."

A MP 1.391 saiu em 11/09/2026 numa edição EXTRA. Medido contra as fontes
vivas no dia:

  - busca do portal por artType="Medida Provisória" em 11/09 → ZERO;
  - o índice TINHA o despacho "Encaminhamento ao Congresso Nacional do texto
    da Medida Provisória nº 1.391, de 11 de setembro de 2026" — e o filtro
    por artType o descartava como ruído;
  - com o Inlabs fora do projeto, o portal virou fonte única e respondeu
    "houve DOU em 11/09/2026 e NENHUMA Medida Provisória";
  - o dia fechou às 06:00 de 12/09, recebeu baixa e saiu da fila.

Perda em SILÊNCIO — o pior modo de falha deste projeto, e o que a premissa
inegociável proíbe. Estes testes travam as quatro correções.
"""
from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest
import respx

from bot.services import dou_planalto as pl
from bot.services import dou_portal
from bot.services.proactive import _Colheita

ANO = 2026


def _pagina_mp(numero="1.391", dia="11", mes="SETEMBRO", ano="2026",
               ementa="Autoriza a concessão de subvenção econômica.") -> bytes:
    """Página do Planalto COMO ELA É: latin-1, sem declarar charset, e com o
    título quebrado em três pedaços por <br>/tabulação. As duas coisas
    derrubaram a primeira versão do parser — a regex não casava e a MP ficava
    invisível, que é exatamente o bug que este módulo combate."""
    html = (
        f"<html><body><p>mpv{numero.replace('.', '')}</p>"
        f"<p>Presidência da República</p>"
        f"<p>MEDIDA PROVISÓRIA Nº {numero}, DE <br>\t{dia}\n "
        f"DE {mes} DE {ano}</p>"
        f"<p>{ementa}</p>"
        f"<p>O PRESIDENTE DA REPÚBLICA, no uso da atribuição que lhe confere "
        f"o art. 62 da Constituição, adota a seguinte Medida Provisória:</p>"
        f"</body></html>"
    )
    return html.encode("iso-8859-1")


def _url(numero, ano=ANO) -> str:
    return pl.url_mp(numero, ano)


# ─────────────── parsing: os dois enganos do mundo real ───────────────

@respx.mock
def test_pagina_latin1_sem_charset_e_lida_corretamente() -> None:
    """A página não declara charset. O httpx assume utf-8 ("PROVIS�RIA") e o
    autodetect do BeautifulSoup chutou cp1250 ("Nş 1.391"). Nos dois casos a
    MP ficava invisível."""
    respx.get(_url(1391)).mock(return_value=httpx.Response(200, content=_pagina_mp()))
    mp = asyncio.run(pl.buscar_mp(1391, ANO))
    assert mp is not None, "página latin-1 não foi lida"
    assert mp.numero == "1391" and mp.ano == 2026
    assert "subvenção econômica" in (mp.ementa or ""), mp.ementa
    assert "Nº 1.391" in mp.titulo


@respx.mock
def test_titulo_quebrado_em_varias_linhas_ainda_casa() -> None:
    """'MEDIDA PROVISÓRIA Nº 1.391, DE' / '11' / 'DE SETEMBRO DE 2026' — a
    regex tem que rodar sobre o texto inteiro, nunca linha a linha."""
    respx.get(_url(1391)).mock(return_value=httpx.Response(200, content=_pagina_mp()))
    mp = asyncio.run(pl.buscar_mp(1391, ANO))
    assert mp.data_publicacao == "2026-09-11", (
        "data saiu errada — ela vem do TÍTULO da MP, não da requisição"
    )


@respx.mock
def test_404_e_ausencia_e_nao_erro() -> None:
    respx.get(_url(1392)).mock(return_value=httpx.Response(404))
    assert asyncio.run(pl.buscar_mp(1392, ANO)) is None


@respx.mock
def test_200_que_nao_e_mp_nao_vira_mp() -> None:
    """O Planalto às vezes responde 200 com página de erro. Inventar MP é tão
    grave quanto perder uma."""
    respx.get(_url(1392)).mock(return_value=httpx.Response(
        200, content="<html><body>Página não encontrada</body></html>".encode()))
    assert asyncio.run(pl.buscar_mp(1392, ANO)) is None


@respx.mock
def test_pagina_servindo_outro_numero_e_recusada() -> None:
    """URL da 1.392 devolvendo o texto da 1.391: nada dessa página é confiável."""
    respx.get(_url(1392)).mock(return_value=httpx.Response(
        200, content=_pagina_mp(numero="1.391")))
    assert asyncio.run(pl.buscar_mp(1392, ANO)) is None


@respx.mock
def test_erro_de_rede_sobe_e_nao_vira_ausencia() -> None:
    """Planalto fora é 'não sei', jamais 'não há MP nova'."""
    respx.get(_url(1392)).mock(side_effect=httpx.ConnectError("fora"))
    with pytest.raises(httpx.HTTPError):
        asyncio.run(pl.buscar_mp(1392, ANO))


# ───────────────────────── a sonda sequencial ─────────────────────────

def _controle(numero: int = 1390, ano: int = ANO) -> None:
    """A sonda exige que a página da PRÓPRIA régua responda como MP (prova
    de que o Planalto está servindo MPs pra nós nesta sonda)."""
    respx.get(pl.url_mp(numero, ano)).mock(return_value=httpx.Response(
        200, content=_pagina_mp(numero=f"{numero // 1000}.{numero % 1000:03d}")))


@respx.mock
def test_sonda_acha_a_mp_perdida() -> None:
    """O caso real: entregou até a 1.390, a 1.391 existe. A pergunta não é
    'saiu MP no dia D?' (que depende do índice cobrir D) e sim 'existe MP
    depois da última que entreguei?'."""
    _controle()
    respx.get(_url(1391)).mock(return_value=httpx.Response(200, content=_pagina_mp()))
    for n in (1392, 1393):
        respx.get(_url(n)).mock(return_value=httpx.Response(404))
    novas = asyncio.run(pl.sondar_novas(1390, [ANO]))
    assert [m.numero for m in novas] == ["1391"]
    assert novas[0].data_publicacao == "2026-09-11"


@respx.mock
def test_um_404_no_meio_nao_encerra_a_varredura() -> None:
    """Planalto pode demorar a publicar UMA página. Parar no primeiro 404
    esconderia as seguintes — esconder MP é o que este módulo combate."""
    _controle()
    respx.get(_url(1391)).mock(return_value=httpx.Response(404))
    respx.get(_url(1392)).mock(return_value=httpx.Response(
        200, content=_pagina_mp(numero="1.392", dia="12")))
    for n in (1393, 1394):
        respx.get(_url(n)).mock(return_value=httpx.Response(404))
    novas = asyncio.run(pl.sondar_novas(1390, [ANO]))
    assert [m.numero for m in novas] == ["1392"], "parou no primeiro buraco"


@respx.mock
def test_dois_404_seguidos_encerram() -> None:
    """Sem teto a sonda varreria o infinito a cada janela proativa."""
    _controle()
    for n in (1391, 1392, 1393, 1394):
        respx.get(_url(n)).mock(return_value=httpx.Response(404))
    assert asyncio.run(pl.sondar_novas(1390, [ANO])) == []
    # 1391 e 1392 bastam pra parar; 1393 não deve nem ser pedido.
    pedidas = {str(c.request.url) for c in respx.calls}
    assert _url(1393) not in pedidas


@respx.mock
def test_virada_de_ano_sonda_as_duas_pastas() -> None:
    """A numeração NÃO reinicia em janeiro, mas a pasta do Planalto sim: a MP
    de janeiro/2027 mora em /2027/, a última de dezembro/2026 em /2026/.
    Sondar só um ano perderia MP toda primeira semana de janeiro."""
    respx.get(pl.url_mp(1399, 2027)).mock(return_value=httpx.Response(404))
    _controle(1399, 2026)
    respx.get(pl.url_mp(1400, 2026)).mock(return_value=httpx.Response(404))
    respx.get(pl.url_mp(1400, 2027)).mock(return_value=httpx.Response(
        200, content=_pagina_mp(numero="1.400", dia="5", mes="JANEIRO", ano="2027")))
    for n in (1401, 1402):
        for a in (2027, 2026):
            respx.get(pl.url_mp(n, a)).mock(return_value=httpx.Response(404))
    novas = asyncio.run(pl.sondar_novas(1399, [2027, 2026]))
    assert [(m.numero, m.ano) for m in novas] == [("1400", 2027)]


# ──────────── detector do despacho no índice do portal ────────────

def _pagina_busca(itens):
    import json
    return ('<html><script id="_x_params" type="application/json">'
            + json.dumps({"jsonArray": itens}, ensure_ascii=False)
            + "</script></html>")


def _item_despacho(numero="1.391", pub_name="DO1_EXTRA_D"):
    return {
        "title": "DESPACHO DO PRESIDENTE DA REPÚBLICA",
        "artType": "Mensagem",
        "pubName": pub_name,
        "pubDate": "11/09/2026",
        "urlTitle": "despacho-do-presidente-da-republica-731353673",
        "content": (
            "Encaminhamento ao Congresso Nacional do texto da <span "
            f"class='highlight'>Medida Provisória nº {numero}</span>, de 11 "
            "de setembro de 2026DESPACHO DO PRESIDENTE DA REPÚBLICA"
        ),
    }


@respx.mock
def test_despacho_revela_mp_que_o_indice_nao_tem() -> None:
    """O achado central: a informação ESTAVA no portal e o filtro por artType
    a jogava fora."""
    respx.get(url__startswith=dou_portal.BUSCA_URL).mock(
        return_value=httpx.Response(200, text=_pagina_busca([_item_despacho()])))
    respx.get(_url(1391)).mock(return_value=httpx.Response(200, content=_pagina_mp()))
    dia = asyncio.run(dou_portal.checar_dia_portal(date(2026, 9, 11)))
    assert [m.numero for m in dia.mps] == ["1391"]
    assert dia.mps[0].edicao == "Extra", (
        "edição saiu do índice errada — rotular extra como Normal já mentiu "
        "pro prompt da nota uma vez (MP 1.382)"
    )
    assert dia.edicao_confirmada is True


@respx.mock
def test_citacao_de_mp_antiga_nao_vira_mp_nova() -> None:
    """Casar só 'Medida Provisória nº X' faria qualquer portaria que cita uma
    MP velha virar MP do dia. Por isso a frase do encaminhamento é exigida
    inteira."""
    ruido = {
        "title": "PORTARIA Nº 123", "artType": "Portaria", "pubName": "DO1",
        "pubDate": "11/09/2026", "urlTitle": "portaria-123",
        "content": ("Considerando o disposto na Medida Provisória nº 1.200, "
                    "de 3 de março de 2025, resolve..."),
    }
    respx.get(url__startswith=dou_portal.BUSCA_URL).mock(
        return_value=httpx.Response(200, text=_pagina_busca([ruido])))
    dia = asyncio.run(dou_portal.checar_dia_portal(date(2026, 9, 11)))
    assert dia.mps == [], "inventou MP a partir de citação"


@respx.mock
def test_planalto_fora_com_despacho_citando_mp_estoura_alto() -> None:
    """O despacho é ato oficial: se ele cita a MP, ela EXISTE. Não poder ler o
    texto não pode virar 'dia sem MP'."""
    respx.get(url__startswith=dou_portal.BUSCA_URL).mock(
        return_value=httpx.Response(200, text=_pagina_busca([_item_despacho()])))
    respx.get(_url(1391)).mock(side_effect=httpx.ConnectError("fora"))
    with pytest.raises(dou_portal.PortalError, match="1391"):
        asyncio.run(dou_portal.checar_dia_portal(date(2026, 9, 11)))


@respx.mock
def test_extra_no_indice_sem_mp_marca_inconclusivo() -> None:
    """A sonda que confirma a edição olha secao=do1 (a REGULAR). Concluir
    'nenhuma MP' sobre um dia que teve EXTRA é estender a evidência além do
    que ela cobre."""
    portaria_extra = {
        "title": "PORTARIA MF Nº 2.715", "artType": "Portaria",
        "pubName": "DO1_EXTRA_C", "pubDate": "10/09/2026",
        "urlTitle": "portaria-mf", "content": "texto qualquer",
    }
    respx.get(url__startswith=dou_portal.BUSCA_URL).mock(
        return_value=httpx.Response(200, text=_pagina_busca([portaria_extra])))
    dia = asyncio.run(dou_portal.checar_dia_portal(date(2026, 9, 10)))
    assert dia.mps == []
    assert dia.edicao_confirmada is True
    assert dia.extras_sem_mp is True


# ─────────────────────────── baixa ───────────────────────────

def test_extra_sem_mp_nao_da_baixa() -> None:
    """O elo que fechou o dia 11/09 e apagou a MP da fila."""
    assert _Colheita([], True, False, False, 0, extras_sem_mp=True).baixa is False
    assert _Colheita([], True, False, False, 0, extras_sem_mp=False).baixa is True


# ────────── Planalto como plano B do TEXTO da nota (13/09/2026) ──────────
# Era o último trabalho exclusivo do Inlabs: quando o corpo da matéria no
# portal reprovava na régua de sanidade, a nota ficava esperando por ele
# ("nota vai esperar o Inlabs", dou_portal). O Planalto tem o ato íntegro na
# mesma URL determinística da rede de captura.

def _item_mp_indexada(numero="1.391"):
    return {
        "title": f"MEDIDA PROVISÓRIA Nº {numero}, DE 11 DE SETEMBRO DE 2026",
        "artType": "Medida Provisória",
        "pubName": "DO1",
        "pubDate": "11/09/2026",
        "urlTitle": "medida-provisoria-n-1391",
        "content": "",
    }


@respx.mock
def test_corpo_reprovado_no_portal_busca_texto_no_planalto() -> None:
    respx.get(url__startswith=dou_portal.BUSCA_URL).mock(
        return_value=httpx.Response(200, text=_pagina_busca([_item_mp_indexada()])))
    # Matéria do portal sem corpo aproveitável (o caso que reprovava na régua).
    respx.get(url__startswith=dou_portal.MATERIA_URL.format(url_title="")).mock(
        return_value=httpx.Response(200, text="<html><body></body></html>"))
    respx.get(_url(1391)).mock(return_value=httpx.Response(200, content=_pagina_mp()))

    dia = asyncio.run(dou_portal.checar_dia_portal(date(2026, 9, 11)))
    assert len(dia.mps) == 1
    mp = dia.mps[0]
    assert mp.texto, "nota continuaria esperando o Inlabs"
    assert "subvenção econômica" in mp.texto
    # E o dict que alimenta o pipeline da nota deixa de vir None.
    assert dou_portal.mp_dict_para_nota(mp, date(2026, 9, 11)) is not None


@respx.mock
def test_planalto_fora_nao_derruba_a_deteccao_da_mp() -> None:
    """Aqui a MP JÁ foi detectada — só o texto falta. Estourar transformaria
    'nota adiada' em 'dia não verificado', que é bem pior."""
    respx.get(url__startswith=dou_portal.BUSCA_URL).mock(
        return_value=httpx.Response(200, text=_pagina_busca([_item_mp_indexada()])))
    respx.get(url__startswith=dou_portal.MATERIA_URL.format(url_title="")).mock(
        return_value=httpx.Response(200, text="<html><body></body></html>"))
    respx.get(_url(1391)).mock(side_effect=httpx.ConnectError("fora"))

    dia = asyncio.run(dou_portal.checar_dia_portal(date(2026, 9, 11)))
    assert [m.numero for m in dia.mps] == ["1391"], "perdeu a MP por falta de texto"
    assert dia.mps[0].texto is None, "inventou texto"


# ───────────── revisão de 11/11/2026: "não sei" nunca vira "não há" ─────────────

def test_titulo_com_dia_primeiro_ordinal() -> None:
    """MP 1.382, "DE 1º DE AGOSTO DE 2026": toda MP assinada no dia 1º era
    invisível pra sonda e contava como "não existe"."""
    for dia in ("1º", "1°", "1o", "1"):
        parsed = pl._parse(_pagina_mp(numero="1.382", dia=dia, mes="AGOSTO"))
        assert parsed is not None, dia
        assert pl._data_do_titulo(parsed[0]).isoformat() == "2026-08-01"
    assert pl._data_publicacao_dou(
        "publicado no DOU de 1º.8.2026".encode()).isoformat() == "2026-08-01"


def test_despacho_do_portal_com_dia_primeiro() -> None:
    from bot.services import dou_portal
    assert dou_portal._DESPACHO_MP_RE.search(
        "Encaminhamento ao Congresso Nacional do texto da Medida Provisória "
        "nº 1.382, de 1º de agosto de 2026")


_BLOQUEIO = (b"<html><head><title>Acesso Negado!</title></head><body>"
             b"Esta requisi\xe7\xe3o foi bloqueada.</body></html>")


@respx.mock
def test_bloqueio_com_status_200_nao_vira_nenhuma_mp_nova() -> None:
    """Visto ao vivo em 11/11/2026: "Acesso Negado!" com status 200 pra todos
    os números. A sonda antiga devolvia [] — a evidência que dá baixa em dia."""
    for n in (1390, 1391, 1392):
        respx.get(_url(n)).mock(return_value=httpx.Response(200, content=_BLOQUEIO))
    with pytest.raises(pl.PlanaltoInconclusivo):
        asyncio.run(pl.sondar_novas(1390, [ANO]))


@respx.mock
def test_bloqueio_so_nos_numeros_novos_tambem_levanta() -> None:
    _controle()
    respx.get(_url(1391)).mock(return_value=httpx.Response(200, content=_BLOQUEIO))
    with pytest.raises(pl.PlanaltoInconclusivo):
        asyncio.run(pl.sondar_novas(1390, [ANO]))


@respx.mock
def test_controle_404_e_inconclusivo() -> None:
    """A MP da régua existe por definição; se o Planalto diz 404 pra ela, o
    que ele disser das seguintes não vale como ausência."""
    for n in (1390, 1391, 1392):
        respx.get(_url(n)).mock(return_value=httpx.Response(404))
    with pytest.raises(pl.PlanaltoInconclusivo, match="controle"):
        asyncio.run(pl.sondar_novas(1390, [ANO]))


@respx.mock
def test_buraco_abaixo_da_regua_e_sondado() -> None:
    """Entregues {1393, 1395}; a 1394 existe e nunca foi vista: a régua em
    1395 a escondia pra sempre."""
    _controle(1395)
    respx.get(_url(1394)).mock(return_value=httpx.Response(
        200, content=_pagina_mp(numero="1.394", dia="7", mes="OUTUBRO")))
    for n in (1396, 1397):
        respx.get(_url(n)).mock(return_value=httpx.Response(404))
    novas = asyncio.run(pl.sondar_novas(1395, [ANO], conhecidas={1393, 1395}))
    assert [m.numero for m in novas] == ["1394"]


def test_buscar_mp_fora_da_sonda_continua_devolvendo_none() -> None:
    @respx.mock
    def _run():
        respx.get(_url(1392)).mock(return_value=httpx.Response(200, content=_BLOQUEIO))
        return asyncio.run(pl.buscar_mp(1392, ANO))
    assert _run() is None


class _SessaoConhecidas:
    def __init__(self, seen, avisos):
        self.seen, self.avisos = seen, avisos

    async def scalars(self, stmt):
        sql = str(stmt)
        if "dou_seen" in sql:
            return [type("R", (), {"numero": n, "ano": a}) for n, a in self.seen]
        return [type("R", (), {"key": k}) for k in self.avisos]


def test_regua_conta_mp_anunciada_sem_nota() -> None:
    """Nota da 1399 recusada: a régua ficava em 1398, a sonda devolvia a 1399
    em toda janela e "sem novas" nunca ligava."""
    s = _SessaoConhecidas(seen=[("1398", 2026), ("1.397", 2026)], avisos=["1399/2026", "lixo"])
    assert asyncio.run(pl.ultimo_numero_entregue(s, 1)) == (1399, 2026)
    assert set(asyncio.run(pl.mps_conhecidas(s, 1))) == {1397, 1398, 1399}
