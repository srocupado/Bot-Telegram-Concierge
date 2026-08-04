"""Helpers de texto compartilhados."""
from __future__ import annotations

import re

# Tags inline que o Telegram aceita e que podem ficar ABERTAS quando um bloco é
# cortado. <br> não existe no HTML do Telegram, mas é barato ignorar.
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)([^>]*)>")
_VAZIAS = {"br", "hr", "img"}


def _tags_abertas(fragmento: str) -> list[tuple[str, str]]:
    """[(nome, tag de abertura)] que ficaram sem fechar em `fragmento`."""
    pilha: list[tuple[str, str]] = []
    for m in _TAG_RE.finditer(fragmento):
        fechando, nome = m.group(1), m.group(2).lower()
        if nome in _VAZIAS:
            continue
        if fechando:
            for i in range(len(pilha) - 1, -1, -1):
                if pilha[i][0] == nome:
                    pilha.pop(i)
                    break
        else:
            pilha.append((nome, m.group(0)))
    return pilha


def _corte_seguro(linha: str, limit: int) -> int:
    """Índice de corte ≤ limit que não cai DENTRO de uma tag (`<b>`) nem de uma
    entidade (`&amp;`) — cortar no meio delas gera HTML inválido e o Telegram
    recusa o bloco inteiro. Prefere um espaço perto do fim."""
    corte = limit
    abre = linha.rfind("<", 0, corte)
    if abre != -1 and linha.find(">", abre) >= corte:
        corte = abre
    amp = linha.rfind("&", 0, corte)
    if amp != -1 and corte - amp <= 10:
        fim = linha.find(";", amp)
        if fim == -1 or fim >= corte:
            corte = amp
    espaco = linha.rfind(" ", max(0, corte - 200), corte)
    if espaco > 0:
        # Voltar pro espaço não pode DESFAZER a proteção de tag: o espaço pode
        # estar DENTRO de uma tag com atributos (`<a href="...">`) que o passo
        # inicial tinha pulado inteira — re-checa no novo ponto. (Bug real: o
        # corte caía entre `<a` e `href`, o Telegram recusava o bloco e o
        # fallback texto-puro mostrava o href cru ao usuário.)
        abre2 = linha.rfind("<", 0, espaco)
        if abre2 != -1 and linha.find(">", abre2) >= espaco:
            # Espaço dentro de tag: corta ANTES da tag. Se a tag começa na
            # posição 0 (linha inteira é uma tag gigante), não há ponto bom —
            # mantém o corte protegido anterior.
            if abre2 > 0:
                corte = abre2
        else:
            corte = espaco
    return max(1, corte)


def _len16(s: str) -> int:
    """Comprimento em unidades UTF-16 — é assim que o Telegram conta o teto
    de 4096: emoji fora do BMP (a maioria dos usados aqui) vale 2, não 1.
    `len()` subconta e um bloco "dentro do limite" era recusado."""
    return sum(2 if ord(c) > 0xFFFF else 1 for c in s)


def chunk_text(text: str, limit: int = 4000, *, mode: str = "plain") -> list[str]:
    """Quebra `text` em blocos de no máx. `limit` chars (teto do Telegram é
    4096). Corta em quebra de linha; linha isolada maior que o limite é cortada
    num ponto SEGURO (fora de tag/entidade — antes era corte cego no meio da
    tag e o bloco inteiro caía pro texto puro, com as tags cruas à vista).

    `mode` costura a formatação que atravessa blocos:
      - "html": fecha as tags abertas no fim do bloco e reabre no seguinte;
      - "markdown": fecha e reabre bloco de código (```) partido ao meio;
      - "plain" (default): não mexe.
    Devolve [] pra texto vazio.
    """
    if not text:
        return []
    if _len16(text) <= limit:
        return [text]

    # A costura acrescenta tags/cercas ao bloco; reserva espaço pra elas não
    # empurrarem o bloco além do teto real do Telegram (4096). A reserva fixa
    # é só a primeira defesa — a garantia DURA fica no laço pós-costura.
    teto = limit
    if mode in ("html", "markdown"):
        limit = max(200, limit - 80)

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            corte = _corte_seguro(line, limit) if mode == "html" else limit
            chunks.append(line[:corte])
            line = line[corte:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line
    if current:
        chunks.append(current)

    if mode == "html":
        return _garantir_teto_html(_costurar_html(chunks), teto)
    if mode == "markdown":
        return _costurar_markdown(chunks)
    return chunks


def _garantir_teto_html(blocos: list[str], teto: int) -> list[str]:
    """Garantia DURA de que nenhum bloco costurado passa do teto (em UTF-16,
    como o Telegram conta). A reserva fixa de 80 chars não cobria a reabertura
    de tag com atributo longo (`<a href="...">` do in.gov.br): o bloco saía
    >4096, o envio HTML falhava E o fallback texto-puro reenviava o MESMO
    bloco grande — briefing/digest sumiam inteiros. Bloco que estoura é
    re-partido ao meio (ponto seguro) e re-costurado até caber."""
    i = 0
    while i < len(blocos):
        b = blocos[i]
        if _len16(b) <= teto:
            i += 1
            continue
        meio = _corte_seguro(b, max(1, len(b) // 2))
        if meio >= len(b):   # sem ponto de corte útil: desiste (não trava)
            i += 1
            continue
        blocos[i:i + 1] = _costurar_html([b[:meio], b[meio:]])
    return blocos


def _costurar_html(chunks: list[str]) -> list[str]:
    """Fecha no fim do bloco o que ficou aberto e reabre no bloco seguinte."""
    out: list[str] = []
    pendentes: list[tuple[str, str]] = []
    for chunk in chunks:
        corpo = "".join(t for _, t in pendentes) + chunk
        pendentes = _tags_abertas(corpo)
        fecho = "".join(f"</{nome}>" for nome, _ in reversed(pendentes))
        out.append(corpo + fecho)
    return out


def _costurar_markdown(chunks: list[str]) -> list[str]:
    """Bloco de código partido entre chunks deixa a cerca aberta nos DOIS —
    ambos falham no parse e viram texto puro. Fecha e reabre."""
    out: list[str] = []
    aberto = False
    for chunk in chunks:
        corpo = ("```\n" + chunk) if aberto else chunk
        aberto = corpo.count("```") % 2 == 1
        out.append(corpo + ("\n```" if aberto else ""))
    return out
