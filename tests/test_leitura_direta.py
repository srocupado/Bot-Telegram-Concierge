"""Sites que barram IP de datacenter são lidos DIRETO, sem passar pelo Jina.

Medido em 02/08/2026 com o Mercado Livre. Do IP deste ambiente (datacenter),
TODOS os caminhos caem em /gz/account-verification: navegador, curl, e os
user-agents de facebookexternalhit, WhatsApp, TelegramBot e Twitterbot. A API
com token de app devolve 403 em /items e /sites/MLB/search.

Do IP residencial do Orange Pi, a mesma URL abre normal.

A causa era essa: o `read_url` busca via Jina Reader, e quem faz a requisição
é o servidor do Jina — datacenter. O IP do bot nunca chegava a ser usado.
"""
from __future__ import annotations

import asyncio

import pytest

from bot.services import websearch as ws


class _Resp:
    def __init__(self, texto: str, url: str, status: int = 200):
        self.text = texto
        self.url = url
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"status {self.status_code}")


class _Client:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, _url):
        return self._resp


def _com_resposta(monkeypatch, resp):
    monkeypatch.setattr(ws.httpx, "AsyncClient", lambda **kw: _Client(resp))


PRODUTO = _Resp(
    "<html><head><title>Chave Magnética</title></head><body>"
    "<nav>menu que não interessa</nav>"
    "<h1>Chave Magnética Trifásica 2CV 220V com Relé 5-8A</h1>"
    "<p>Corrente nominal 9A. Tensão da bobina 220V. Relé térmico ajustável "
    "de 5 a 8 amperes. Indicada para motores de até 2 CV em 220V "
    "monofásico.</p>" + "<p>especificação adicional relevante</p>" * 20 +
    "</body></html>",
    "https://produto.mercadolivre.com.br/MLB-2779941281",
)

MURO = _Resp(
    "<html><body>Verifique sua conta para continuar</body></html>",
    "https://www.mercadolivre.com.br/gz/account-verification?go=x",
)


# ───────────────────────── roteamento por domínio ─────────────────────────

@pytest.mark.parametrize("url,esperado", [
    ("https://produto.mercadolivre.com.br/MLB-2779941281", True),
    ("https://www.mercadolivre.com.br/up/MLBU143?x=1", True),
    ("https://api.mercadolibre.com/items/X", True),
    ("https://www.magazineluiza.com.br/p/123", False),
    ("https://r.jina.ai/https://x.com", False),
])
def test_dominio_certo_vai_direto(url, esperado) -> None:
    assert ws._direto(url) is esperado


def test_dominio_parecido_nao_engana() -> None:
    """`mercadolivre.com.br.evil.com` não pode ativar o caminho direto —
    seria mandar o IP do bot pra um domínio de terceiro achando que é o ML."""
    assert ws._direto("https://mercadolivre.com.br.evil.com/x") is False
    assert ws._direto("https://naomercadolivre.com.br/x") is False


# ───────────────────────── leitura direta ─────────────────────────

def test_le_a_pagina_e_devolve_o_conteudo(monkeypatch) -> None:
    _com_resposta(monkeypatch, PRODUTO)
    texto = asyncio.run(ws._ler_direto("https://produto.mercadolivre.com.br/MLB-2779941281"))
    assert "Chave Magnética Trifásica 2CV" in texto
    assert "Relé térmico ajustável de 5 a 8 amperes" in texto.replace("\n", " ")


def test_script_e_nav_saem_do_texto(monkeypatch) -> None:
    _com_resposta(monkeypatch, PRODUTO)
    texto = asyncio.run(ws._ler_direto("https://produto.mercadolivre.com.br/MLB-2779941281"))
    assert "menu que não interessa" not in texto


def test_muro_de_login_vira_erro_explicito(monkeypatch) -> None:
    """Devolver o HTML do muro faria o LLM responder em cima de uma página de
    erro — inventando ficha técnica que não leu. Melhor falhar dizendo."""
    _com_resposta(monkeypatch, MURO)
    with pytest.raises(ws.WebSearchError, match="verificação/login"):
        asyncio.run(ws._ler_direto("https://produto.mercadolivre.com.br/MLB-1"))


def test_pagina_quase_vazia_vira_erro(monkeypatch) -> None:
    """200 com corpo mínimo é o outro disfarce do bloqueio."""
    _com_resposta(monkeypatch, _Resp("<html><body>ok</body></html>",
                                     "https://produto.mercadolivre.com.br/MLB-1"))
    with pytest.raises(ws.WebSearchError, match="quase vazia"):
        asyncio.run(ws._ler_direto("https://produto.mercadolivre.com.br/MLB-1"))


# ───────────────────────── integração no read_url ─────────────────────────

def test_read_url_do_ml_nao_toca_no_jina(monkeypatch) -> None:
    """O ponto do conserto: a requisição tem que sair do IP do bot."""
    chamou_jina = []

    async def _jina(*a, **kw):
        chamou_jina.append(1)
        return "não deveria ter vindo por aqui"

    monkeypatch.setattr(ws, "_jina_get", _jina)
    _com_resposta(monkeypatch, PRODUTO)

    saida = asyncio.run(ws.read_url("https://produto.mercadolivre.com.br/MLB-2779941281"))
    assert not chamou_jina, "link do ML foi pro Jina — volta a bater no muro"
    assert "Chave Magnética Trifásica 2CV" in saida
    assert saida.startswith("Conteúdo lido de https://produto.mercadolivre.com.br/")


def test_outros_sites_continuam_no_jina(monkeypatch) -> None:
    """A mudança é cirúrgica: quem funciona hoje não muda de caminho."""
    async def _jina(_client, url):
        return f"markdown do jina para {url}"

    monkeypatch.setattr(ws, "_jina_get", _jina)
    monkeypatch.setattr(ws.httpx, "AsyncClient", lambda **kw: _Client(PRODUTO))

    saida = asyncio.run(ws.read_url("https://www.magazineluiza.com.br/p/123"))
    assert "markdown do jina" in saida


def test_truncamento_avisa_nas_duas_vias() -> None:
    """O rodapé de página truncada é o que impede o modelo de concluir
    'não existe' a partir de um trecho — vale pro caminho direto também."""
    curto = ws._montar_saida("https://x", "abc", cortado=False)
    longo = ws._montar_saida("https://x", "abc", cortado=True)
    assert "TRUNCADA" not in curto
    assert "TRUNCADA" in longo


# ───────────── link de compartilhamento → URL canônica ─────────────

def test_link_de_compartilhamento_vira_canonico() -> None:
    """O caso real: o dono colou o link do botão "compartilhar" e o bot bateu
    no muro, mesmo lendo direto. A URL canônica do MESMO item abre normal do
    IP do Orange Pi — e o id vem dentro do próprio link compartilhado."""
    compartilhado = (
        "https://www.mercadolivre.com.br/up/MLBU1430518104?matt_tool=38524122"
        "&pdp_filters=item_id:MLB2779941281&ua=BQf#origin=share&wid=MLB2779941281"
    )
    assert ws.canonizar_ml(compartilhado) == "https://produto.mercadolivre.com.br/MLB-2779941281"


def test_id_de_catalogo_nao_e_confundido_com_item() -> None:
    """MLBU1430518104 é id de CATÁLOGO, não de item. Tratá-lo como item geraria
    uma URL inexistente, e o erro viraria 'produto não encontrado' — pior que o
    atual, porque parece resposta. Sem id de item, a URL fica como está."""
    so_catalogo = "https://www.mercadolivre.com.br/up/MLBU1430518104?x=1"
    assert ws.canonizar_ml(so_catalogo) == so_catalogo


def test_url_canonica_fica_intacta() -> None:
    u = "https://produto.mercadolivre.com.br/MLB-2779941281"
    assert ws.canonizar_ml(u) == u


def test_url_do_ml_sem_item_nao_e_reescrita() -> None:
    """Home, busca e ofertas não viram URL de produto inventada."""
    for u in ("https://www.mercadolivre.com.br/ofertas",
              "https://lista.mercadolivre.com.br/chave-magnetica"):
        assert ws.canonizar_ml(u) == u


def test_read_url_canoniza_antes_de_buscar(monkeypatch) -> None:
    """Ponta a ponta: o que vai pra rede é a URL canônica, não a compartilhada."""
    buscadas: list[str] = []

    async def _fake(url):
        buscadas.append(url)
        return "Chave Magnética 2CV 220V — corrente nominal 9A" * 10

    monkeypatch.setattr(ws, "_ler_direto", _fake)
    asyncio.run(ws.read_url(
        "https://www.mercadolivre.com.br/up/MLBU1430518104?pdp_filters=item_id:MLB2779941281"
    ))
    assert buscadas == ["https://produto.mercadolivre.com.br/MLB-2779941281"]
