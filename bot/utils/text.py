"""Helpers de texto compartilhados."""
from __future__ import annotations


def chunk_text(text: str, limit: int = 4000) -> list[str]:
    """Quebra `text` em blocos de no máx. `limit` chars (teto do Telegram é
    4096). Corta SÓ em quebra de linha — como as tags <b>..</b> ficam na mesma
    linha, nenhum bloco parte uma tag ao meio. Linha isolada maior que o limite
    (raro) sofre corte duro. Devolve [] pra texto vazio."""
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks
