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
    "inseri", "salvo", "salvei", "gravd", "gravado", "gravei",
)

_VALUE_RE = re.compile(r"(r\$\s*)?\d", re.IGNORECASE)


def is_financial_logging_intent(text: str) -> bool:
    """True se o texto parece pedir o REGISTRO de um movimento financeiro."""
    t = (text or "").lower()
    if not t.strip():
        return False
    if any(q in t for q in _QUERY_CUES):
        return False
    if not any(c in t for c in _LOG_CUES):
        return False
    # exige um número/valor pra não pegar frases genéricas ("falei do cartão")
    return bool(_VALUE_RE.search(t))


def _reply_claims_success(reply: str) -> bool:
    r = (reply or "").lower()
    return any(c in r for c in _SUCCESS_CLAIMS)


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
