"""Execução de código Python sob demanda do LLM (a "calculadora" cognitiva).

Micro-demandas que nenhuma tool cobre — conversão de unidades, juros
compostos, parsing de um texto colado, combinatória — o modelo resolve
escrevendo código e executando AQUI, em segundos, dentro do próprio turno.
Demanda de minutos/arquivos é do agente (agent_runner), não daqui.

Recorte de segurança HONESTO (bot pessoal, tool owner-only):
- processo separado com env LIMPO (nenhum segredo do .env chega ao código),
  HOME num diretório efêmero e cwd idem — o processo não enxerga o /app;
- rlimits: CPU, memória, tamanho de arquivo e nº de processos (fork bomb);
- timeout de parede com kill do GRUPO de processos (session própria);
- python -I (isolated: sem site-packages do usuário, sem PYTHONPATH).
O que este sandbox NÃO faz: bloquear rede (exigiria namespaces/seccomp que o
container não garante no Orange Pi) nem impedir leitura de caminhos do
sistema legíveis por qualquer processo. Por isso a tool é restrita ao dono.
"""
from __future__ import annotations

import asyncio
import logging
import os
import resource
import signal
import sys
import tempfile

logger = logging.getLogger(__name__)

TIMEOUT_S = 30           # parede
CPU_S = 15               # RLIMIT_CPU
MEM_BYTES = 512 * 1024 * 1024
FSIZE_BYTES = 10 * 1024 * 1024
MAX_SAIDA = 3500         # cabe numa mensagem e não infla o contexto do LLM


def _limites() -> None:
    # Roda NO FILHO, antes do exec. setsid separado via start_new_session.
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_S, CPU_S + 5))
    resource.setrlimit(resource.RLIMIT_AS, (MEM_BYTES, MEM_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (FSIZE_BYTES, FSIZE_BYTES))
    resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))


def _truncar(texto: str, limite: int = MAX_SAIDA) -> str:
    if len(texto) <= limite:
        return texto
    corte = len(texto) - limite
    return texto[:limite] + f"\n… [saída truncada: +{corte} caracteres]"


async def executar_python(codigo: str, timeout_s: int = TIMEOUT_S) -> dict:
    """Roda `codigo` num processo isolado. Nunca levanta por causa do código
    do usuário — devolve {"ok": bool, "saida": str, "detalhe": str}."""
    with tempfile.TemporaryDirectory(prefix="sandbox-") as tmp:
        script = os.path.join(tmp, "main.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(codigo)
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": tmp,
            "TMPDIR": tmp,
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "C.UTF-8",
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=tmp,
            env=env,
            preexec_fn=_limites,
            start_new_session=True,
        )
        try:
            out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            # Mata o GRUPO: filho que forka não pode sobreviver ao timeout
            # (CPU do Orange Pi é recurso do bot inteiro).
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            return {"ok": False, "saida": "",
                    "detalhe": f"tempo esgotado ({timeout_s}s de parede)"}
        saida = _truncar(out_b.decode("utf-8", errors="replace").strip())
        if proc.returncode != 0:
            return {"ok": False, "saida": saida,
                    "detalhe": f"exit={proc.returncode}"}
        return {"ok": True, "saida": saida, "detalhe": ""}
