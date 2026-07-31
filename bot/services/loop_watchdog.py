"""Watchdog do event loop: mede se o loop está sendo BLOQUEADO.

Por que existe: quando uma requisição longa (nota técnica do DOU) parecia
"segurar" o bot inteiro, não dava pra distinguir três causas com sintoma
idêntico — (a) o loop travado por trabalho síncrono/CPU/swap, (b) rate limit
do provider de LLM derrubando a OUTRA chamada em backoff, (c) espera legítima
de rede. Sem medição, qualquer conserto seria chute.

Como funciona: uma task dorme `INTERVALO` e compara o tempo real decorrido com
o esperado. A diferença é o **atraso do loop** — o tempo em que ele NÃO pôde
rodar nada. Loop saudável fica em milissegundos; acima de meio segundo alguém
está segurando a linha (ou a máquina está em swap).

Custo: um `sleep` por segundo. Zero I/O, zero alocação relevante.
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

INTERVALO = 1.0          # segundos entre amostras
ALERTA_S = 0.5           # atraso a partir do qual vale logar
SILENCIO_S = 30.0        # não repete o alerta antes disso (evita enxurrada)
RESUMO_S = 300.0         # resumo periódico (só quando houve atraso)


def _rss_mb() -> float | None:
    """RSS do processo em MB (Linux). None fora do Linux."""
    try:
        with open("/proc/self/status", "r") as fh:
            for linha in fh:
                if linha.startswith("VmRSS:"):
                    return int(linha.split()[1]) / 1024
    except Exception:
        pass
    return None


async def watchdog_loop() -> None:
    logger.info("loop watchdog ativo (alerta acima de %.1fs de atraso)", ALERTA_S)
    ultimo_alerta = 0.0
    ultimo_resumo = time.monotonic()
    pior = 0.0
    amostras = 0
    atrasadas = 0
    while True:
        inicio = time.monotonic()
        await asyncio.sleep(INTERVALO)
        atraso = (time.monotonic() - inicio) - INTERVALO
        amostras += 1
        if atraso > ALERTA_S:
            atrasadas += 1
            pior = max(pior, atraso)
            agora = time.monotonic()
            if agora - ultimo_alerta >= SILENCIO_S:
                ultimo_alerta = agora
                rss = _rss_mb()
                logger.warning(
                    "event loop BLOQUEADO por %.1fs (o bot não respondeu nada "
                    "nesse tempo)%s — algo síncrono/CPU está segurando a linha, "
                    "ou a máquina está em swap",
                    atraso, f"; RSS={rss:.0f}MB" if rss else "",
                )
        agora = time.monotonic()
        if atrasadas and agora - ultimo_resumo >= RESUMO_S:
            logger.warning(
                "loop watchdog (últimos %.0f min): %d de %d amostras com "
                "atraso > %.1fs; pior = %.1fs",
                (agora - ultimo_resumo) / 60, atrasadas, amostras, ALERTA_S, pior,
            )
            ultimo_resumo, pior, amostras, atrasadas = agora, 0.0, 0, 0
