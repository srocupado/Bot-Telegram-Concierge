"""Envio de mensagem HTML que não desiste da formatação na 1ª falha."""
from __future__ import annotations

import asyncio
import logging

from bot.utils.text import html_para_texto

logger = logging.getLogger(__name__)

# Pausas entre as novas tentativas EM HTML quando a falha não é de formatação.
PAUSAS_S = (2.0, 5.0)


async def enviar_html(bot, chat_id: int, bloco: str, **kw) -> bool:
    """Manda `bloco` em HTML. Devolve True se a mensagem saiu.

    Log do Orange Pi, 14/11/2026 21h30: o Telegram respondeu "Bad Gateway" e
    depois "Request timeout" — falha DELES, passageira — e o bot caiu direto
    no texto puro (o dono viu as tags cruas). Agora:
    - erro de FORMATAÇÃO (TelegramBadRequest): texto puro na hora, sem tags —
      repetir o mesmo HTML não muda nada;
    - qualquer outro erro (servidor/rede): tenta de novo em HTML depois de
      cada pausa de PAUSAS_S; só então texto puro, sem tags."""
    from aiogram.exceptions import TelegramBadRequest

    for pausa in (*PAUSAS_S, None):
        try:
            await bot.send_message(chat_id, bloco, parse_mode="HTML", **kw)
            return True
        except TelegramBadRequest:
            logger.exception("envio: HTML recusado pelo Telegram p/ %d — texto puro", chat_id)
            break
        except Exception as exc:
            logger.warning("envio: falha passageira p/ %d (%s: %s)%s", chat_id,
                           type(exc).__name__, exc,
                           f" — tento de novo em {pausa:.0f}s" if pausa else "")
            if pausa is None:
                break
            await asyncio.sleep(pausa)
    try:
        await bot.send_message(chat_id, html_para_texto(bloco), parse_mode=None, **kw)
        return True
    except Exception:
        logger.exception("envio: texto puro também falhou p/ %d", chat_id)
        return False
