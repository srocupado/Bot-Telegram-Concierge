"""Trabalhos LONGOS fora do handler que os disparou.

Motivação real: a nota técnica do DOU leva minutos (download dos ZIPs +
pesquisa de contexto + redação + DOCX, uma MP por vez). Rodando dentro do
handler do botão, ela:

- segurava a **sessão de banco** do update por todo esse tempo (a sessão é
  criada pelo middleware e só fecha quando o handler retorna);
- deixava o update "em andamento" por minutos, sem nada indicar ao usuário
  quanto ainda falta;
- e qualquer erro só aparecia no fim.

Aqui o handler responde na hora e o trabalho segue numa task própria, com
sessão própria. Duas garantias que o `asyncio.create_task` cru não dá:

1. **referência forte** — o event loop guarda só weakref das tasks, então uma
   task sem referência pode ser coletada antes de terminar (mesmo footgun já
   corrigido em `memoria.py`);
2. **dedup por chave** — apertar o botão duas vezes (ou pedir a mesma data de
   novo) não dispara dois pipelines competindo pelo mesmo recurso escasso.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

_jobs: dict[str, asyncio.Task] = {}


def job_em_andamento(chave: str) -> bool:
    t = _jobs.get(chave)
    return t is not None and not t.done()


def jobs_ativos() -> list[str]:
    return [k for k, t in _jobs.items() if not t.done()]


def spawn(chave: str, fabrica: Callable[[], Awaitable[None]]) -> bool:
    """Dispara `fabrica()` em background sob `chave`.

    Devolve False (e NÃO dispara) se já houver um job vivo com a mesma chave —
    o chamador avisa o usuário. `fabrica` é uma função que devolve a corrotina,
    e não a corrotina pronta: assim nada é criado (nem fica "never awaited")
    quando o job é recusado.
    """
    if job_em_andamento(chave):
        logger.info("job %s já em andamento — pedido ignorado", chave)
        return False

    async def _wrapper() -> None:
        try:
            await fabrica()
        except asyncio.CancelledError:
            raise
        except Exception:
            # O job é responsável por avisar o usuário; aqui é a rede de
            # segurança pra falha NÃO virar silêncio no log.
            logger.exception("job %s terminou com exceção não tratada", chave)

    task = asyncio.get_running_loop().create_task(_wrapper(), name=f"job:{chave}")
    _jobs[chave] = task
    task.add_done_callback(lambda t: _jobs.pop(chave, None))
    logger.info("job %s iniciado (%d ativo(s))", chave, len(jobs_ativos()))
    return True
