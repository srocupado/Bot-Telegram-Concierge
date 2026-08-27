"""Envio seguro quando texto DO USUÁRIO entra numa mensagem formatada.

O bot manda com parse_mode=Markdown por padrão. Texto que o dono escreveu
("pagar João_Silva", "revisar função get_user", "comprar 2*3 metros") entra
cru nessas mensagens e um marcador desbalanceado faz o Telegram RECUSAR a
mensagem INTEIRA — não é degradação visual, é a mensagem não chegar.

Modos de falha reais que isso já causou:
- `/tool_ativar onvif_scan` (27/08/2026): o '_' do nome matou o handler
  antes dos botões de aprovação; o dono não recebeu nada e achou que o
  comando estava morto;
- `/lembrar pagar João_Silva amanhã 9h`: o lembrete é GRAVADO e só a
  confirmação falha — o dono acha que não funcionou, repete, e fica com
  dois lembretes.

O padrão aqui é o mesmo que o scheduler já usava na entrega de lembrete:
tenta com Markdown (mantém o negrito/itálico quando o texto permite) e, se
o Telegram recusar, reenvia SEM formatação. A mensagem sempre chega.

`plano` é a versão sem marcadores, escrita à mão pelo chamador: ela evita
que o fallback mostre os asteriscos crus na tela. Sem ela, reenvia o mesmo
texto — feio, porém íntegro (nunca perder conteúdo é o que importa).
"""
from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

logger = logging.getLogger(__name__)


async def answer_md(
    message: Message, texto: str, *, plano: str | None = None, **kw
) -> None:
    try:
        await message.answer(texto, parse_mode="Markdown", **kw)
    except TelegramBadRequest:
        logger.warning(
            "markdown recusado pelo Telegram (texto do usuário com marcador "
            "desbalanceado); reenviando em texto puro", exc_info=True,
        )
        await message.answer(plano if plano is not None else texto,
                             parse_mode=None, **kw)
