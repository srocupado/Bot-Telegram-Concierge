from __future__ import annotations

import logging
import sys

from pythonjsonlogger import jsonlogger


def make_formatter() -> jsonlogger.JsonFormatter:
    """Formatter do log. Separado do setup pra ser testável: o setup sai cedo
    quando a raiz já tem handler (caso do pytest), e aí não dá pra inspecionar
    o que ele teria montado."""
    return jsonlogger.JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "ts", "levelname": "level"},
        # Sem isto o json.dumps escapa acento: "não publicada" vira
        # "não publicada" no log. Como as mensagens são todas em
        # português, `docker compose logs | grep` não achava nada e o log
        # parecia vazio — foi assim que uma correção que funcionou pareceu
        # não ter rodado. JSON com UTF-8 continua válido.
        json_ensure_ascii=False,
    )


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if root.handlers:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(make_formatter())
    root.addHandler(handler)
    root.setLevel(level.upper())

    for noisy in ("aiogram.event", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
