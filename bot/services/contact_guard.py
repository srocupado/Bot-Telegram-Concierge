"""Blindagem determinística contra TELEFONE não verificado na resposta.

O LLM insistia em sugerir o contato de uma empresa que NÃO era a pedida — o
telefone vinha da memória da conversa (ele mesmo citara antes) e sobrevivia a
quatro versões de regra no system prompt: ao ser proibido de chamar de
"filial", passou a apresentar a mesma empresa como "que opera na região",
mantendo o número. Regra de prompt não segurou; esta camada de CÓDIGO segura.

Princípio: um telefone só pode aparecer na resposta se estiver na SAÍDA DE
ALGUMA TOOL deste turno (página lida, buscar_local, etc.) ou na mensagem do
próprio usuário. Qualquer outro número veio de memória/invenção e é removido
junto com a frase que o carrega.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Telefone BR em qualquer formatação: (61) 3771-8949, 61 3771-8949,
# +55 61 99999-9999, 3771-8949…
_PHONE_RE = re.compile(
    r"(?:\+?55[\s.-]?)?(?:\(?\d{2}\)?[\s.-]?)?\d{4,5}[\s.-]?\d{4}\b"
)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _normalize(num: str) -> str:
    """Só os dígitos, sem +55 e sem 9 extra, pra comparar formatações diferentes."""
    d = _digits(num)
    if d.startswith("55") and len(d) > 10:
        d = d[2:]
    return d


def _autorizados(*fontes: str) -> set[str]:
    out: set[str] = set()
    for f in fontes:
        for m in _PHONE_RE.finditer(f or ""):
            n = _normalize(m.group(0))
            if len(n) >= 8:
                out.add(n)
    return out


def _split_frases(texto: str) -> list[str]:
    """Quebra em frases exigindo ESPAÇO depois da pontuação — sem isso,
    'R$ 3.303,00' era partido em 'R$ 3.' + '303,00' e a remontagem corrompia
    o valor. Assim, ponto decimal (sem espaço em seguida) não quebra nada."""
    return re.split(r"(?<=[.!?])\s+", texto)


def guard_contact_reply(reply: str, tool_output: str, user_text: str = "") -> str:
    """Remove da resposta as frases com telefone NÃO presente nas fontes
    confiáveis (saída de tool deste turno + mensagem do usuário).

    Devolve a resposta original quando não há telefone suspeito — o caminho
    comum. Nunca esvazia a resposta: se sobrar só isso, devolve um aviso curto.
    """
    if not reply:
        return reply
    suspeitos = [
        m.group(0) for m in _PHONE_RE.finditer(reply)
        if len(_normalize(m.group(0))) >= 8
    ]
    if not suspeitos:
        return reply

    ok = _autorizados(tool_output, user_text)
    nao_verificados = [t for t in suspeitos if _normalize(t) not in ok]
    if not nao_verificados:
        return reply  # todos os números vieram de fonte confiável

    alvo = {_normalize(t) for t in nao_verificados}

    def _limpa_linha(linha: str) -> str:
        mantidas: list[str] = []
        for frase in _split_frases(linha):
            nums = {
                _normalize(m.group(0)) for m in _PHONE_RE.finditer(frase)
                if len(_normalize(m.group(0))) >= 8
            }
            if nums & alvo:
                continue  # frase carrega número não verificado → cai fora
            mantidas.append(frase)
        return " ".join(p for p in (s.strip() for s in mantidas) if p)

    # Linha a linha, pra preservar quebras/bullets da formatação original.
    linhas = [_limpa_linha(ln) for ln in reply.split("\n")]
    limpo = "\n".join(linhas).strip()
    limpo = re.sub(r"\n{3,}", "\n\n", limpo)
    logger.warning(
        "contact_guard: removido(s) telefone(s) sem fonte: %s", nao_verificados,
    )
    if not limpo:
        return (
            "Não tenho um telefone verificado pra passar aqui — o contato que "
            "eu ia citar não veio da página consultada."
        )
    return limpo
