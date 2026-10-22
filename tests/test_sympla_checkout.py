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

    async def _entrar(page, creds):
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


# ───────────── login que "não pegou" falha no login, não no checkout ─────────────

class _Home:
    def __init__(self, logado):
        self.logado, self.modal, self.cliques = logado, False, []
        self.keyboard = self

    async def press(self, _k):
        self.modal = False

    def get_by_role(self, role, name=None):
        return _Loc(self, "conta")

    def get_by_text(self, padrao):
        return _Loc(self, "modal")

    def visivel(self, alvo):
        return self.modal if alvo == "modal" else True

    def clicar(self, alvo):
        self.cliques.append(alvo)
        self.modal = not self.logado

    async def wait_for_timeout(self, _ms):
        return None

    async def screenshot(self, **kw):
        return b"PRINT"


def test_login_que_nao_pegou_falha_dizendo_isso() -> None:
    with pytest.raises(sy.SymplaError) as e:
        asyncio.run(sy._conferir_login(_Home(logado=False)))
    assert "o login não pegou" in str(e.value)
    assert e.value.print_meio == b"PRINT"


def test_login_que_pegou_segue() -> None:
    h = _Home(logado=True)
    asyncio.run(sy._conferir_login(h))
    assert h.cliques == ["conta"]
