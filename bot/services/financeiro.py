"""Integração com gerenciador-financeiro (Firestore).

O gerenciador é um app React puro sem backend; persiste todo o `state` da
loja em um único doc `users/{uid}` no Firestore. Bot anexa entradas em
arrays nested (`bankTransactions`, `cardEntries`, `treasuryHoldings`)
dentro de transactions pra evitar race com escritas do frontend.

Credencial = service account JSON do Firebase, guardado em
`kv_settings.firebase_service_account_json`. Init é lazy + cached em
memória; rotação dispara reinit no próximo uso.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import string
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import KVSetting

logger = logging.getLogger(__name__)

FIREBASE_SA_KEY = "firebase_service_account_json"

# Estado global do SDK. Reinicializa quando _stored_fingerprint diverge do JSON
# atualmente carregado (rotação via /financeiro_setup).
_app = None  # firebase_admin.App
_db = None   # firestore.Client
_loaded_fingerprint: str | None = None
_init_lock = asyncio.Lock()


class FinanceiroError(Exception):
    pass


class NotConfiguredError(FinanceiroError):
    """Service account ou UID ainda não configurado."""


def _gen_id() -> str:
    """7 chars alfanuméricos minúsculos, mesmo padrão do uid() JS."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(7))


# Categorias padrão do gerenciador-financeiro (src/store.jsx → DEFAULT_CATEGORIES).
# Mantidas em sync manualmente; se mudarem, atualizar aqui.
DEFAULT_CATEGORIES: list[dict[str, str]] = [
    {"id": "alimentacao", "name": "Alimentação"},
    {"id": "transporte", "name": "Transporte"},
    {"id": "moradia", "name": "Moradia"},
    {"id": "saude", "name": "Saúde"},
    {"id": "lazer", "name": "Lazer"},
    {"id": "educacao", "name": "Educação"},
    {"id": "compras", "name": "Compras"},
    {"id": "servicos", "name": "Serviços"},
    {"id": "outros", "name": "Outros"},
]

# Sinônimos comuns → id de categoria padrão. Resolvedor passa por essa
# tabela quando o hint do LLM não bate diretamente com id/name de
# nenhuma categoria existente.
_CATEGORY_SYNONYMS: dict[str, str] = {
    # alimentacao
    "mercado": "alimentacao", "supermercado": "alimentacao", "feira": "alimentacao",
    "padaria": "alimentacao", "restaurante": "alimentacao", "lanche": "alimentacao",
    "lanchonete": "alimentacao", "ifood": "alimentacao", "rappi": "alimentacao",
    "hortifruti": "alimentacao", "acougue": "alimentacao", "comida": "alimentacao",
    "delivery": "alimentacao", "bar": "alimentacao",
    # transporte
    "uber": "transporte", "99": "transporte", "taxi": "transporte",
    "gasolina": "transporte", "posto": "transporte", "combustivel": "transporte",
    "etanol": "transporte", "diesel": "transporte", "metro": "transporte",
    "onibus": "transporte", "passagem": "transporte", "pedagio": "transporte",
    "estacionamento": "transporte", "passagem aerea": "transporte",
    # moradia
    "aluguel": "moradia", "condominio": "moradia", "luz": "moradia",
    "energia": "moradia", "agua": "moradia", "gas": "moradia",
    "internet": "moradia", "iptu": "moradia", "conta de luz": "moradia",
    "conta de agua": "moradia", "movel": "moradia", "moveis": "moradia",
    # saude
    "farmacia": "saude", "remedio": "saude", "medico": "saude",
    "hospital": "saude", "exame": "saude", "consulta": "saude",
    "plano de saude": "saude", "dentista": "saude", "psicologa": "saude",
    "psicologo": "saude", "academia": "saude",
    # lazer
    "netflix": "lazer", "spotify": "lazer", "cinema": "lazer",
    "show": "lazer", "viagem": "lazer", "hotel": "lazer",
    "passeio": "lazer", "festa": "lazer", "streaming": "lazer",
    "disney": "lazer", "hbo": "lazer", "prime video": "lazer",
    # educacao
    "escola": "educacao", "curso": "educacao", "livro": "educacao",
    "faculdade": "educacao", "mensalidade": "educacao", "udemy": "educacao",
    "alura": "educacao",
    # compras
    "roupa": "compras", "shopping": "compras", "presente": "compras",
    "eletronico": "compras", "magazine": "compras", "amazon": "compras",
    "mercado livre": "compras", "shopee": "compras", "calcado": "compras",
    "tenis": "compras",
    # servicos
    "encanador": "servicos", "eletricista": "servicos", "faxina": "servicos",
    "manutencao": "servicos", "lavanderia": "servicos", "barbeiro": "servicos",
    "cabelo": "servicos",
}


def _normalize_text(s: str) -> str:
    """Lowercase, sem acento, sem espaços extras."""
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\s+", " ", s)
    return s


def _effective_categories(state: dict) -> list[dict]:
    """Defaults + customCategories do state. customCategories sobrescrevem
    defaults se compartilharem id."""
    custom = state.get("customCategories") or []
    by_id: dict[str, dict] = {c["id"]: c for c in DEFAULT_CATEGORIES}
    for c in custom:
        if isinstance(c, dict) and c.get("id"):
            by_id[c["id"]] = c
    return list(by_id.values())


def _resolve_category_id(hint: str | None, state: dict) -> str:
    """Resolve um hint de categoria (vindo do LLM) pra um id válido.

    Ordem de tentativa:
      1. Match exato no id (normalizado)
      2. Match exato no name (normalizado)
      3. Substring no name (ex: 'plano de saude' bate em 'Saúde')
      4. Tabela de sinônimos
      5. Fallback 'outros'
    """
    if not hint:
        return "outros"
    cats = _effective_categories(state)
    norm_hint = _normalize_text(hint)
    if not norm_hint:
        return "outros"

    # 1 + 2
    for c in cats:
        if _normalize_text(c.get("id", "")) == norm_hint:
            return c["id"]
    for c in cats:
        if _normalize_text(c.get("name", "")) == norm_hint:
            return c["id"]
    # 3: substring contra nome
    for c in cats:
        nname = _normalize_text(c.get("name", ""))
        if nname and (norm_hint in nname or nname in norm_hint):
            return c["id"]
    # 4: sinônimos
    syn = _CATEGORY_SYNONYMS.get(norm_hint)
    if syn:
        # garante que o id resolvido existe no conjunto efetivo
        if any(c.get("id") == syn for c in cats):
            return syn
    return "outros"


async def _read_state(db, uid: str) -> dict:
    """Lê o doc do usuário e devolve state (ou {})."""
    ref = db.collection("users").document(uid)
    snap = await _run_blocking(ref.get)
    data = snap.to_dict() if snap.exists else {}
    return data.get("state") or {}


async def get_service_account_json(session: AsyncSession) -> str | None:
    row = await session.get(KVSetting, FIREBASE_SA_KEY)
    return row.value if row else None


async def save_service_account_json(session: AsyncSession, json_str: str) -> None:
    """Valida shape mínimo e grava em kv_settings (upsert).
    Levanta FinanceiroError em formato inválido."""
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise FinanceiroError(f"JSON inválido: {e}")
    if not isinstance(data, dict):
        raise FinanceiroError("JSON precisa ser um objeto.")
    required = ("type", "project_id", "private_key", "client_email")
    missing = [k for k in required if not data.get(k)]
    if missing:
        raise FinanceiroError(f"campos faltando: {', '.join(missing)}")
    if data.get("type") != "service_account":
        raise FinanceiroError(
            f"type='{data.get('type')}' inesperado (esperado 'service_account')."
        )

    row = await session.get(KVSetting, FIREBASE_SA_KEY)
    if row is None:
        row = KVSetting(key=FIREBASE_SA_KEY, value=json_str)
        session.add(row)
    else:
        row.value = json_str
    await session.commit()


def describe_credential(json_str: str) -> str:
    """Resumo curto pra exibir ao usuário (project_id + client_email)."""
    try:
        data = json.loads(json_str)
        return f"{data.get('project_id', '?')} ({data.get('client_email', '?')})"
    except Exception:
        return "(JSON inválido)"


def _fingerprint(json_str: str) -> str:
    # Suficiente pra detectar troca; não precisa ser hash criptográfico.
    return str(hash(json_str))


async def _ensure_initialized(json_str: str) -> None:
    """Garante que firebase_admin está inicializado com o JSON dado.
    Reinicializa se o JSON mudou."""
    global _app, _db, _loaded_fingerprint
    fp = _fingerprint(json_str)
    if _app is not None and _loaded_fingerprint == fp:
        return

    async with _init_lock:
        if _app is not None and _loaded_fingerprint == fp:
            return
        # Import lazy pra não pagar custo de import quando feature não é usada.
        import firebase_admin
        from firebase_admin import credentials, firestore

        if _app is not None:
            try:
                firebase_admin.delete_app(_app)
            except Exception:
                logger.exception("failed to delete old firebase app")

        cred = credentials.Certificate(json.loads(json_str))
        _app = firebase_admin.initialize_app(cred, name=f"concierge-{fp[-8:]}")
        _db = firestore.client(_app)
        _loaded_fingerprint = fp
        logger.info("firebase admin initialized")


async def _get_db(session: AsyncSession):
    json_str = await get_service_account_json(session)
    if not json_str:
        raise NotConfiguredError(
            "service account do Firebase não configurada. Use /financeiro_setup."
        )
    await _ensure_initialized(json_str)
    return _db


def _require_uid(user) -> str:
    if not getattr(user, "firebase_uid", None):
        raise NotConfiguredError(
            "seu UID do Firebase não está configurado. Use /financeiro_setup uid <uid>."
        )
    return user.firebase_uid


def _run_blocking(fn, *args, **kwargs):
    """firebase_admin é síncrono; rodar no executor pra não travar o loop."""
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(None, lambda: fn(*args, **kwargs))


def _append_in_transaction(db, uid: str, array_name: str, entry: dict) -> None:
    """Transaction: lê doc, anexa entry em state[array_name], escreve back.
    Bloqueante; chamar via _run_blocking."""
    from firebase_admin import firestore as _fs

    ref = db.collection("users").document(uid)

    @_fs.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        data = snap.to_dict() if snap.exists else {}
        state = dict(data.get("state") or {})
        arr = list(state.get(array_name) or [])
        arr.append(entry)
        state[array_name] = arr
        transaction.set(
            ref,
            {"state": state, "updatedAt": _fs.SERVER_TIMESTAMP},
            merge=True,
        )

    _txn(db.transaction())


class NotOwnedError(FinanceiroError):
    """Entrada existe mas não foi criada pelo bot (source != 'bot')."""


def _delete_from_state_array(db, uid: str, array_name: str, entry_id: str) -> dict | None:
    """Remove entrada por id de state[array_name] SOMENTE se source=='bot'.
    Retorna o item removido, None se id não existe, ou levanta
    NotOwnedError se a entrada foi criada por outro cliente."""
    from firebase_admin import firestore as _fs

    ref = db.collection("users").document(uid)

    @_fs.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        data = snap.to_dict() if snap.exists else {}
        state = dict(data.get("state") or {})
        arr = list(state.get(array_name) or [])
        target_idx = next(
            (i for i, it in enumerate(arr) if it.get("id") == entry_id),
            None,
        )
        if target_idx is None:
            return None
        target = arr[target_idx]
        if target.get("source") != "bot":
            raise NotOwnedError(
                f"lançamento {entry_id} não foi criado pelo bot — apague pelo app web."
            )
        new_arr = arr[:target_idx] + arr[target_idx + 1:]
        state[array_name] = new_arr
        transaction.set(
            ref,
            {"state": state, "updatedAt": _fs.SERVER_TIMESTAMP},
            merge=True,
        )
        return target

    return _txn(db.transaction())


def _delete_treasury_contribution(db, uid: str, contribution_id: str) -> dict | None:
    """Procura contribuição por id em todos os títulos. Retorna
    {'titulo': name, 'contribution': {...}} ou None."""
    from firebase_admin import firestore as _fs

    ref = db.collection("users").document(uid)

    @_fs.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        data = snap.to_dict() if snap.exists else {}
        state = dict(data.get("state") or {})
        holdings = list(state.get("treasuryHoldings") or [])
        for i, h in enumerate(holdings):
            contribs = list(h.get("contributions") or [])
            target_idx = next(
                (j for j, c in enumerate(contribs) if c.get("id") == contribution_id),
                None,
            )
            if target_idx is None:
                continue
            target = contribs[target_idx]
            if target.get("source") != "bot":
                raise NotOwnedError(
                    f"aporte {contribution_id} não foi criado pelo bot — apague pelo app web."
                )
            new_contribs = contribs[:target_idx] + contribs[target_idx + 1:]
            holding = dict(h)
            holding["contributions"] = new_contribs
            holdings[i] = holding
            state["treasuryHoldings"] = holdings
            transaction.set(
                ref,
                {"state": state, "updatedAt": _fs.SERVER_TIMESTAMP},
                merge=True,
            )
            return {"titulo": h.get("name", "?"), "contribution": target}
        return None

    return _txn(db.transaction())


def _set_treasury_contribution(
    db, uid: str, titulo_query: str, contribution: dict,
) -> str:
    """Procura título por nome (case-insensitive, match parcial) e anexa
    contribution em holding.contributions. Retorna o nome do título
    realmente atingido. Levanta FinanceiroError se não achar."""
    from firebase_admin import firestore as _fs

    ref = db.collection("users").document(uid)
    q = titulo_query.strip().lower()

    @_fs.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        data = snap.to_dict() if snap.exists else {}
        state = dict(data.get("state") or {})
        holdings = list(state.get("treasuryHoldings") or [])
        if not holdings:
            raise FinanceiroError(
                "nenhum título de Tesouro Direto cadastrado. Crie um pelo app web primeiro."
            )

        # Match: primeiro exato (case-insensitive), depois substring.
        idx_exact = next(
            (i for i, h in enumerate(holdings)
             if (h.get("name") or "").strip().lower() == q),
            None,
        )
        if idx_exact is not None:
            idx = idx_exact
        else:
            cand = [i for i, h in enumerate(holdings)
                    if q in (h.get("name") or "").strip().lower()]
            if not cand:
                names = ", ".join(h.get("name", "?") for h in holdings)
                raise FinanceiroError(
                    f"título '{titulo_query}' não encontrado. Disponíveis: {names}"
                )
            if len(cand) > 1:
                names = ", ".join(holdings[i].get("name", "?") for i in cand)
                raise FinanceiroError(
                    f"título ambíguo — match em vários: {names}. Seja mais específico."
                )
            idx = cand[0]

        holding = dict(holdings[idx])
        contribs = list(holding.get("contributions") or [])
        contribs.append(contribution)
        holding["contributions"] = contribs
        holdings[idx] = holding
        state["treasuryHoldings"] = holdings
        transaction.set(
            ref,
            {"state": state, "updatedAt": _fs.SERVER_TIMESTAMP},
            merge=True,
        )
        return holding.get("name", titulo_query)

    return _txn(db.transaction())


# -------- API pública (chamada pelas tools) --------

TIPO_CREDITO = {"credito", "crédito", "credit", "receita", "recebimento"}
TIPO_DEBITO = {"debito", "débito", "debit", "despesa", "pagamento", "gasto"}


async def lancar_movimento_banco(
    session: AsyncSession,
    user,
    desc: str,
    valor: float,
    tipo: str,
    data_iso: str,
    categoria: str = "outros",
    recorrente: bool = False,
) -> dict:
    uid = _require_uid(user)
    db = await _get_db(session)
    tipo_norm = (tipo or "").strip().lower()
    if tipo_norm in TIPO_CREDITO:
        type_str = "credit"
        amount = abs(valor)
    elif tipo_norm in TIPO_DEBITO:
        type_str = "debit"
        amount = -abs(valor)
    else:
        raise FinanceiroError(
            f"tipo '{tipo}' inválido (use credito/debito/despesa/receita)."
        )

    state = await _read_state(db, uid)
    category_id = _resolve_category_id(categoria, state)

    entry = {
        "id": _gen_id(),
        "date": data_iso,
        "desc": desc.strip(),
        "category": category_id,
        "amount": amount,
        "type": type_str,
        "source": "bot",
    }
    if recorrente:
        entry["recurring"] = True

    await _run_blocking(_append_in_transaction, db, uid, "bankTransactions", entry)
    return entry


async def lancar_despesa_cartao(
    session: AsyncSession,
    user,
    desc: str,
    valor: float,
    data_iso: str,
    categoria: str = "outros",
    parcelas: int = 1,
) -> dict:
    uid = _require_uid(user)
    db = await _get_db(session)
    parcelas = max(1, int(parcelas or 1))
    state = await _read_state(db, uid)
    category_id = _resolve_category_id(categoria, state)
    entry = {
        "id": _gen_id(),
        "date": data_iso,
        "desc": desc.strip(),
        "category": category_id,
        "amount": abs(float(valor)),
        "installments": parcelas,
        "currentInstallment": 1,
        "source": "bot",
    }
    await _run_blocking(_append_in_transaction, db, uid, "cardEntries", entry)
    return entry


async def registrar_aporte_tesouro(
    session: AsyncSession,
    user,
    titulo: str,
    valor: float,
    data_iso: str,
    taxa: float | None = None,
) -> dict:
    uid = _require_uid(user)
    db = await _get_db(session)
    contrib: dict[str, Any] = {
        "id": _gen_id(),
        "date": data_iso,
        "amount": abs(float(valor)),
        "source": "bot",
    }
    if taxa is not None:
        contrib["rate"] = float(taxa)

    matched_name = await _run_blocking(
        _set_treasury_contribution, db, uid, titulo, contrib,
    )
    return {"titulo": matched_name, "contribution": contrib}


async def apagar_lancamento(
    session: AsyncSession,
    user,
    modulo: str,
    entry_id: str,
) -> dict:
    """Apaga lançamento por id no módulo dado. Retorna o item removido.
    `modulo` ∈ {'banco', 'cartao', 'tesouro'}."""
    uid = _require_uid(user)
    db = await _get_db(session)
    mod = (modulo or "").strip().lower()
    if mod in ("banco", "bank"):
        removed = await _run_blocking(
            _delete_from_state_array, db, uid, "bankTransactions", entry_id,
        )
        if removed is None:
            raise FinanceiroError(f"lançamento {entry_id} não encontrado no banco.")
        return {"modulo": "banco", "removido": removed}
    if mod in ("cartao", "cartão", "card"):
        removed = await _run_blocking(
            _delete_from_state_array, db, uid, "cardEntries", entry_id,
        )
        if removed is None:
            raise FinanceiroError(f"compra {entry_id} não encontrada no cartão.")
        return {"modulo": "cartao", "removido": removed}
    if mod in ("tesouro", "treasury"):
        res = await _run_blocking(_delete_treasury_contribution, db, uid, entry_id)
        if res is None:
            raise FinanceiroError(f"aporte {entry_id} não encontrado no tesouro.")
        return {"modulo": "tesouro", **res}
    raise FinanceiroError(
        f"módulo '{modulo}' inválido (use 'banco', 'cartao' ou 'tesouro')."
    )


def _get_card_closing_day(state: dict) -> int | None:
    """Procura o dia de fechamento da fatura no settings do state.
    Suporta vários nomes (frontend ainda pode mudar). Retorna 1-31 ou None."""
    settings = state.get("settings") or {}
    for key in (
        "cardClosingDay", "closingDay", "card_closing_day",
        "diaFechamento", "dia_fechamento", "fechamentoCartao",
    ):
        v = settings.get(key)
        if isinstance(v, (int, float)) and 1 <= int(v) <= 31:
            return int(v)
    return None


def _bill_month_for_date(purchase_date: date, closing_day: int | None) -> tuple[int, int]:
    """Retorna (ano, mês) da fatura que contém uma compra nessa data.
    Sem closing_day: usa mês calendário (mês da própria data)."""
    if closing_day is None:
        return purchase_date.year, purchase_date.month
    if purchase_date.day > closing_day:
        if purchase_date.month == 12:
            return purchase_date.year + 1, 1
        return purchase_date.year, purchase_date.month + 1
    return purchase_date.year, purchase_date.month


def _entry_in_bill(
    entry: dict, target_year: int, target_month: int, closing_day: int | None,
) -> dict | None:
    """Decide se uma cardEntry aparece na fatura (target_year, target_month).
    Replica getCardBillForMonth do frontend, cobrindo:
    - compras à vista (installments=1)
    - parcelas (installments>1) — parcela N aparece no mês de início + (N-1)
    - recorrentes (recurring=true) — aparecem em todo mês a partir do início,
      respeitando recurringEndMonth e recurringExcludedMonths

    Retorna dict com 'kind' ('parcela' | 'recorrente' | 'avista'), 'value'
    (valor exibido na fatura), e campos extras dependendo do kind. None
    se a entry não entra nessa fatura.
    """
    try:
        pd = datetime.fromisoformat((entry.get("date") or "").replace(" ", "T")).date()
    except ValueError:
        return None

    target_key = f"{target_year:04d}-{target_month:02d}"
    start_year, start_month = _bill_month_for_date(pd, closing_day)
    start_key = f"{start_year:04d}-{start_month:02d}"
    if target_key < start_key:
        return None  # fatura é anterior à compra

    if entry.get("recurring"):
        end = entry.get("recurringEndMonth")
        if end and str(end) and target_key > str(end):
            return None
        excluded = entry.get("recurringExcludedMonths") or []
        if target_key in excluded:
            return None
        amt = float(entry.get("amount") or 0)
        return {"kind": "recorrente", "value": amt}

    installments = int(entry.get("installments") or 1)
    base_inst = int(entry.get("currentInstallment") or 1)
    months_diff = (target_year - start_year) * 12 + (target_month - start_month)
    current = base_inst + months_diff
    if not (1 <= current <= installments):
        return None

    amt_total = float(entry.get("amount") or 0)
    value = amt_total / installments if installments else amt_total
    if installments > 1:
        return {
            "kind": "parcela",
            "num": current,
            "total": installments,
            "value": value,
        }
    return {"kind": "avista", "value": value}


def _open_invoice_range(state: dict, today: date) -> tuple[date, date, str]:
    """Calcula intervalo [início, fim] da fatura em aberto, mais um label.
    Compras com date no intervalo fazem parte da fatura corrente.

    Com closing day D:
      - se today.day > D: fatura em aberto vai de (D+1 do mês atual) até
        (D do mês seguinte). today está dentro.
      - se today.day <= D: ainda dentro da fatura aberta do mês anterior,
        que vai de (D+1 do mês anterior) até (D do mês atual).
    Sem closing day configurado: mês calendário (dia 1 → último dia).
    """
    from calendar import monthrange

    closing = _get_card_closing_day(state)
    if closing is None:
        first = today.replace(day=1)
        last_day = monthrange(today.year, today.month)[1]
        last = today.replace(day=last_day)
        return first, last, f"fatura em aberto ({first.strftime('%d/%m')} → {last.strftime('%d/%m')}, mês calendário)"

    if today.day > closing:
        # fatura abriu neste mês, fecha no próximo
        start_year, start_month = today.year, today.month
    else:
        # fatura abriu no mês passado, fecha neste
        if today.month == 1:
            start_year, start_month = today.year - 1, 12
        else:
            start_year, start_month = today.year, today.month - 1

    last_day_start_month = monthrange(start_year, start_month)[1]
    start_day = min(closing + 1, last_day_start_month)
    start = date(start_year, start_month, start_day)

    if start_month == 12:
        end_year, end_month = start_year + 1, 1
    else:
        end_year, end_month = start_year, start_month + 1
    last_day_end_month = monthrange(end_year, end_month)[1]
    end_day = min(closing, last_day_end_month)
    end = date(end_year, end_month, end_day)

    return start, end, (
        f"fatura em aberto ({start.strftime('%d/%m')} → "
        f"{end.strftime('%d/%m')}, fechamento dia {closing})"
    )


def _filter_by_range(arr: list[dict], start: date, end: date) -> list[dict]:
    out = []
    for it in arr:
        try:
            d = datetime.fromisoformat(it.get("date", "")).date()
        except ValueError:
            continue
        if start <= d <= end:
            out.append(it)
    return out


def _filter_by_days(arr: list[dict], dias: int, today_iso: str) -> list[dict]:
    """Itens com `date` >= (today - dias)."""
    try:
        today_d = datetime.fromisoformat(today_iso).date()
    except ValueError:
        return arr
    cutoff = today_d - timedelta(days=dias)
    out = []
    for it in arr:
        try:
            d = datetime.fromisoformat(it.get("date", "")).date()
        except ValueError:
            continue
        if d >= cutoff:
            out.append(it)
    return out


async def consultar_lancamentos(
    session: AsyncSession,
    user,
    modulo: str,
    dias: int,
    today_iso: str,
    escopo_cartao: str = "fatura_aberta",
) -> str:
    uid = _require_uid(user)
    db = await _get_db(session)
    state = await _read_state(db, uid)

    parts: list[str] = []
    mods = {"banco", "cartao", "cartão", "tesouro", "tudo"}
    mod = (modulo or "tudo").strip().lower()
    if mod not in mods:
        raise FinanceiroError(f"módulo '{modulo}' inválido (use {sorted(mods)}).")

    if mod in ("banco", "tudo"):
        bank = _filter_by_days(state.get("bankTransactions") or [], dias, today_iso)
        if not bank:
            parts.append(f"banco ({dias}d): sem lançamentos")
        else:
            lines = [f"banco ({dias}d):"]
            saldo = 0.0
            for it in bank[-15:]:
                amt = float(it.get("amount") or 0)
                saldo += amt
                sign = "+" if amt >= 0 else ""
                tag = "bot" if it.get("source") == "bot" else "web"
                lines.append(
                    f"  [{it.get('id', '?')}|{tag}] {it.get('date', '?')} {sign}{amt:.2f} "
                    f"{it.get('desc', '?')} [{it.get('category', '?')}]"
                )
            lines.append(f"  saldo do período: {saldo:+.2f}")
            parts.append("\n".join(lines))

    if mod in ("cartao", "cartão", "tudo"):
        try:
            today_d = datetime.fromisoformat(today_iso).date()
        except ValueError:
            today_d = datetime.utcnow().date()

        all_card = state.get("cardEntries") or []
        closing = _get_card_closing_day(state)

        if escopo_cartao == "ultimos_dias":
            card_items: list[tuple[dict, dict]] = []
            for it in _filter_by_days(all_card, dias, today_iso):
                amt_total = float(it.get("amount") or 0)
                installments = int(it.get("installments") or 1)
                value = amt_total / installments if installments > 1 else amt_total
                kind = "recorrente" if it.get("recurring") else (
                    "parcela" if installments > 1 else "avista"
                )
                card_items.append((it, {"kind": kind, "value": value}))
            header = f"cartão de crédito (últimos {dias}d, pela data de compra)"
        else:
            # Fatura em aberto = a que está acumulando (fechará na próxima data
            # de fechamento). Replica getCardBillForMonth do frontend.
            target_year, target_month = _bill_month_for_date(today_d, closing)
            _, _, range_label = _open_invoice_range(state, today_d)
            card_items = []
            for it in all_card:
                info = _entry_in_bill(it, target_year, target_month, closing)
                if info is not None:
                    card_items.append((it, info))
            header = (
                f"cartão de crédito — {range_label}, fecha em "
                f"{target_month:02d}/{target_year}"
            )

        if not card_items:
            parts.append(f"{header}: sem compras")
        else:
            lines = [f"{header}:"]
            total = 0.0
            for it, info in card_items[-30:]:
                value = float(info.get("value") or 0)
                kind = info.get("kind")
                if kind == "parcela":
                    par_label = f" ({info['num']}/{info['total']})"
                elif kind == "recorrente":
                    par_label = " (recorrente)"
                else:
                    par_label = ""
                total += value
                tag = "bot" if it.get("source") == "bot" else "web"
                lines.append(
                    f"  [{it.get('id', '?')}|{tag}] {it.get('date', '?')} "
                    f"-R$ {value:.2f} "
                    f"{it.get('desc', '?')}{par_label} [{it.get('category', '?')}]"
                )
            lines.append(
                f"  total da fatura: -R$ {total:.2f} ({len(card_items)} item(ns))"
            )
            parts.append("\n".join(lines))

    if mod in ("tesouro", "tudo"):
        holdings = state.get("treasuryHoldings") or []
        if not holdings:
            parts.append("tesouro: nenhum título cadastrado")
        else:
            lines = ["tesouro:"]
            for h in holdings:
                name = h.get("name", "?")
                contribs = h.get("contributions") or []
                recent = _filter_by_days(contribs, dias, today_iso)
                total_aportado = sum(float(c.get("amount") or 0) for c in contribs)
                recent_amount = sum(float(c.get("amount") or 0) for c in recent)
                lines.append(
                    f"  • {name} — total aportado R$ {total_aportado:.2f}"
                )
                if recent:
                    lines.append(
                        f"      últimos {dias}d: +R$ {recent_amount:.2f} em {len(recent)} aporte(s):"
                    )
                    for c in recent[-10:]:
                        tag = "bot" if c.get("source") == "bot" else "web"
                        lines.append(
                            f"        [{c.get('id', '?')}|{tag}] {c.get('date', '?')} "
                            f"+R$ {float(c.get('amount') or 0):.2f}"
                        )
            parts.append("\n".join(lines))

    return "\n\n".join(parts) if parts else "(sem dados)"


async def build_card_closing_summary(
    session: AsyncSession, user, today: date,
) -> str | None:
    """Sumário enviado quando a fatura do cartão fecha (cardClosingDay).

    Retorna o texto pronto (HTML do Telegram) ou None se:
      - service account não configurado
      - usuário sem firebase_uid
      - sem closingDay no state.settings
      - hoje != closingDay
    Não levanta exceção; loga e devolve None em qualquer falha de leitura.
    """
    try:
        if not getattr(user, "firebase_uid", None):
            return None
        db = await _get_db(session)
        state = await _read_state(db, user.firebase_uid)
    except NotConfiguredError:
        return None
    except Exception:
        logger.exception("card summary: failed to read state for user ", user.id)
        return None

    settings_d = state.get("settings") or {}
    closing = _get_card_closing_day(state)
    if closing is None:
        return None
    if today.day != closing:
        return None

    target_y, target_m = today.year, today.month
    items: list[tuple[dict, dict]] = []
    for e in state.get("cardEntries") or []:
        info = _entry_in_bill(e, target_y, target_m, closing)
        if info:
            items.append((e, info))

    if not items:
        return (
            f"🧾 <b>Sua fatura do cartão fechou hoje</b> ({today.strftime('%d/%m')}). "
            "Sem compras nessa fatura. 🎉"
        )

    total = sum(float(i.get("value") or 0) for _, i in items)

    # Top categorias
    by_cat: dict[str, float] = {}
    for e, i in items:
        cat = (e.get("category") or "outros")
        by_cat[cat] = by_cat.get(cat, 0.0) + float(i.get("value") or 0)
    top_cats = sorted(by_cat.items(), key=lambda x: -x[1])[:3]

    # Mapeia id → name pra exibir bonito
    cats = _effective_categories(state)
    cat_name_by_id = {c["id"]: c.get("name") or c["id"] for c in cats}

    # Vencimento (cardDueDay no próximo mês, clamped)
    from calendar import monthrange
    due_label = ""
    due_day_raw = settings_d.get("cardDueDay")
    if isinstance(due_day_raw, (int, float)) and 1 <= int(due_day_raw) <= 31:
        due_day = int(due_day_raw)
        if today.month == 12:
            dy, dm = today.year + 1, 1
        else:
            dy, dm = today.year, today.month + 1
        last_day = monthrange(dy, dm)[1]
        due_d = min(due_day, last_day)
        due_label = f" — vence em <b>{due_d:02d}/{dm:02d}</b>"

    lines = [
        f"🧾 <b>Fatura do cartão fechou hoje</b> ({today.strftime('%d/%m')}){due_label}",
        f"Total: <b>R$ {total:,.2f}</b> em {len(items)} lançamento(s)".replace(",", "X").replace(".", ",").replace("X", "."),
        "",
        "<i>Top categorias:</i>",
    ]
    for cat_id, val in top_cats:
        name = cat_name_by_id.get(cat_id, cat_id)
        val_fmt = f"R$ {val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        lines.append(f"  • {name}: {val_fmt}")

    # Limite usado (se configurado)
    limit_raw = settings_d.get("cardLimit")
    if isinstance(limit_raw, (int, float)) and float(limit_raw) > 0:
        pct = (total / float(limit_raw)) * 100
        emoji = "🟢" if pct < 70 else ("🟡" if pct < 90 else "🔴")
        lines.append("")
        lines.append(
            f"{emoji} Limite usado: <b>{pct:.0f}%</b> de R$ {float(limit_raw):,.2f}".replace(
                ",", "X"
            ).replace(".", ",").replace("X", ".")
        )

    return "\n".join(lines)
