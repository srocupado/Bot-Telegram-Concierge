"""O dispatch de VOZ tem que cobrir todo comando que existe no bot.

Motivo: 17 comandos (viagem, mp_dou_*, proativo*, reset_memoria, arquivos…)
ficaram meses fora do `_DISPATCH` de `handlers/voice.py` e respondiam
"❌ comando não reconhecido" por voz, contrariando o help. É o mesmo padrão
que motivou a regra do help no CLAUDE.md: feature nova sem emendar o ponto de
integração. Este teste faz a lacuna ERRAR ALTO, sem depender de alguém lembrar.

Lê os `Command("x")` dos handlers por regex (offline, sem importar aiogram nem
subir o bot) e compara com as chaves do `_DISPATCH`, lidas por AST.
"""
from __future__ import annotations

import ast
import pathlib
import re

RAIZ = pathlib.Path(__file__).resolve().parent.parent
HANDLERS = RAIZ / "bot" / "handlers"
VOICE = HANDLERS / "voice.py"

_CMD_RE = re.compile(r"""Command\(\s*["']([a-z0-9_]+)["']""", re.IGNORECASE)

# Comandos deliberadamente fora do dispatch de voz, com o porquê.
_ISENTOS: dict[str, str] = {}


class _FakeCommand:
    """Dublê do CommandObject do aiogram (o teste não importa aiogram)."""

    def __init__(self, prefix: str, command: str, args: str | None) -> None:
        self.prefix, self.command, self.args = prefix, command, args


def _comandos_registrados() -> set[str]:
    out: set[str] = set()
    for f in HANDLERS.glob("*.py"):
        if f.name == "voice.py":
            continue
        out |= set(_CMD_RE.findall(f.read_text()))
    return out


def _chaves_do_dispatch() -> set[str]:
    arvore = ast.parse(VOICE.read_text())
    for node in ast.walk(arvore):
        if not isinstance(node, ast.AnnAssign):
            continue
        alvo = node.target
        if isinstance(alvo, ast.Name) and alvo.id == "_DISPATCH":
            assert isinstance(node.value, ast.Dict)
            return {
                k.value for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
    raise AssertionError("_DISPATCH não encontrado em handlers/voice.py")


def test_todo_comando_e_executavel_por_voz() -> None:
    registrados = _comandos_registrados()
    assert registrados, "nenhum Command() encontrado — regex quebrou?"
    faltando = registrados - _chaves_do_dispatch() - set(_ISENTOS)
    assert not faltando, (
        "comandos sem entrada no _DISPATCH de voz (o usuário ouve "
        f"'comando não reconhecido'): {sorted(faltando)}"
    )


def test_dispatch_nao_tem_comando_inexistente() -> None:
    """O contrário também: entrada apontando pra comando que não existe mais."""
    sobrando = _chaves_do_dispatch() - _comandos_registrados()
    assert not sobrando, f"entradas órfãs no _DISPATCH: {sorted(sobrando)}"


def test_assinaturas_dos_handlers_cabem_no_invocar() -> None:
    """`_invocar` monta os argumentos por nome de parâmetro. Se algum handler
    usar outro nome (ou não começar por `message`), a chamada por voz estoura
    em runtime — aqui erra na hora."""
    conhecidos = {"message", "command", "user", "session"}
    problemas: list[str] = []
    for f in HANDLERS.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if not node.name.startswith("cmd_"):
                continue
            ps = [a.arg for a in node.args.args]
            if not ps or ps[0] != "message" or set(ps) - conhecidos:
                problemas.append(f"{f.name}::{node.name}{tuple(ps)}")
    assert not problemas, f"assinatura fora do contrato de _invocar: {problemas}"


def test_invocar_passa_so_o_que_o_handler_pede() -> None:
    """Executa o `_invocar` real (extraído do fonte pra não importar o módulo,
    que puxa SDKs pesados) contra handlers de mentira com cada assinatura que
    existe no projeto. É o caso que o wrapper manual do /reset errava: ele
    chamava o handler só com `message`, e o comando quebrava sempre."""
    import asyncio
    import inspect
    from typing import Any, Callable  # noqa: F401  (usado pelo exec)

    fonte = VOICE.read_text()
    trecho = fonte[fonte.index("def _cmd("):fonte.index("@router.message(F.voice")]
    trecho = trecho[:trecho.index("_DISPATCH")] + trecho[trecho.index("async def _invocar"):]
    ns: dict[str, Any] = {
        "CommandObject": _FakeCommand, "inspect": inspect,
        "Message": object, "User": object, "AsyncSession": object,
        "Any": Any, "Callable": Callable,
    }
    exec(compile(trecho, "voice_trecho", "exec"), ns)
    invocar = ns["_invocar"]

    vistos: dict[str, tuple] = {}

    async def h_so_message(message):
        vistos["a"] = (message,)

    async def h_message_command(message, command):
        vistos["b"] = (message, command.command, command.args)

    async def h_message_user(message, user):
        vistos["c"] = (message, user)

    async def h_tudo(message, command, user, session):
        vistos["d"] = (message, command.args, user, session)

    async def h_user_session(message, user, session):  # o caso do /reset
        vistos["e"] = (message, user, session)

    async def main():
        for nome, h in (("x", h_so_message), ("viagem", h_message_command),
                        ("ping", h_message_user), ("lembrar", h_tudo),
                        ("reset", h_user_session)):
            await invocar(nome, h, "MSG", "args aqui", "USER", "SESSION")

    asyncio.run(main())
    assert vistos["a"] == ("MSG",)
    assert vistos["b"] == ("MSG", "viagem", "args aqui")
    assert vistos["c"] == ("MSG", "USER")
    assert vistos["d"] == ("MSG", "args aqui", "USER", "SESSION")
    assert vistos["e"] == ("MSG", "USER", "SESSION")
