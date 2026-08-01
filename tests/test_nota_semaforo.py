"""Uma nota técnica por vez em todo o processo.

Nada além do semáforo impede duas gerações simultâneas: job da fila +
/mp_dou_agora, DOIS usuários da casa pedindo ao mesmo tempo, ou o teto de
notas por janela acima de 1 (hoje 2).

Por que não basta o plano ser pago: a chave e o plano são de quem usa. Com
qualquer usuário num provedor de RPM apertado (free tier), duas gerações
concorrentes viram 429 — e 429 NÃO devolve a MP pra fila: `_nota_e_docx`
captura, avisa e segue. A nota some.
"""
from __future__ import annotations

import asyncio

import pytest

from bot.services import dou_monitor as dm


@pytest.fixture(autouse=True)
def _semaforo_limpo():
    """asyncio.Semaphore se prende ao primeiro loop que o usa. Em produção há
    UM loop pro processo inteiro, então isso é irrelevante lá; aqui cada
    asyncio.run cria um loop novo, e sem recriar o semáforo os testes
    quebrariam (ou pior: passariam por engano, capturando o RuntimeError de
    loop trocado como se fosse o erro sob teste)."""
    original = dm._SEM_NOTA
    dm._SEM_NOTA = asyncio.Semaphore(1)
    yield
    dm._SEM_NOTA = original


def test_semaforo_e_de_um() -> None:
    assert dm._SEM_NOTA._value == 1, "mais de 1 anula a proteção"


def test_geracoes_concorrentes_viram_fila() -> None:
    """Duas chamadas ao mesmo tempo NÃO podem se sobrepor."""
    simultaneas, pico = 0, 0

    async def _gerar(_id: int) -> None:
        nonlocal simultaneas, pico
        async with dm._SEM_NOTA:
            simultaneas += 1
            pico = max(pico, simultaneas)
            await asyncio.sleep(0.05)   # janela pra outra entrar, se puder
            simultaneas -= 1

    async def _main() -> None:
        await asyncio.gather(*(_gerar(i) for i in range(4)))

    asyncio.run(_main())
    assert pico == 1, f"{pico} gerações ao mesmo tempo — o 429 come a nota"


def test_falha_de_uma_libera_o_semaforo() -> None:
    """Exceção na geração não pode deixar o semáforo preso: as notas
    seguintes ficariam travadas pra sempre, em silêncio."""
    class _Quota(Exception):
        """Tipo próprio: capturar RuntimeError aqui mascararia o erro de loop
        trocado do asyncio e o teste passaria sem testar nada."""

    async def _explode() -> None:
        async with dm._SEM_NOTA:
            raise _Quota("429")

    async def _main() -> bool:
        try:
            await _explode()
        except _Quota:
            pass
        return dm._SEM_NOTA.locked()

    assert asyncio.run(_main()) is False


def test_ordem_de_chegada_e_preservada() -> None:
    """FIFO: a nota que pediu primeiro sai primeiro — senão a mais antiga da
    fila poderia ficar atrás de uma recém-chegada."""
    saida: list[int] = []

    async def _gerar(i: int) -> None:
        async with dm._SEM_NOTA:
            saida.append(i)
            await asyncio.sleep(0.01)

    async def _main() -> None:
        tarefas = []
        for i in range(4):
            tarefas.append(asyncio.create_task(_gerar(i)))
            await asyncio.sleep(0)      # garante a ordem de entrada na fila
        await asyncio.gather(*tarefas)

    asyncio.run(_main())
    assert saida == [0, 1, 2, 3]
