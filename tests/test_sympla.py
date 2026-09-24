"""Retirada automática de ingresso na Sympla.

Pedido do dono (23/09/2026): toda quarta a Sympla libera 2 ingressos grátis
pro concerto da Orquestra Sinfônica. O evento só é PUBLICADO às 17h59 (URL
nova a cada semana), e a retirada abre às 18h00.

O que dá pra testar de verdade offline: a lógica de ESCOLHA do evento certo
entre os resultados da busca, a leitura do __NEXT_DATA__ (testada com o
FORMATO REAL extraído da página ao vivo durante o levantamento — inclusive
de eventos já encerrados que existem de fato), a matemática de janela
semanal, e a validação de credenciais. O que NÃO dá: a automação de
navegador em si — não existe evento aberto fora do minuto exato da
liberação pra gravar contra ele; ver o docstring de bot/services/sympla.py.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bot.services import sympla as sy

BRT = ZoneInfo("America/Sao_Paulo")


# ───────────────────── escolher_evento ─────────────────────

def _c(titulo, dias_a_partir_de_hoje=0, hoje=None, encerrado=False):
    hoje = hoje or date(2026, 9, 23)
    d = hoje + timedelta(days=dias_a_partir_de_hoje) if dias_a_partir_de_hoje is not None else None
    return sy.EventoCandidato(titulo=titulo, url=f"https://x/{titulo}",
                              data_inicio=d, encerrado=encerrado)


def test_acha_por_titulo_ignorando_acento_e_caixa() -> None:
    hoje = date(2026, 9, 23)
    cands = [_c("ORQUESTRA SINFÔNICA do Teatro Nacional Cláudio Santoro | Concerto X",
                dias_a_partir_de_hoje=1, hoje=hoje)]
    escolhido = sy.escolher_evento(cands, sy.SEARCH_URL and
                                   "Orquestra Sinfônica do Teatro Nacional Claudio Santoro",
                                   hoje)
    assert escolhido is not None
    assert "Concerto X" in escolhido.titulo


def test_ignora_evento_encerrado() -> None:
    hoje = date(2026, 9, 23)
    cands = [_c("Orquestra Sinfônica Teatro Nacional", dias_a_partir_de_hoje=0,
                hoje=hoje, encerrado=True)]
    assert sy.escolher_evento(cands, "Orquestra Sinfônica Teatro Nacional", hoje) is None


def test_ignora_titulo_que_nao_bate_com_a_query() -> None:
    """Não confunde com OUTRO evento no mesmo teatro (ex.: peça de teatro)."""
    hoje = date(2026, 9, 23)
    cands = [_c("Peça de Teatro no Teatro Nacional", dias_a_partir_de_hoje=1, hoje=hoje)]
    assert sy.escolher_evento(cands, "Orquestra Sinfônica Teatro Nacional Claudio Santoro",
                              hoje) is None


def test_tolera_um_dia_pra_tras_por_causa_de_fuso() -> None:
    """Publicado 17h59 de uma quarta que já virou quinta em UTC — não pode
    rejeitar por isso."""
    hoje = date(2026, 9, 23)
    cands = [_c("Orquestra Sinfônica Teatro Nacional", dias_a_partir_de_hoje=-1, hoje=hoje)]
    assert sy.escolher_evento(cands, "Orquestra Sinfônica Teatro Nacional", hoje) is not None


def test_rejeita_evento_de_uma_semana_atras() -> None:
    hoje = date(2026, 9, 23)
    cands = [_c("Orquestra Sinfônica Teatro Nacional", dias_a_partir_de_hoje=-7, hoje=hoje)]
    assert sy.escolher_evento(cands, "Orquestra Sinfônica Teatro Nacional", hoje) is None


def test_escolhe_o_mais_proximo_entre_varios_validos() -> None:
    hoje = date(2026, 9, 23)
    cands = [
        _c("Orquestra Sinfônica Teatro Nacional — daqui a 10 dias", 10, hoje),
        _c("Orquestra Sinfônica Teatro Nacional — amanhã", 1, hoje),
        _c("Orquestra Sinfônica Teatro Nacional — daqui a 3 dias", 3, hoje),
    ]
    escolhido = sy.escolher_evento(cands, "Orquestra Sinfônica Teatro Nacional", hoje)
    assert "amanhã" in escolhido.titulo


def test_sem_candidato_nenhum_devolve_none_sem_inventar() -> None:
    assert sy.escolher_evento([], "Orquestra Sinfônica", date(2026, 9, 23)) is None


def test_query_so_com_stopwords_nao_casa_qualquer_coisa() -> None:
    """Sem termos úteis (só 'de'/'do'/'da'), não pode virar match universal —
    seria escolher um evento às cegas."""
    hoje = date(2026, 9, 23)
    cands = [_c("Qualquer Evento Aleatório", 1, hoje)]
    assert sy.escolher_evento(cands, "de do da", hoje) is None


# ───────────────────── evento_da_hydration ─────────────────────
# Formatos REAIS extraídos da página ao vivo durante o levantamento de
# 23/09/2026 (evento 3149828 e 3577985), não inventados.

def test_le_evento_aberto_do_hydration_real() -> None:
    payload = {
        "props": {"pageProps": {"hydrationData": {"eventHydration": {"event": {
            "id": 3149828,
            "name": "Orquestra Sinfônica do Teatro Nacional Claudio Santoro",
            "startDate": "2025-10-09 20:00:00",
            "isClosed": False,
        }}}}}
    }
    ev = sy.evento_da_hydration("https://x/3149828", payload)
    assert ev is not None
    assert ev.data_inicio == date(2025, 10, 9)
    assert ev.encerrado is False


def test_le_evento_encerrado_do_hydration_real() -> None:
    """O 'Concerto das Nações' (id 3577985) — já aconteceu, isClosed=True."""
    payload = {
        "props": {"pageProps": {"hydrationData": {"eventHydration": {"event": {
            "id": 3577985,
            "name": "Orquestra Sinfônica do Teatro Nacional Claudio Santoro | Concerto das Nações",
            "startDate": "2026-09-17 20:00:00",
            "endDate": "2026-09-17 22:00:00",
            "published": True,
            "isClosed": True,
            "cancelled": False,
        }}}}}
    }
    ev = sy.evento_da_hydration("https://x/3577985", payload)
    assert ev is not None
    assert ev.encerrado is True
    assert ev.data_inicio == date(2026, 9, 17)


def test_payload_sem_o_formato_esperado_nao_estoura() -> None:
    assert sy.evento_da_hydration("https://x", {}) is None
    assert sy.evento_da_hydration("https://x", {"props": {}}) is None
    assert sy.evento_da_hydration("https://x", None) is None


def test_data_ilegivel_nao_derruba_o_resto() -> None:
    payload = {
        "props": {"pageProps": {"hydrationData": {"eventHydration": {"event": {
            "name": "X", "startDate": "lixo-nao-e-data", "isClosed": False,
        }}}}}
    }
    ev = sy.evento_da_hydration("https://x", payload)
    assert ev is not None
    assert ev.data_inicio is None


# ───────────────────── proxima_janela ─────────────────────

def test_antes_da_janela_na_mesma_quarta() -> None:
    # Quarta 23/09/2026, 10h — janela é 18h da MESMA quarta.
    agora = datetime(2026, 9, 23, 10, 0, tzinfo=BRT)
    alvo = sy.proxima_janela(agora, weekday=2, hora=18)
    assert alvo == datetime(2026, 9, 23, 18, 0, tzinfo=BRT)


def test_depois_da_janela_pula_pra_proxima_semana() -> None:
    agora = datetime(2026, 9, 23, 18, 30, tzinfo=BRT)  # quarta, já passou 18h
    alvo = sy.proxima_janela(agora, weekday=2, hora=18)
    assert alvo == datetime(2026, 9, 30, 18, 0, tzinfo=BRT)


def test_dia_diferente_da_semana() -> None:
    agora = datetime(2026, 9, 21, 12, 0, tzinfo=BRT)  # segunda
    alvo = sy.proxima_janela(agora, weekday=2, hora=18)
    assert alvo == datetime(2026, 9, 23, 18, 0, tzinfo=BRT)


def test_nunca_devolve_algo_no_passado() -> None:
    import random
    random.seed(42)
    for _ in range(200):
        agora = datetime(2026, 9, 23, tzinfo=BRT) + timedelta(
            hours=random.uniform(0, 24 * 10))
        alvo = sy.proxima_janela(agora, weekday=random.randint(0, 6),
                                 hora=random.randint(0, 23))
        assert alvo > agora


# ───────────────────── credenciais (kv_settings) ─────────────────────

def _sessionmaker():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    return async_sessionmaker(engine, expire_on_commit=False)


async def _com_tabelas(sm):
    from bot.db.models import Base
    async with sm.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def test_ciclo_completo_de_credenciais() -> None:
    async def _main():
        sm = _sessionmaker()
        await _com_tabelas(sm)
        async with sm() as s:
            assert await sy.get_credenciais(s) is None  # nada configurado
            await sy.save_email(s, "  vinicius@example.com  ")
            await sy.save_senha(s, "senha-forte-123")
            assert await sy.get_credenciais(s) is None  # falta o nome ainda
            await sy.save_nome(s, "Vinicius L Silva")
            creds = await sy.get_credenciais(s)
            assert creds is not None
            assert creds.email == "vinicius@example.com"
            assert creds.cpf is None
            await sy.save_cpf(s, "123.456.789-09")
            creds = await sy.get_credenciais(s)
            assert creds.cpf == "12345678909"
    asyncio.run(_main())


def test_email_invalido_recusado() -> None:
    async def _main():
        sm = _sessionmaker()
        await _com_tabelas(sm)
        async with sm() as s:
            with pytest.raises(sy.SymplaError):
                await sy.save_email(s, "nao-e-email")
    asyncio.run(_main())


def test_nome_sem_sobrenome_recusado() -> None:
    async def _main():
        sm = _sessionmaker()
        await _com_tabelas(sm)
        async with sm() as s:
            with pytest.raises(sy.SymplaError):
                await sy.save_nome(s, "Vinicius")
    asyncio.run(_main())


def test_cpf_com_digitos_errados_recusado() -> None:
    async def _main():
        sm = _sessionmaker()
        await _com_tabelas(sm)
        async with sm() as s:
            with pytest.raises(sy.SymplaError):
                await sy.save_cpf(s, "123")
    asyncio.run(_main())


def test_descrever_email_mascara_sem_reexibir() -> None:
    assert sy.descrever_email("vi@example.com") == "v•@example.com"
    masked = sy.descrever_email("vinicius@example.com")
    assert masked.startswith("v") and masked.endswith("s@example.com")
    assert "inicius" not in masked


# ───────────────────── help (regra do projeto) ─────────────────────

@pytest.mark.parametrize("frase", [
    "como configuro a sympla",
    "quero o ingresso da orquestra sinfonica",
    "como pego o ingresso do concerto",
])
def test_help_roteia_para_a_secao_sympla(frase: str) -> None:
    from bot.handlers.start import HELP_TEXT, find_help_sections

    assert "/sympla_setup" in HELP_TEXT
    assert "/sympla_testar" in HELP_TEXT
    secoes = find_help_sections(frase)
    assert any("sympla" in s.lower() for s in secoes), frase


# ───────────────── falha real de 23/09/2026: "Entrar" não existe ─────────────
# Primeiro uso real: TimeoutError esperando get_by_role("button", name=~entrar)
# — investigação contra os bundles JS baixados no levantamento mostrou que
# "Entrar" não existe como STRING em lugar nenhum do código da Sympla (o
# gatilho do header provavelmente é ícone sem texto, com rótulo vindo de
# tradução externa). A correção troca "adivinha um texto de botão" por "vai
# direto na rota oficial de login" (sympla.com.br/login redireciona pra
# /#login — confirmado contra o site real, não é chute) e só cai pra
# candidatos de texto como plano B.

class _ElementoFalso:
    def __init__(self, texto):
        self._texto = texto

    async def wait_for(self, **kw):
        pass


class _PaginaFalsa:
    """Dublê mínimo só pra exercitar _dump_clicaveis sem navegador real."""

    def __init__(self, textos):
        self._textos = textos

    async def eval_on_selector_all(self, _sel, _js):
        return self._textos


def test_dump_clicaveis_lista_e_deduplica() -> None:
    pagina = _PaginaFalsa(["Entrar", "Cadastrar", "Entrar", "  ", "Buscar"])
    out = asyncio.run(sy._dump_clicaveis(pagina))
    assert out == "Entrar | Cadastrar | Buscar"


def test_dump_clicaveis_sem_elementos_nao_finge_achar_algo() -> None:
    pagina = _PaginaFalsa([])
    out = asyncio.run(sy._dump_clicaveis(pagina))
    assert "nenhum elemento" in out


def test_dump_clicaveis_trunca_pra_nao_estourar_mensagem() -> None:
    pagina = _PaginaFalsa([f"Botão número {i} com texto longo" for i in range(200)])
    out = asyncio.run(sy._dump_clicaveis(pagina))
    assert len(out) <= 801  # 800 + "…"
    assert out.endswith("…")


def test_dump_clicaveis_pagina_que_estoura_nao_derruba_o_caller() -> None:
    class _Explode:
        async def eval_on_selector_all(self, *a):
            raise RuntimeError("página fechou no meio")
    out = asyncio.run(sy._dump_clicaveis(_Explode()))
    assert "não consegui listar" in out


def _sem_comentarios(src: str) -> str:
    """Remove comentários E a docstring da função — evita que uma asserção
    sobre o CÓDIGO passe (ou falhe) só porque a string aparece em texto que
    EXPLICA uma decisão, e não no código em si.

    Dois bugs reais neste arquivo de teste vieram daqui: a 1ª versão só
    filtrava linhas '#' e uma mutação que trocava o goto() continuava
    passando porque "/#login" sobrevivia no comentário acima; depois, uma
    asserção NEGATIVA ("location.hash" não deve aparecer) falhou porque a
    PRÓPRIA docstring explica, em prosa, por que aquilo foi removido —
    então o texto que documenta a correção acionava a checagem que existe
    pra impedir a regressão."""
    sem_hash = "\n".join(
        ln for ln in src.splitlines()
        if not ln.strip().startswith("#")
    )
    # Docstring = 1º bloco entre aspas triplas logo após a linha "def ...:".
    return re.sub(r'"""(?:.|\n)*?"""', "", sem_hash, count=1)


def test_login_carrega_pagina_limpa_e_delega_a_abertura() -> None:
    """2ª falha real (23/09/2026): a 1ª versão ia direto pra "…/#login" —
    mas o PRÓPRIO dump que ela trouxe na falha provou que isso não abre
    nada (nenhuma palavra de conta/login apareceu na tela). Motivo provável:
    o roteador só reage ao EVENTO hashchange, não ao hash já presente no
    carregamento inicial. Agora a página carrega LIMPA e quem muda o hash
    é _abrir_login, depois de montada — ver os testes dela abaixo."""
    import inspect
    src = _sem_comentarios(inspect.getsource(sy._login))
    assert '_ir(page, BASE_URL)' in src
    assert '"{BASE_URL}/#login"' not in src, "voltou a carregar já com o hash"
    assert "_abrir_login(page)" in src


def test_login_usa_tipo_de_input_nao_label_ou_classe_css() -> None:
    """Rótulo ARIA e classe CSS são coisas que a Sympla controla e pode
    trocar a qualquer deploy; tipo de input é HTML semântico padrão."""
    import inspect
    src = inspect.getsource(sy._login)
    assert "input[type=email]" in src
    assert "get_by_label" not in src, "voltou a depender de rótulo ARIA"


def test_abrir_login_nao_confia_mais_em_hash() -> None:
    """3ª reescrita (23/09/2026): fui direto no JS real da Sympla e
    "openSignInModal" é método de um store que só mexe em flag interna
    (setModalOpen) — SEM nenhuma referência a location.hash por perto. As
    duas tentativas anteriores miravam essa hipótese errada (por isso o
    dump saiu IDÊNTICO nas duas: a mudança de hash não fazia NADA). Guarda
    de regressão: não pode voltar a depender disso."""
    import inspect
    src = _sem_comentarios(inspect.getsource(sy._abrir_login))
    assert "location.hash" not in src
    assert "hashchange" not in src


def test_abrir_login_tenta_a_pista_open_dropdown() -> None:
    """A pista concreta do dump real: "Open Dropdown" é texto em inglês
    solto numa tela em português — rótulo PADRÃO do Radix UI (confirmado no
    HTML real: id="radix-...", aria-haspopup="menu", logo após o botão
    id="btn-my-tickets") quando ninguém customiza."""
    import inspect
    src = inspect.getsource(sy._abrir_login)
    assert "open dropdown" in src.lower()


_POS = {"gatilho": (10, 10), "opcao": (100, 100), "senha": (100, 200),
        "fundo": (500, 500)}


class _LocFalso:
    """Sem método click(): o código TEM que clicar pelo mouse (o click() do
    locator trava no teste de "estável" numa CPU lenta — log real do Pi)."""

    def __init__(self, pagina, nome):
        self._p = pagina
        self._nome = nome

    @property
    def first(self):
        return self

    async def is_visible(self):
        return self._p.visivel(self._nome)

    async def wait_for(self, state="visible", timeout=None):
        if not self._p.visivel(self._nome):
            raise TimeoutError(f"{self._nome} não apareceu")

    async def bounding_box(self, timeout=None):
        x, y = _POS[self._nome]
        return {"x": x - 5, "y": y - 5, "width": 10, "height": 10}


class _MouseFalso:
    def __init__(self, pagina):
        self._p = pagina

    async def click(self, x, y):
        alvo = next(n for n, pos in _POS.items() if pos == (x, y))
        self._p.clique(alvo)


class _PaginaLoginFalsa:
    """Reproduz o que foi VISTO ao vivo (24/09/2026): antes do React
    hidratar, clicar no "Open Dropdown" não faz nada; depois, abre direto o
    modal (sem menu), e "e-mail e senha" revela o formulário. Clique em
    qualquer lugar com o modal aberto que não seja a opção cai no fundo
    escuro e FECHA o modal (a corrida vista com a CPU limitada 6x)."""

    def __init__(self, hidrata_no_clique=1, modal_ja_aberto=False):
        self.hidrata_no_clique = hidrata_no_clique
        self.cliques_gatilho = 0
        self.modal_aberto = modal_ja_aberto
        self.form_aberto = False
        self.eventos: list[str] = []
        self.mouse = _MouseFalso(self)

    def visivel(self, nome):
        return {"gatilho": True, "opcao": self.modal_aberto and not self.form_aberto,
                "senha": self.form_aberto}[nome]

    def clique(self, alvo):
        self.eventos.append(alvo)
        if self.modal_aberto and alvo != "opcao":
            self.modal_aberto = False  # caiu no fundo escuro
            return
        if alvo == "gatilho":
            self.cliques_gatilho += 1
            if self.cliques_gatilho >= self.hidrata_no_clique:
                self.modal_aberto = True
        elif alvo == "opcao":
            self.form_aberto = True

    def get_by_role(self, role, name=None):
        assert role == "button"
        if name.search("Open Dropdown"):
            return _LocFalso(self, "gatilho")
        if name.search("Continuar com e-mail e senha"):
            return _LocFalso(self, "opcao")
        raise AssertionError(name)

    def locator(self, seletor):
        assert "password" in seletor
        return _LocFalso(self, "senha")

    async def eval_on_selector_all(self, *a):
        return ["Open Dropdown"]


def test_abrir_login_caminho_visto_ao_vivo() -> None:
    pagina = _PaginaLoginFalsa()
    asyncio.run(sy._abrir_login(pagina))
    assert pagina.eventos == ["gatilho", "opcao"]
    assert pagina.form_aberto


def test_abrir_login_reclica_ate_a_pagina_hidratar() -> None:
    """Reproduzido ao vivo com o MESMO dump da produção: o 1º clique vinha
    antes da hidratação e não fazia nada."""
    pagina = _PaginaLoginFalsa(hidrata_no_clique=4)
    asyncio.run(sy._abrir_login(pagina))
    assert pagina.cliques_gatilho == 4
    assert pagina.form_aberto


def test_abrir_login_com_modal_ja_aberto_nao_clica_no_gatilho() -> None:
    """A corrida vista com a CPU limitada 6x: o clique anterior abriu o
    modal ATRASADO. Clicar no gatilho de novo cairia no fundo e fecharia o
    modal — tem que olhar a tela e ir direto na opção."""
    pagina = _PaginaLoginFalsa(modal_ja_aberto=True)
    asyncio.run(sy._abrir_login(pagina))
    assert pagina.eventos == ["opcao"]


def test_abrir_login_desiste_com_diagnostico_se_nunca_hidratar(monkeypatch) -> None:
    monkeypatch.setattr(sy, "LOGIN_ABRIR_TIMEOUT_S", 0.05)
    pagina = _PaginaLoginFalsa(hidrata_no_clique=10**9)
    with pytest.raises(sy.SymplaError, match="Elementos visíveis"):
        asyncio.run(sy._abrir_login(pagina))
    assert pagina.cliques_gatilho >= 1


def test_nenhum_clique_usa_locator_click() -> None:
    """Log real do Pi: locator.click() ficou 10s em "waiting for element to
    be visible, enabled and stable". Todo clique passa por _clicar."""
    import inspect
    src = _sem_comentarios(inspect.getsource(sy))
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    assert ".click(" not in src.replace("page.mouse.click(", "")


def test_abrir_login_nao_aperta_escape_nem_procura_menu() -> None:
    """Visto ao vivo: não existe menu; o Escape das versões anteriores
    FECHAVA o modal que o clique certo tinha acabado de abrir."""
    import inspect
    src = _sem_comentarios(inspect.getsource(sy._abrir_login))
    assert "Escape" not in src
    assert "menuitem" not in src


def test_login_nunca_usa_input_text_generico() -> None:
    """Visto ao vivo: a busca "Buscar experiências" (type=text) vem antes
    do modal no DOM — o seletor antigo digitava o e-mail nela."""
    import inspect
    src = _sem_comentarios(inspect.getsource(sy._login))
    assert "input[type=text]" not in src
    assert 'input[type=email]:visible' in src


# ───────────── bug real: screenshot de falha nunca chegava ─────────────
# Dono, 23/09/2026, depois de reproduzir a falha: "Não mandou nada fora essa
# msg" — nenhum print chegou em NENHUMA das três falhas reais até agora. A
# causa: `finally: await browser.close()` fechava o navegador ANTES da
# exceção alcançar o except de fora, e page.screenshot() numa página já
# fechada estourava e era engolido em silêncio DENTRO do próprio
# _screenshot_seguro — a mensagem de erro chegava, o print nunca.

class _FakePageScreenshot:
    def __init__(self, ordem: list[str]):
        self._ordem = ordem
        self.closed = False

    async def screenshot(self, **kw):
        if self.closed:
            raise RuntimeError("página já fechada — Playwright recusaria isto")
        self._ordem.append("screenshot")
        return b"PNGDATA"

    async def eval_on_selector_all(self, *a, **kw):
        return []


def _fake_playwright_module(page, ordem: list[str]):
    """Dublê mínimo de `playwright.async_api` — só o suficiente pra exercitar
    a ORDEM screenshot-antes-do-close, sem precisar de navegador real."""
    import types

    class _Ctx:
        async def new_page(self):
            return page

    class _Browser:
        async def new_context(self, **kw):
            return _Ctx()

        async def close(self):
            ordem.append("close")
            page.closed = True

    class _Chromium:
        async def launch(self, **kw):
            return _Browser()

    class _PlaywrightCtx:
        def __init__(self):
            self.chromium = _Chromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    return types.SimpleNamespace(async_playwright=lambda: _PlaywrightCtx())


def test_screenshot_de_falha_e_capturada_antes_do_browser_fechar(monkeypatch) -> None:
    import sys

    ordem: list[str] = []
    page = _FakePageScreenshot(ordem)
    monkeypatch.setitem(sys.modules, "playwright.async_api",
                        _fake_playwright_module(page, ordem))

    async def _login_que_falha(_page, _creds):
        raise RuntimeError("falha proposital deste teste")
    monkeypatch.setattr(sy, "_login", _login_que_falha)

    creds = sy.SymplaCredenciais(email="x@x.com", senha="1234x",
                                 nome_completo="X Y", cpf=None)
    resultado = asyncio.run(sy.retirar_ingresso(creds, "query", 2))

    assert resultado.sucesso is False
    assert resultado.screenshot == b"PNGDATA", (
        "screenshot não chegou — o bug de 23/09/2026 voltou"
    )
    assert ordem == ["screenshot", "close"], (
        f"ordem errada: {ordem} — screenshot TEM que vir antes do close"
    )


def test_resultado_sem_falha_no_login_tambem_tras_o_dump_de_elementos(monkeypatch) -> None:
    """Falha em QUALQUER etapa depois do login (não só nela) também precisa
    do screenshot vivo — o bug não era específico do _login."""
    import sys

    ordem: list[str] = []
    page = _FakePageScreenshot(ordem)
    monkeypatch.setitem(sys.modules, "playwright.async_api",
                        _fake_playwright_module(page, ordem))

    async def _login_ok(_page, _creds):
        return None
    monkeypatch.setattr(sy, "_login", _login_ok)

    async def _localizar_que_falha(*a, **kw):
        raise RuntimeError("falha na busca do evento, proposital")
    monkeypatch.setattr(sy, "_localizar_evento", _localizar_que_falha)

    creds = sy.SymplaCredenciais(email="x@x.com", senha="1234x",
                                 nome_completo="X Y", cpf=None)
    resultado = asyncio.run(sy.retirar_ingresso(creds, "query", 2))

    assert resultado.sucesso is False
    assert resultado.screenshot == b"PNGDATA"
    assert "elementos visíveis" in resultado.detalhe


def test_abrir_login_sem_estrategia_nenhuma_reporta_com_diagnostico() -> None:
    """Falha de TODAS as estratégias tem que vir com o dump — é a diferença
    entre 'estourou de novo' e 'aqui está o texto real da tela'."""
    import inspect
    src = inspect.getsource(sy._abrir_login)
    assert "_dump_clicaveis" in src
    assert "SymplaError" in src


def test_falha_fora_do_login_tambem_carrega_o_dump() -> None:
    import inspect
    src = inspect.getsource(sy.retirar_ingresso)
    assert "_dump_clicaveis" in src
    assert "isinstance(exc, SymplaError)" in src, (
        "sem essa checagem o dump do login duplicaria dentro dele mesmo"
    )


# ───────────── 1º teste real depois do login novo (24/09/2026) ─────────────

def test_busca_usa_o_parametro_real_s_e_nao_q() -> None:
    """Visto ao vivo digitando na caixa do site: a busca é /eventos?s=...
    Com "q=" a página ignorava o termo e listava eventos genéricos de
    Brasília (Rock Night, Bailão...) — o concerto nunca seria achado."""
    url = sy.SEARCH_URL.format(query="x")
    assert url == "https://www.sympla.com.br/eventos?s=x"


def test_teste_manual_faz_uma_busca_so_e_avisa_cada_etapa(monkeypatch) -> None:
    import sys

    ordem: list[str] = []
    page = _FakePageScreenshot(ordem)
    monkeypatch.setitem(sys.modules, "playwright.async_api",
                        _fake_playwright_module(page, ordem))

    async def _login_ok(_page, _creds):
        return None
    monkeypatch.setattr(sy, "_login", _login_ok)

    buscas = []

    async def _localizar_vazio(*a, **kw):
        buscas.append(1)
        return None
    monkeypatch.setattr(sy, "_localizar_evento", _localizar_vazio)

    etapas: list[str] = []

    async def _on_etapa(e):
        etapas.append(e)

    creds = sy.SymplaCredenciais("x@x.com", "1234x", "X Y", None)
    r = asyncio.run(sy.retirar_ingresso(
        creds, "query", 2, poll_timeout_s=0, on_etapa=_on_etapa))

    assert buscas == [1]
    assert etapas == ["login", "localizar o evento da semana"]
    assert r.sucesso is False and "busca única" in r.detalhe


def test_progresso_que_falha_nao_derruba_a_retirada(monkeypatch) -> None:
    import sys

    ordem: list[str] = []
    page = _FakePageScreenshot(ordem)
    monkeypatch.setitem(sys.modules, "playwright.async_api",
                        _fake_playwright_module(page, ordem))

    async def _login_ok(_page, _creds):
        return None
    monkeypatch.setattr(sy, "_login", _login_ok)

    async def _localizar_vazio(*a, **kw):
        return None
    monkeypatch.setattr(sy, "_localizar_evento", _localizar_vazio)

    async def _on_etapa(e):
        raise RuntimeError("Telegram fora")

    creds = sy.SymplaCredenciais("x@x.com", "1234x", "X Y", None)
    r = asyncio.run(sy.retirar_ingresso(
        creds, "query", 2, poll_timeout_s=0, on_etapa=_on_etapa))
    assert r.etapa == "localizar o evento da semana"


class _AvisoFalso:
    def __init__(self, log):
        self._log = log

    async def edit_text(self, texto, parse_mode=None):
        if parse_mode == "HTML":
            import re as _re
            # Telegram recusa tag desconhecida — é o que matava a resposta.
            for tag in _re.findall(r"</?([a-zA-Z]+)", texto):
                if tag not in ("b", "i", "code", "pre", "a"):
                    raise RuntimeError(f"can't parse entities: <{tag}>")
        self._log.append(("edit", texto))


class _MensagemFalsa:
    def __init__(self):
        self.log: list = []

    async def answer(self, texto, parse_mode=None):
        self.log.append(("answer", texto))
        return _AvisoFalso(self.log)

    async def answer_photo(self, *a, **kw):
        self.log.append(("photo",))


def _rodar_testar(monkeypatch, fake_retirar):
    from bot.handlers import sympla as h
    import types

    async def _creds(_s):
        return sy.SymplaCredenciais("x@x.com", "1234x", "X Y", None)
    monkeypatch.setattr(h, "get_credenciais", _creds)
    monkeypatch.setattr(h, "retirar_ingresso", fake_retirar)
    monkeypatch.setattr(h, "_is_owner", lambda u: True)
    msg = _MensagemFalsa()
    user = types.SimpleNamespace(is_authorized=True)
    asyncio.run(h.cmd_testar(msg, user, None))
    return msg.log


def test_testar_escapa_html_do_detalhe(monkeypatch) -> None:
    """Detalhe de falha do Playwright traz HTML cru ("<input ...>"); sem
    escape o Telegram recusa a mensagem e o dono fica sem resposta."""
    async def _retirar(*a, **kw):
        return sy.SymplaResultado(
            False, "login",
            'TimeoutError: resolved to visible <input type="password">',
            screenshot=b"PNG")

    log = _rodar_testar(monkeypatch, _retirar)
    edits = [t for k, *t in log if k == "edit"]
    assert edits and "&lt;input" in edits[-1][0]
    assert ("photo",) in log


def test_testar_nunca_morre_calado(monkeypatch) -> None:
    async def _retirar(*a, **kw):
        raise RuntimeError("playwright sumiu")

    log = _rodar_testar(monkeypatch, _retirar)
    assert any(k == "answer" and "quebrou" in t[0] for k, *t in log)


def test_busca_que_nao_carregou_nao_vira_nao_achei(monkeypatch) -> None:
    """Com a CPU limitada 12x os resultados levaram 13,4s; o timeout antigo
    (10s) devolvia lista vazia → "não achei nenhum evento". Busca que não
    carregou tem que ser reportada como "não consegui checar"."""
    import sys

    ordem: list[str] = []
    page = _FakePageScreenshot(ordem)
    monkeypatch.setitem(sys.modules, "playwright.async_api",
                        _fake_playwright_module(page, ordem))

    async def _login_ok(_page, _creds):
        return None
    monkeypatch.setattr(sy, "_login", _login_ok)

    async def _busca_lenta(*a, **kw):
        raise sy.SymplaError("a busca da Sympla não terminou de carregar em 60s")
    monkeypatch.setattr(sy, "_localizar_evento", _busca_lenta)

    creds = sy.SymplaCredenciais("x@x.com", "1234x", "X Y", None)
    r = asyncio.run(sy.retirar_ingresso(creds, "q", 2, poll_timeout_s=0))
    assert r.sucesso is False
    assert "não consegui checar" in r.detalhe
    assert "não achei" not in r.detalhe


# ───────────── Cloudflare no Pi (print real, 23/09/2026) ─────────────

class _ContaFalsa:
    def __init__(self, fn):
        self._fn = fn

    async def count(self):
        return self._fn()


class _PaginaCloudflareFalsa:
    """O desafio fica na tela por `some_apos` checagens e depois libera."""

    def __init__(self, some_apos):
        self.checagens = 0
        self.some_apos = some_apos

    def _cf(self):
        self.checagens += 1
        return 1 if self.checagens <= self.some_apos else 0

    def get_by_text(self, padrao):
        assert padrao.search("Executando verificação de segurança")
        return _ContaFalsa(self._cf)

    async def wait_for_timeout(self, ms):
        pass

    async def wait_for_load_state(self, *a):
        pass


def test_cloudflare_que_libera_sozinho_so_espera() -> None:
    pagina = _PaginaCloudflareFalsa(some_apos=3)
    asyncio.run(sy._passar_cloudflare(pagina))
    assert pagina.checagens == 4


def test_cloudflare_que_nao_libera_vira_falha_explicita(monkeypatch) -> None:
    monkeypatch.setattr(sy, "CLOUDFLARE_ESPERA_S", 0.05)
    pagina = _PaginaCloudflareFalsa(some_apos=10**9)
    with pytest.raises(sy.SymplaError, match="Cloudflare"):
        asyncio.run(sy._passar_cloudflare(pagina))


def test_pagina_de_evento_ilegivel_nao_e_pulada_em_silencio(monkeypatch) -> None:
    """Antes: hydration ausente → None → o evento era pulado e o resultado
    final virava "não achei nenhum evento". Com a página do Cloudflare no
    lugar da Sympla, era assim que o concerto podia sumir."""
    async def _ir(*a, **kw):
        return None

    async def _sem_hydration(_page):
        return None
    monkeypatch.setattr(sy, "_ir", _ir)
    monkeypatch.setattr(sy, "_ler_hydration", _sem_hydration)
    cand = sy.EventoCandidato("Orquestra", "https://x/evento/1", None, False)
    with pytest.raises(sy.SymplaError, match="não consegui ler"):
        asyncio.run(sy._detalhar(None, cand))


def test_cloudflare_depois_do_entrar_nao_conta_como_login_feito(monkeypatch) -> None:
    """O login dava como concluído quando o campo de senha sumia — e ele
    some quando o Cloudflare cobre a tela. Cenário: desafio por 2 checagens,
    depois volta o modal com a recusa da Sympla. Tem que dar a recusa."""
    async def _nada(*a, **kw):
        return None
    monkeypatch.setattr(sy, "_ir", _nada)
    monkeypatch.setattr(sy, "_abrir_login", _nada)
    monkeypatch.setattr(sy, "_clicar", _nada)
    monkeypatch.setattr(sy, "_passar_cloudflare", _nada)

    estado = {"t": 0}

    def cf():
        return 1 if estado["t"] < 2 else 0

    def recusa():
        return 1 if estado["t"] >= 2 else 0

    class _Campo:
        first = property(lambda self: self)

        async def fill(self, *a, **kw):
            pass

        async def is_visible(self):
            return estado["t"] >= 2

    class _Pagina:
        def locator(self, _sel):
            return _Campo()

        def get_by_role(self, *a, **kw):
            return _Campo()

        def get_by_text(self, padrao):
            if padrao.search("E-mail ou senha inválidos"):
                return _ContaFalsa(recusa)
            return _ContaFalsa(cf)

        async def wait_for_timeout(self, ms):
            estado["t"] += 1

    async def _cf_na_tela(_page):
        return bool(cf())
    monkeypatch.setattr(sy, "_cloudflare_na_tela", _cf_na_tela)

    async def _cf_passa(_page):
        estado["t"] += 1
    monkeypatch.setattr(sy, "_passar_cloudflare", _cf_passa)

    creds = sy.SymplaCredenciais("x@x.com", "1234x", "X Y", None)
    with pytest.raises(sy.SymplaError, match="recusou"):
        asyncio.run(sy._login(_Pagina(), creds))
