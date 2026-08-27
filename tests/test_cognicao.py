"""Cognição própria (27/08/2026): A) propor o agente pra demanda fora do
catálogo; B) executar_python em sandbox; C) tools dinâmicas com aprovação.

O fio comum dos testes: nada executa/ativa sem decisão explícita do dono
(botão), falha nunca é silêncio, e módulo dinâmico quebrado não derruba o
bot (nem o boot, nem o turno).
"""
from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.services import sandbox, tools_dinamicas as td


# ───────────────────────── B: sandbox ─────────────────────────

def test_sandbox_executa_e_devolve_stdout() -> None:
    res = asyncio.run(sandbox.executar_python("print(2**10)"))
    assert res["ok"] and res["saida"] == "1024"


def test_sandbox_timeout_mata_o_processo() -> None:
    res = asyncio.run(sandbox.executar_python(
        "while True:\n    pass", timeout_s=2))
    assert not res["ok"] and "tempo esgotado" in res["detalhe"]


def test_sandbox_env_limpo_sem_segredos() -> None:
    """O código NÃO pode enxergar o .env do bot (BOT_TOKEN, chaves)."""
    res = asyncio.run(sandbox.executar_python(
        "import os; print(sorted(k for k in os.environ if 'TOKEN' in k or 'KEY' in k))"
    ))
    assert res["ok"] and res["saida"] == "[]"


def test_sandbox_erro_de_execucao_vira_detalhe() -> None:
    res = asyncio.run(sandbox.executar_python("1/0"))
    assert not res["ok"] and "ZeroDivisionError" in res["saida"]


def test_sandbox_trunca_saida_gigante() -> None:
    res = asyncio.run(sandbox.executar_python("print('x' * 100_000)"))
    assert res["ok"]
    assert len(res["saida"]) < 4000 and "truncada" in res["saida"]


# ─────────────────── C: carregador de tools dinâmicas ───────────────────

_TOOL_OK = textwrap.dedent('''
    TOOL_NOME = "fipe_teste"
    TOOL_DESCRICAO = "Consulta o valor FIPE de um carro pelo modelo informado."
    TOOL_PARAMETROS = {"type": "object", "properties": {
        "modelo": {"type": "string"}}, "required": ["modelo"]}

    async def executar(args, ctx):
        return "ok: R$ 50.000 (" + args["modelo"] + ")"
''')


def _escrever(dirp: Path, nome: str, corpo: str) -> Path:
    p = dirp / f"{nome}.py"
    p.write_text(corpo, encoding="utf-8")
    return p


def test_carrega_tool_valida_e_executa(tmp_path) -> None:
    _escrever(tmp_path, "fipe_teste", _TOOL_OK)
    problemas = td.carregar_todas(tmp_path)
    assert problemas == []
    tools = td.ativas()
    assert [t.name for t in tools] == ["fipe_teste"]
    out = asyncio.run(tools[0].handler({"modelo": "gol"}, None))
    assert out == "ok: R$ 50.000 (gol)"


def test_modulo_quebrado_nao_derruba_os_demais(tmp_path) -> None:
    """Arquivo ruim vira PROBLEMA dito alto — os bons carregam normal."""
    _escrever(tmp_path, "quebrada", "isso nem é python válido ((")
    _escrever(tmp_path, "fipe_teste", _TOOL_OK)
    problemas = td.carregar_todas(tmp_path)
    assert len(problemas) == 1 and "quebrada.py" in problemas[0]
    assert [t.name for t in td.ativas()] == ["fipe_teste"]


def test_contrato_incompleto_e_reprovado(tmp_path) -> None:
    _escrever(tmp_path, "semdesc", 'TOOL_NOME = "semdesc"\nasync def executar(a, c):\n    return "ok"')
    problemas = td.carregar_todas(tmp_path)
    assert problemas and "TOOL_DESCRICAO" in problemas[0]
    assert td.ativas() == []


def test_colisao_com_tool_fixa_e_reprovada(tmp_path) -> None:
    """Tool dinâmica não pode SOMBREAR uma fixa (ex.: consultar_saldo)."""
    corpo = _TOOL_OK.replace('"fipe_teste"', '"consultar_saldo"')
    _escrever(tmp_path, "consultar_saldo", corpo)
    problemas = td.carregar_todas(tmp_path)
    assert problemas and "colide" in problemas[0]


def test_excecao_da_tool_dinamica_vira_erro_pro_llm(tmp_path) -> None:
    corpo = _TOOL_OK.replace('return "ok: R$ 50.000 (" + args["modelo"] + ")"',
                             "raise RuntimeError('api fora')")
    _escrever(tmp_path, "fipe_teste", corpo)
    td.carregar_todas(tmp_path)
    out = asyncio.run(td.ativas()[0].handler({"modelo": "gol"}, None))
    assert out.startswith("erro:") and "fipe_teste" in out


def test_validacao_em_subprocesso_aprova_e_reprova(tmp_path) -> None:
    ok_path = _escrever(tmp_path, "fipe_teste", _TOOL_OK)
    ok, nome = asyncio.run(td.validar_em_subprocesso(ok_path))
    assert ok and nome == "fipe_teste"
    ruim = _escrever(tmp_path, "ruim", "import os\nwhile True: pass")
    ok2, detalhe = asyncio.run(td.validar_em_subprocesso(ruim, timeout_s=3))
    assert not ok2 and "não terminou" in detalhe


def test_ativar_e_remover(tmp_path) -> None:
    td.carregar_todas(tmp_path / "ativas")
    origem = _escrever(tmp_path, "fipe_teste", _TOOL_OK)
    assert td.ativar("fipe_teste", origem, tmp_path / "ativas") is None
    assert [t.name for t in td.ativas()] == ["fipe_teste"]
    removido = td.remover("fipe_teste", tmp_path / "ativas")
    assert removido is not None and removido.name == "fipe_teste.py"
    assert td.ativas() == []


def test_tools_do_chat_inclui_dinamicas(tmp_path) -> None:
    from bot.services.tools import TOOLS, tools_do_chat
    _escrever(tmp_path, "fipe_teste", _TOOL_OK)
    td.carregar_todas(tmp_path)
    nomes = {t.name for t in tools_do_chat()}
    assert "fipe_teste" in nomes and len(nomes) == len(TOOLS) + 1
    td.carregar_todas(tmp_path / "vazio")  # limpa pro resto da suíte


# ─────────────── A: proposta do agente (botão, não execução) ───────────────

def test_propor_agente_gera_botao_e_nao_executa(monkeypatch) -> None:
    from bot.services import tools
    from bot.handlers import agent as ah

    monkeypatch.setattr(tools.settings, "owner_telegram_id", 42)
    disparos: list[str] = []
    monkeypatch.setattr(ah, "start_background_task",
                        lambda prompt, chat_id, **kw: disparos.append(prompt) or "started")

    ctx = SimpleNamespace(user=SimpleNamespace(id=42), direct_html=None,
                          direct_markup=None, short_circuit=False)
    out = asyncio.run(tools._h_propor_agente(
        {"tarefa": "raspar o placar do campeonato X"}, ctx))
    assert out.startswith("ok:")
    assert disparos == [], "propor NÃO pode executar — só oferecer"
    assert ctx.direct_markup is not None and ctx.short_circuit
    assert "US$" in ctx.direct_html, "o custo precisa estar na oferta"

    # O botão dispara de verdade (callback ok) e proposta expirada avisa.
    pid = ctx.direct_markup.inline_keyboard[0][0].callback_data.split(":")[2]
    assert ah._propostas[pid][0] == "raspar o placar do campeonato X"


def test_executar_agente_tambem_passa_pelo_botao(monkeypatch) -> None:
    """Medido no 1º teste real (27/08): 'gera um PDF com um calendário' caiu
    no executar_agente e US$ 0,53 rodaram SEM o portão de custo — o gate não
    pode depender de o modelo escolher a tool certa entre duas parecidas.
    Agora o caminho de tool SEMPRE propõe; imediato é só /agente e cron."""
    from bot.services import tools
    from bot.handlers import agent as ah

    monkeypatch.setattr(tools.settings, "owner_telegram_id", 42)
    disparos: list[str] = []
    monkeypatch.setattr(ah, "start_background_task",
                        lambda prompt, chat_id, **kw: disparos.append(prompt) or "started")
    ctx = SimpleNamespace(user=SimpleNamespace(id=42), direct_html=None,
                          direct_markup=None, short_circuit=False)
    out = asyncio.run(tools._h_executar_agente(
        {"tarefa": "gera um PDF com um calendário"}, ctx))
    assert out.startswith("ok:") and disparos == []
    assert ctx.direct_markup is not None, "executar_agente sem botão = custo sem consentimento"


def test_clique_na_proposta_pede_produto_final_limpo(monkeypatch) -> None:
    """A tarefa disparada pelo botão leva a instrução de deixar auxiliares em
    .aux/ — senão o script que gerou o PDF vem junto como artefato."""
    from bot.handlers import agent as ah
    import asyncio as aio

    monkeypatch.setattr(ah.settings, "owner_telegram_id", 42)
    disparos: list[str] = []
    monkeypatch.setattr(ah, "start_background_task",
                        lambda prompt, chat_id, **kw: disparos.append(prompt) or "started")
    pid = ah.registrar_proposta("gera um PDF X")

    class _Msg:
        chat = SimpleNamespace(id=42)
        async def edit_reply_markup(self, reply_markup=None): pass
        async def answer(self, *a, **kw): pass

    q = SimpleNamespace(data=f"agprop:ok:{pid}",
                        from_user=SimpleNamespace(id=42), message=_Msg())
    async def _ans(*a, **kw): pass
    q.answer = _ans
    aio.run(ah.cb_proposta_agente(q, SimpleNamespace(id=42)))
    assert len(disparos) == 1 and disparos[0].startswith("gera um PDF X")
    assert ".aux/" in disparos[0]


def test_propor_agente_para_nao_owner_e_recusa_franca(monkeypatch) -> None:
    from bot.services import tools
    monkeypatch.setattr(tools.settings, "owner_telegram_id", 42)
    ctx = SimpleNamespace(user=SimpleNamespace(id=7), direct_html=None,
                          direct_markup=None, short_circuit=False)
    out = asyncio.run(tools._h_propor_agente({"tarefa": "x"}, ctx))
    assert out.startswith("erro:") and "restrito" in out


def test_executar_python_tool_entrega_verbatim(monkeypatch) -> None:
    from bot.services import tools
    monkeypatch.setattr(tools.settings, "owner_telegram_id", 42)
    ctx = SimpleNamespace(user=SimpleNamespace(id=42), direct_html=None,
                          fallback_text=None, short_circuit=False)
    out = asyncio.run(tools._h_executar_python(
        {"codigo": "print(sum(range(101)))"}, ctx))
    assert "5050" in out
    assert "5050" in (ctx.direct_html or ""), "número calculado vai verbatim"
    assert not ctx.short_circuit, "o modelo precisa da saída pra encadear"


def test_ajuda_acha_a_secao_de_cognicao_para_o_dono() -> None:
    from bot.handlers.start import find_help_sections
    for frase in ["como crio uma ferramenta nova?",
                  "você consegue desenvolver uma tool?",
                  "dá pra automatizar uma consulta?"]:
        blocos = find_help_sections(frase, incluir_owner=True)
        assert any("Cognição" in b for b in blocos), frase
    # Pra não-dono a seção NEM EXISTE (recurso invisível, como o /agente).
    assert not any("Cognição" in b
                   for b in find_help_sections("como crio uma ferramenta nova?"))
