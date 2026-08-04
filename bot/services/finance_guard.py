"""Blindagem anti-alucinação para lançamentos financeiros.

O LLM (sobretudo modelos menores) às vezes responde "Lançado!" sem ter
chamado a tool de lançamento — confirmação falsa. Esta camada de CÓDIGO
intercepta a resposta: se o pedido do usuário é claramente um LANÇAMENTO
financeiro e (a) nenhuma tool de lançamento gravou com sucesso e (b) a
resposta afirma sucesso, troca a resposta por um aviso honesto.

Precisão: só dispara quando os três sinais coincidem (intenção de
lançamento + nada gravado + resposta alega sucesso), evitando atropelar
consultas, perguntas de esclarecimento ou exclusões.
"""
from __future__ import annotations

import re

# Verbos/termos que indicam REGISTRAR um movimento novo.
_LOG_CUES = (
    "lança", "lanca", "lancar", "lançar", "registr", "gastei", "gasto",
    "paguei", "pagar", "comprei", "comprar", "recebi", "receber", "pix",
    "débito", "debito", "crédito", "credito", "cartão", "cartao", "boleto",
    "transferi", "transferência", "transferencia", "depósito", "deposito",
    "salário", "salario", "aporte", "apliquei", "investi", "fatura",
)
# Palavras que indicam CONSULTA/edição (não lançamento) — desarmam o guard.
_QUERY_CUES = (
    # "cadê/onde/apareceu/consta/tem algum" faltavam: "cadê o pix de 200 que
    # recebi?" era lido como LANÇAMENTO, e o guard trocava a resposta CORRETA
    # da consulta por "não registrei isso".
    "cadê", "cade", "onde está", "onde esta", "apareceu", "consta",
    "tem algum", "teve algum", "achei", "encontrei", "procur", "verifica",
    "confere", "conferir", "checa", "checar",
    "quanto", "quais", "qual", "mostra", "mostrar", "lista", "listar",
    "extrato", "saldo", "consulta", "consultar", "resumo", "relatório",
    "relatorio", "quanto gastei", "apaga", "apagar", "cancela", "cancelar",
    "remove", "remover", "estorna", "estornar", "deleta", "deletar",
    "corrige", "corrigir",
)
# A resposta ALEGA que registrou algo.
_SUCCESS_CLAIMS = (
    "lançad", "lancad", "lancei", "lançei", "registrad", "registrei",
    "registrado", "anotad", "anotei", "adicionad", "adicionei", "feito",
    # "gravad" (radical) cobre gravado/gravada/gravadas; a lista antiga tinha o
    # typo "gravd" (cue morto) e só "gravado" — "Despesa gravada!" alucinada
    # passava pela blindagem.
    "inseri", "salvo", "salvei", "gravad", "gravei",
    # Fraseados sem os radicais acima que passavam intactos (auditoria
    # 03/08/2026): "Pronto! Já está no seu financeiro."
    "pronto", "no seu financeiro", "na sua fatura", "efetuad", "concluíd",
    "concluid", "incluí", "incluid",
)

# RADICAIS: casam por prefixo de palavra (\bregistr → registrado/registrei…).
# O resto casa palavra/frase INTEIRA — matching por substring furava nas duas
# direções (auditoria 03/08/2026): "cade" ⊂ "CADEira" e "qual" ⊂ "QUALidade"
# desarmavam o guard ("comprei uma cadeira por 300" + confirmação alucinada
# passava — exatamente o que a camada existe pra impedir), e "feito" ⊂
# "PerFEITO" bloqueava pergunta de esclarecimento legítima.
_QUERY_STEMS = frozenset({"procur", "consult", "verifica", "confere", "checa",
                          "apaga", "cancela", "remove", "estorna", "deleta",
                          "corrige", "mostra", "lista"})
_CLAIM_STEMS = frozenset({"lançad", "lancad", "registrad", "anotad",
                          "adicionad", "gravad", "inseri", "efetuad",
                          "concluíd", "concluid", "incluí", "incluid"})


def _cue_re(cues: tuple[str, ...], stems: frozenset[str]) -> re.Pattern:
    partes = []
    for c in cues:
        p = r"\b" + re.escape(c)
        if c not in stems:
            p += r"\b"
        partes.append(p)
    return re.compile("|".join(partes), re.IGNORECASE)


_QUERY_RE = _cue_re(_QUERY_CUES, _QUERY_STEMS)
_CLAIM_RE = _cue_re(_SUCCESS_CLAIMS, _CLAIM_STEMS)

_VALUE_RE = re.compile(r"(r\$\s*)?\d", re.IGNORECASE)


def is_financial_logging_intent(text: str) -> bool:
    """True se o texto parece pedir o REGISTRO de um movimento financeiro."""
    t = (text or "").lower()
    if not t.strip():
        return False
    if _QUERY_RE.search(t):
        return False
    if not any(c in t for c in _LOG_CUES):
        return False
    # exige um número/valor pra não pegar frases genéricas ("falei do cartão")
    return bool(_VALUE_RE.search(t))


def _reply_claims_success(reply: str) -> bool:
    return bool(_CLAIM_RE.search((reply or "").lower()))


GUARD_MESSAGE = (
    "⚠️ Não registrei isso no financeiro — a ação não foi executada, então "
    "evitei confirmar algo que não aconteceu. Pode repetir? Ex.: "
    '"lança 40 no débito, mercado, hoje".'
)


def guard_financial_reply(user_text: str, financial_logged_ok: bool, reply: str) -> str:
    """Retorna a resposta original, ou o aviso de blindagem quando detecta
    confirmação alucinada (intenção de lançamento + nada gravado + resposta
    alegando sucesso)."""
    if financial_logged_ok:
        return reply
    if not is_financial_logging_intent(user_text):
        return reply
    if not _reply_claims_success(reply):
        return reply  # ex.: o modelo pediu esclarecimento — deixa passar
    return GUARD_MESSAGE
