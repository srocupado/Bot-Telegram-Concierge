"""Checkout em etapas (prints do dono, 22/10/2026).

Os dois prints mostraram o bot no checkout como VISITANTE: um com o modal
"Que bom ter você aqui! Você precisa estar logado para acessar este evento"
por cima de "Dados do comprador"; outro com "Recebimento do ingresso" (Nome,
Sobrenome, E-mail, Confirmação do e-mail) → "Próximo" → "2 Finalização". O
código antigo esperava um "Finalizar" na 1ª tela e nunca clicava em
"Próximo"; o login era dado como feito só porque o campo de senha sumiu.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from bot.services import sympla as sy

CREDS = sy.SymplaCredenciais("dono@x.com", "s", "Vinicius de Tal", "12345678900")


class _Loc:
    def __init__(self, pagina, alvo):
        self.p, self.alvo = pagina, alvo

    @property
    def first(self):
        return self

    async def is_visible(self):
        return self.p.visivel(self.alvo)

    async def inner_text(self):
        return self.alvo

    async def input_value(self, timeout=None):
        return self.p.campos[self.alvo]

    async def fill(self, valor, timeout=None):
        self.p.campos[self.alvo] = valor


class _Checkout:
    ROTULOS = ("Nome *", "Sobrenome *", "E-mail *", "Confirmação do e-mail *")

    def __init__(self, modal=False, extra_obrigatorio=None, confirma=True,
                 prefill=None):
        self.modal = modal
        self.etapa = "recebimento"
        self.campos = {r: "" for r in self.ROTULOS}
        if extra_obrigatorio:
            self.campos[extra_obrigatorio] = ""
        self.extra = extra_obrigatorio
        self.campos.update(prefill or {})
        self.confirma = confirma
        self.cliques: list[str] = []
        self.url = "https://www.sympla.com.br/checkout/abc"

    def visivel(self, alvo):
        if alvo == "modal":
            return self.modal
        if alvo == "Próximo":
            return self.etapa == "recebimento" and not self.modal
        if alvo == "Finalizar inscrição":
            return self.etapa == "finalizacao"
        if alvo == "Pedido confirmado!":
            return self.etapa == "confirmado"
        return self.etapa == "recebimento" and not self.modal and alvo in self.campos

    def get_by_text(self, padrao):
        if padrao.search("Que bom ter você aqui!"):
            return _Loc(self, "modal")
        if padrao.search("Pedido confirmado!"):
            return _Loc(self, "Pedido confirmado!")
        return _Loc(self, "-")

    def get_by_role(self, role, name=None):
        for b in ("Próximo", "Finalizar inscrição"):
            if name.search(b):
                return _Loc(self, b)
        return _Loc(self, "-")

    def get_by_label(self, padrao):
        for r in self.campos:
            if padrao.search(r):
                return _Loc(self, r)
        return _Loc(self, "-")

    async def wait_for_timeout(self, _ms):
        return None

    async def screenshot(self, **kw):
        return b"PRINT"

    async def eval_on_selector_all(self, *_a):
        return [r.rstrip(" *") for r, v in self.campos.items() if not v]

    def clicar(self, alvo):
        self.cliques.append(alvo)
        if alvo == "Próximo" and all(self.campos.values()):
            self.etapa = "finalizacao"
        elif alvo == "Finalizar inscrição" and self.confirma:
            self.etapa = "confirmado"


@pytest.fixture(autouse=True)
def _rapido(monkeypatch):
    async def _clicar(page, loc, timeout_ms=10_000):
        page.clicar(loc.alvo)
    monkeypatch.setattr(sy, "_clicar", _clicar)
    monkeypatch.setattr(sy, "CHECKOUT_CONFIRMA_S", 0.05)


def test_fluxo_visto_nos_prints_chega_a_confirmacao() -> None:
    p = _Checkout()
    prova = asyncio.run(sy._preencher_checkout(p, CREDS))
    assert prova == "Pedido confirmado!"
    assert p.campos["Nome *"] == "Vinicius"
    assert p.campos["Sobrenome *"] == "de Tal"
    assert p.campos["Confirmação do e-mail *"] == "dono@x.com"
    assert p.cliques == ["Próximo", "Finalizar inscrição"]


def test_modal_de_login_no_checkout_faz_login_ali(monkeypatch) -> None:
    entrou = []

    async def _entrar(page, creds, *_a):
        entrou.append(creds.email)
        page.modal = False
    monkeypatch.setattr(sy, "_entrar", _entrar)
    p = _Checkout(modal=True)
    assert asyncio.run(sy._preencher_checkout(p, CREDS)) == "Pedido confirmado!"
    assert entrou == ["dono@x.com"]


def test_campo_que_o_bot_nao_tem_e_dito_na_mensagem() -> None:
    """Opção (b) do dono: data de nascimento/CEP não são inventados — a
    mensagem lista o que ficou vazio."""
    p = _Checkout(extra_obrigatorio="Data de nascimento *")
    with pytest.raises(sy.SymplaError) as e:
        asyncio.run(sy._preencher_checkout(p, CREDS))
    assert "Data de nascimento" in str(e.value)
    assert p.cliques.count("Próximo") == sy.CHECKOUT_MAX_PROXIMOS
    assert "Finalizar inscrição" not in p.cliques


def test_sem_confirmacao_na_tela_nao_diz_que_retirou() -> None:
    p = _Checkout(confirma=False)
    with pytest.raises(sy.SymplaError) as e:
        asyncio.run(sy._preencher_checkout(p, CREDS))
    assert "PODE ter dado certo" in str(e.value) and "Meus ingressos" in str(e.value)


def test_dado_ja_preenchido_pela_conta_nao_e_sobrescrito() -> None:
    p = _Checkout(prefill={"Nome *": "Vini"})
    asyncio.run(sy._preencher_checkout(p, CREDS))
    assert p.campos["Nome *"] == "Vini"


def test_nome_nao_casa_com_sobrenome() -> None:
    assert not re.compile(r"^\s*nome\b", re.I).search("Sobrenome *")


# ───────────── login conferido, sessão e código por e-mail ─────────────

class _LocH:
    def __init__(self, h, alvo):
        self.h, self.alvo = h, alvo

    @property
    def first(self):
        return self

    async def wait_for(self, **kw):
        if not self.h.visivel(self.alvo):
            raise TimeoutError(self.alvo)

    async def is_visible(self):
        return self.h.visivel(self.alvo)

    async def count(self):
        return 6 if self.alvo == "caixas" else int(self.h.visivel(self.alvo))

    def nth(self, _i):
        return self

    async def fill(self, valor, timeout=None):
        self.h.preenchido.append((self.alvo, valor))


class _Home:
    """Botão da conta: deslogado abre o modal; logado não. `hidrata_no`:
    cliques antes disso não fazem nada (React ainda não hidratou)."""

    def __init__(self, logado, hidrata_no=1, tem_botao=True, sem_menu=False):
        self.sem_menu = sem_menu
        self.logado, self.hidrata_no, self.tem_botao = logado, hidrata_no, tem_botao
        self.modal, self.menu, self.cliques, self.preenchido = False, False, [], []
        self.tela_codigo, self.digitado, self.codigo_certo = False, "", "123456"
        self.keyboard = self

    async def press(self, _k):
        self.modal = self.menu = False

    async def type(self, texto, delay=None):
        self.digitado = texto

    def get_by_role(self, role, name=None):
        if name.search("Continuar"):
            return _LocH(self, "continuar")
        if name.search("Entrar"):
            return _LocH(self, "entrar")
        return _LocH(self, "conta")

    def get_by_text(self, padrao):
        if padrao.search("Digite o código que enviamos"):
            return _LocH(self, "tela_codigo")
        if padrao.search("Código inválido"):
            return _LocH(self, "recusa")
        if padrao.search("Senha inválida"):
            return _LocH(self, "-")
        if padrao.search("Sair"):
            return _LocH(self, "sair")
        return _LocH(self, "modal")

    def locator(self, sel):
        if "password" in sel:
            return _LocH(self, "senha")
        if "email" in sel:
            return _LocH(self, "email")
        return _LocH(self, "caixas")

    def visivel(self, alvo):
        return {"modal": self.modal, "sair": self.menu, "conta": self.tem_botao,
                "tela_codigo": self.tela_codigo, "continuar": self.tela_codigo,
                "recusa": self.tela_codigo and self.digitado not in ("", self.codigo_certo),
                "caixas": self.tela_codigo, "senha": False}.get(alvo, False)

    def clicar(self, alvo):
        self.cliques.append(alvo)
        if alvo == "conta" and self.cliques.count("conta") >= self.hidrata_no:
            self.modal = not self.logado
            self.menu = self.logado and not self.sem_menu
        elif alvo == "entrar":
            self.tela_codigo = True
        elif alvo == "continuar" and self.digitado == self.codigo_certo:
            self.tela_codigo, self.logado = False, True

    async def wait_for_timeout(self, _ms):
        return None

    async def screenshot(self, **kw):
        return b"PRINT"


@pytest.fixture
def _sem_navegar(monkeypatch):
    async def _ir(page, url, timeout_ms=0):
        return None
    monkeypatch.setattr(sy, "_ir", _ir)


def test_login_que_nao_pegou_falha_dizendo_isso(_sem_navegar) -> None:
    with pytest.raises(sy.SymplaError) as e:
        asyncio.run(sy._conferir_login(_Home(logado=False)))
    assert "o login não pegou" in str(e.value)
    assert e.value.print_meio == b"PRINT"


def test_sem_botao_da_conta_nao_passa_mais_calado(_sem_navegar) -> None:
    """23/10: na tela do código não há botão da conta, e o conferente antigo
    dava o login como certo."""
    with pytest.raises(sy.SymplaError):
        asyncio.run(sy._conferir_login(_Home(logado=False, tem_botao=False)))


def test_clique_antes_de_hidratar_nao_vira_logado(_sem_navegar) -> None:
    h = _Home(logado=False, hidrata_no=2)
    assert asyncio.run(sy._sessao_ativa(h)) is False


def test_login_que_pegou_segue(_sem_navegar) -> None:
    """Prova positiva vista no print do dono: menu da conta com "Sair"."""
    h = _Home(logado=True)
    asyncio.run(sy._conferir_login(h))
    assert h.cliques.count("conta") == 1


def test_logado_so_com_sair_na_tela(_sem_navegar) -> None:
    """Nem modal nem "Sair" depois de 3 cliques: não afirma sessão."""
    h = _Home(logado=True, sem_menu=True)
    assert asyncio.run(sy._sessao_ativa(h)) is False
    assert h.cliques.count("conta") == 3


def test_logado_que_hidrata_tarde_ainda_e_reconhecido(_sem_navegar) -> None:
    h = _Home(logado=True, hidrata_no=2)
    assert asyncio.run(sy._sessao_ativa(h)) is True


def test_sessao_salva_valida_nao_pede_login(_sem_navegar, monkeypatch) -> None:
    async def _entrar(*a):
        raise AssertionError("não devia entrar")
    monkeypatch.setattr(sy, "_entrar", _entrar)
    assert asyncio.run(sy._login(_Home(logado=True), CREDS)) is False


# código por e-mail (print do dono, 23/10/2026)

def _pedidor(respostas, pedidos):
    async def _pedir(tentativa):
        pedidos.append(tentativa)
        if respostas:
            resp = respostas.pop(0)
            asyncio.get_running_loop().call_later(
                0.01, lambda: sy.entregar_codigo(resp))
    return _pedir


def _entrar_com_codigo(monkeypatch, h, pedir):
    async def _nada(*a, **kw):
        return None

    async def _sem_cf(_p):
        return False
    monkeypatch.setattr(sy, "_abrir_login", _nada)
    monkeypatch.setattr(sy, "_cloudflare_na_tela", _sem_cf)
    asyncio.run(sy._entrar(h, CREDS, pedir))


def test_tela_do_codigo_pede_ao_dono_e_digita(monkeypatch) -> None:
    h, pedidos = _Home(logado=False), []
    _entrar_com_codigo(monkeypatch, h, _pedidor(["123 456"], pedidos))
    assert pedidos == [1] and h.digitado == "123456" and h.logado
    assert "continuar" in h.cliques


def test_codigo_errado_pede_de_novo(monkeypatch) -> None:
    h, pedidos = _Home(logado=False), []
    _entrar_com_codigo(monkeypatch, h, _pedidor(["999999", "123456"], pedidos))
    assert pedidos == [1, 2] and h.logado


def test_sem_resposta_para_no_login_dizendo_isso(monkeypatch) -> None:
    monkeypatch.setattr(sy, "CODIGO_ESPERA_S", 0.05)
    with pytest.raises(sy.SymplaError, match="não recebi resposta"):
        _entrar_com_codigo(monkeypatch, _Home(logado=False), _pedidor([], []))


def test_sem_canal_pra_perguntar_falha_alto(monkeypatch) -> None:
    with pytest.raises(sy.SymplaError, match="não tem como te perguntar"):
        _entrar_com_codigo(monkeypatch, _Home(logado=False), None)


def test_codigo_sem_ninguem_esperando_nao_e_consumido() -> None:
    assert sy.entregar_codigo("123456") is False
    assert sy.aguardando_codigo() is False


def test_handler_so_consome_o_codigo_quando_ha_login_esperando(monkeypatch) -> None:
    from types import SimpleNamespace

    from aiogram.dispatcher.event.bases import SkipHandler

    from bot.handlers import sympla as hs
    monkeypatch.setattr(hs.settings, "owner_telegram_id", 1)
    respostas = []

    class _Msg:
        text = "123 456"

        async def answer(self, t, **kw):
            respostas.append(t)
    dono = SimpleNamespace(id=1)
    with pytest.raises(SkipHandler):
        asyncio.run(hs.capturar_codigo(_Msg(), dono))

    async def _com_espera():
        espera = asyncio.create_task(sy._esperar_codigo(5))
        await asyncio.sleep(0)
        await hs.capturar_codigo(_Msg(), dono)
        return await espera
    assert asyncio.run(_com_espera()) == "123456"
    assert respostas and "Código recebido" in respostas[0]
