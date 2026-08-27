"""Tools DINÂMICAS: ferramentas que o próprio bot desenvolve e registra.

Fluxo (regra do dono, 27/08/2026 — "capacidade cognitiva própria"):
1. /tool_nova <pedido>  → o AGENTE escreve a candidata no workspace
   (tool_candidata/<nome>.py) seguindo o CONTRATO abaixo, com teste;
2. /tool_ativar <nome>  → o bot valida em SUBPROCESSO (import + contrato;
   módulo quebrado não derruba o bot), roda o teste se existir, manda o
   código-fonte pro dono e espera o botão de APROVAÇÃO;
3. aprovada → o arquivo vai pra DIR_ATIVAS (volume persistente — sobrevive
   ao rebuild do deploy), é carregado SEM restart e entra no catálogo do
   chat na próxima mensagem.

CONTRATO do módulo (um arquivo .py):
    TOOL_NOME: str        — [a-z0-9_], 3..32 chars, único no catálogo
    TOOL_DESCRICAO: str   — quando o LLM deve usá-la (é o que o modelo lê)
    TOOL_PARAMETROS: dict — JSON schema {"type": "object", ...}
    async def executar(args: dict, ctx) -> str
        — mesmo protocolo dos handlers de bot/services/tools.py: devolve
          "ok: ..." / "erro: ..."; pode usar ctx.direct_html/short_circuit.

Guardrail INEGOCIÁVEL: tool dinâmica roda EM PROCESSO, com os poderes do
bot — por isso NADA ativa sem aprovação explícita do dono, e a validação de
import roda fora do processo. Exceção em runtime vira "erro: ..." pro LLM
(nunca derruba o loop de tools).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import re
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

DIR_ATIVAS = Path("/app/data/tools_dinamicas")
DIR_PENDENTES = Path("/app/data/tools_pendentes")

_NOME_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")

# Registro em memória: nome → Tool (bot.services.tools.Tool).
_ativas: dict[str, object] = {}


def _nomes_estaticos() -> set[str]:
    from bot.services.tools import TOOLS
    return {t.name for t in TOOLS}


def _validar_contrato(mod, nome_arquivo: str) -> str | None:
    """None se ok; senão a mensagem do problema (dita alto, nunca engolida)."""
    nome = getattr(mod, "TOOL_NOME", None)
    if not isinstance(nome, str) or not _NOME_RE.match(nome):
        return f"TOOL_NOME inválido ({nome!r}) — use [a-z0-9_], 3..32 chars"
    if nome != Path(nome_arquivo).stem:
        return f"TOOL_NOME ('{nome}') difere do nome do arquivo ('{Path(nome_arquivo).stem}')"
    desc = getattr(mod, "TOOL_DESCRICAO", None)
    if not isinstance(desc, str) or len(desc.strip()) < 20:
        return "TOOL_DESCRICAO ausente ou curta demais (<20 chars) — o LLM decide por ela"
    params = getattr(mod, "TOOL_PARAMETROS", None)
    if not isinstance(params, dict) or params.get("type") != "object":
        return "TOOL_PARAMETROS deve ser um JSON schema {'type': 'object', ...}"
    fn = getattr(mod, "executar", None)
    if not asyncio.iscoroutinefunction(fn):
        return "executar(args, ctx) ausente ou não-async"
    if nome in _nomes_estaticos():
        return f"nome '{nome}' colide com uma tool fixa do bot"
    return None


def _carregar_arquivo(path: Path):
    """Importa o módulo e devolve (Tool, None) ou (None, motivo)."""
    from bot.services.tools import Tool

    mod_name = f"tool_dinamica_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        mod = importlib.util.module_from_spec(spec)
        # sys.modules ANTES do exec: dataclasses/imports internos do módulo
        # podem resolver o próprio nome durante o exec_module.
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
    except Exception as exc:
        sys.modules.pop(mod_name, None)
        return None, f"import falhou: {type(exc).__name__}: {exc}"
    problema = _validar_contrato(mod, path.name)
    if problema:
        sys.modules.pop(mod_name, None)
        return None, problema

    fn = mod.executar

    async def _blindado(args: dict, ctx) -> str:
        # Tool dinâmica que estoura não pode derrubar o turno: vira "erro"
        # pro modelo explicar — e fica no log com traceback.
        try:
            return await fn(args, ctx)
        except Exception as exc:
            logger.exception("tool dinâmica %s estourou", mod.TOOL_NOME)
            return (f"erro: a tool dinâmica '{mod.TOOL_NOME}' falhou "
                    f"({type(exc).__name__}: {exc}). Diga isso ao usuário — "
                    "ele pode corrigi-la com /tool_nova ou removê-la com /tool_rm.")

    return Tool(name=mod.TOOL_NOME, description=mod.TOOL_DESCRICAO,
                parameters=mod.TOOL_PARAMETROS, handler=_blindado), None


def carregar_todas(dir_ativas: Path | None = None) -> list[str]:
    """Carrega/recarrega o diretório inteiro (startup). Devolve a lista de
    problemas (um por arquivo ruim) — o chamador decide como avisar; um
    arquivo quebrado NUNCA impede os demais nem o boot."""
    base = dir_ativas or DIR_ATIVAS
    base.mkdir(parents=True, exist_ok=True)
    _ativas.clear()
    problemas: list[str] = []
    for path in sorted(base.glob("*.py")):
        tool, motivo = _carregar_arquivo(path)
        if tool is None:
            problemas.append(f"{path.name}: {motivo}")
            logger.error("tool dinâmica %s NÃO carregada: %s", path.name, motivo)
            continue
        _ativas[tool.name] = tool
        logger.info("tool dinâmica carregada: %s", tool.name)
    return problemas


def ativas() -> list:
    return list(_ativas.values())


def ativar(nome: str, origem: Path, dir_ativas: Path | None = None) -> str | None:
    """Move o arquivo aprovado pra DIR_ATIVAS e carrega. None=ok, senão erro."""
    base = dir_ativas or DIR_ATIVAS
    base.mkdir(parents=True, exist_ok=True)
    destino = base / f"{nome}.py"
    shutil.copy2(origem, destino)
    tool, motivo = _carregar_arquivo(destino)
    if tool is None:
        destino.unlink(missing_ok=True)
        return motivo
    _ativas[tool.name] = tool
    return None


def remover(nome: str, dir_ativas: Path | None = None) -> Path | None:
    """Descarrega e apaga. Devolve o path removido (pra backup) ou None."""
    base = dir_ativas or DIR_ATIVAS
    path = base / f"{nome}.py"
    _ativas.pop(nome, None)
    if path.is_file():
        return path
    return None


# Script que roda NO SUBPROCESSO da validação: import + contrato, sem tocar
# no processo do bot (módulo com loop infinito no import morre no timeout).
_VALIDADOR = r"""
import asyncio, importlib.util, json, re, sys
path = sys.argv[1]
spec = importlib.util.spec_from_file_location("cand", path)
mod = importlib.util.module_from_spec(spec)
sys.modules["cand"] = mod
spec.loader.exec_module(mod)
nome = getattr(mod, "TOOL_NOME", None)
assert isinstance(nome, str) and re.match(r"^[a-z][a-z0-9_]{2,31}$", nome), f"TOOL_NOME invalido: {nome!r}"
desc = getattr(mod, "TOOL_DESCRICAO", None)
assert isinstance(desc, str) and len(desc.strip()) >= 20, "TOOL_DESCRICAO ausente/curta"
params = getattr(mod, "TOOL_PARAMETROS", None)
assert isinstance(params, dict) and params.get("type") == "object", "TOOL_PARAMETROS invalido"
json.dumps(params)
assert asyncio.iscoroutinefunction(getattr(mod, "executar", None)), "executar(args, ctx) ausente/nao-async"
print("OK " + nome)
"""


async def validar_em_subprocesso(path: Path, timeout_s: int = 20) -> tuple[bool, str]:
    """Valida a candidata FORA do processo do bot."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _VALIDADOR, str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        import os, signal
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
        return False, f"validação não terminou em {timeout_s}s (import travado?)"
    out = out_b.decode("utf-8", errors="replace").strip()
    if proc.returncode == 0 and out.startswith("OK "):
        return True, out[3:]
    return False, out[-800:] or f"exit={proc.returncode}"
