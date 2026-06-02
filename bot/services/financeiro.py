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
import concurrent.futures
import json
import logging
import re
import secrets
import string
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any

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


# Executor DEDICADO pro firebase-admin (síncrono, com chamadas gRPC ao Google
# que podem demorar). Isolar do executor default impede que o Firestore
# monopolize as threads que o aiohttp usa pra resolver DNS dos envios ao
# Telegram (senão os sends estouram timeout parecendo erro de rede).
_FB_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="firebase"
)


def _run_blocking(fn, *args, **kwargs):
    """firebase_admin é síncrono; rodar num executor dedicado pra não travar o
    loop nem disputar threads com DNS/LLM no executor default."""
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(_FB_EXECUTOR, lambda: fn(*args, **kwargs))


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


# ---- Investimentos (state.investments.assets) ----
# Schema do app React (gerenciador-financeiro/src/investimentos.jsx):
#   state.investments.assets = [
#     { id, ticker, name, class, currentPrice, lastPriceUpdate,
#       operations: [{id,date,type:"buy"|"sell",qty,price}],
#       dividends:  [{id,date,amount}] }
#   ]
# class ∈ {"acoes","fiis","etfs","rf","fundos","cripto"}.

ASSET_CLASSES_VALID = {"acoes", "fiis", "etfs", "rf", "fundos", "cripto"}
ASSET_CLASSES_LABEL = {
    "acoes": "Ações",
    "fiis": "Fundos Imobiliários",
    "etfs": "ETFs",
    "rf": "Renda Fixa",
    "fundos": "Fundos",
    "cripto": "Cripto",
}
ASSET_CLASS_ALIAS = {
    # PT-BR / variantes que o LLM pode passar
    "ação": "acoes", "acao": "acoes", "ações": "acoes", "acoes": "acoes",
    "stock": "acoes", "stocks": "acoes",
    "fii": "fiis", "fiis": "fiis", "fii11": "fiis", "fundo imobiliário": "fiis",
    "fundo imobiliario": "fiis", "fundos imobiliários": "fiis",
    "etf": "etfs", "etfs": "etfs",
    "rf": "rf", "renda fixa": "rf", "renda-fixa": "rf",
    "cdb": "rf", "lci": "rf", "lca": "rf", "debenture": "rf", "debênture": "rf",
    "fundo": "fundos", "fundos": "fundos", "fundo di": "fundos", "fia": "fundos",
    "cripto": "cripto", "crypto": "cripto", "criptomoeda": "cripto", "bitcoin": "cripto",
}


def _normalize_asset_class(raw: str) -> str:
    """Normaliza classe vinda do LLM pra um dos ids canônicos do React."""
    if not raw:
        raise FinanceiroError("classe do ativo é obrigatória (acoes/fiis/etfs/rf/fundos/cripto)")
    k = raw.strip().lower()
    if k in ASSET_CLASSES_VALID:
        return k
    if k in ASSET_CLASS_ALIAS:
        return ASSET_CLASS_ALIAS[k]
    raise FinanceiroError(
        f"classe '{raw}' inválida. Use uma de: {sorted(ASSET_CLASSES_VALID)}"
    )


def _set_investment_operation(
    db, uid: str, *, ticker: str, name: str | None, klass: str,
    op_type: str, qty: float, price: float, data_iso: str,
) -> dict:
    """Anexa uma operação (buy/sell) ao asset que casa por ticker+class.
    Se não existe, cria novo asset com currentPrice = price (igual ao
    comportamento do form React quando mode='new'). Retorna
    {'asset_name','asset_class','operation','was_new'}.
    """
    from firebase_admin import firestore as _fs

    ref = db.collection("users").document(uid)
    ticker_u = (ticker or "").strip().upper()
    if not ticker_u:
        raise FinanceiroError("ticker é obrigatório")
    if op_type not in ("buy", "sell"):
        raise FinanceiroError(f"op_type '{op_type}' inválido (use 'buy' ou 'sell')")
    if qty <= 0 or price < 0:
        raise FinanceiroError("qty deve ser > 0 e price >= 0")

    @_fs.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        data = snap.to_dict() if snap.exists else {}
        state = dict(data.get("state") or {})
        invest = dict(state.get("investments") or {})
        assets = list(invest.get("assets") or [])

        idx = next(
            (i for i, a in enumerate(assets)
             if (a.get("ticker") or "").strip().upper() == ticker_u
             and (a.get("class") or "") == klass),
            None,
        )

        op = {
            "id": _gen_id(),
            "date": data_iso,
            "type": op_type,
            "qty": float(qty),
            "price": float(price),
            "source": "bot",
        }
        was_new = idx is None
        if was_new:
            asset = {
                "id": _gen_id(),
                "ticker": ticker_u,
                "name": (name or ticker_u).strip(),
                "class": klass,
                "currentPrice": float(price),
                "lastPriceUpdate": data_iso,
                "operations": [op],
                "dividends": [],
                "source": "bot",
            }
            assets.append(asset)
            asset_out = asset
        else:
            asset = dict(assets[idx])
            ops = list(asset.get("operations") or [])
            ops.append(op)
            asset["operations"] = ops
            assets[idx] = asset
            asset_out = asset

        invest["assets"] = assets
        state["investments"] = invest
        transaction.set(
            ref,
            {"state": state, "updatedAt": _fs.SERVER_TIMESTAMP},
            merge=True,
        )
        return {
            "asset_name": asset_out.get("name", ticker_u),
            "asset_class": klass,
            "asset_id": asset_out.get("id"),
            "operation": op,
            "was_new": was_new,
        }

    return _txn(db.transaction())


def _delete_investment_operation(db, uid: str, op_id: str) -> dict | None:
    """Remove uma operação por id (procura em todos os assets). Só apaga se
    source=='bot'. Retorna {'asset_name','asset_class','operation'} ou None."""
    from firebase_admin import firestore as _fs

    ref = db.collection("users").document(uid)

    @_fs.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        data = snap.to_dict() if snap.exists else {}
        state = dict(data.get("state") or {})
        invest = dict(state.get("investments") or {})
        assets = list(invest.get("assets") or [])
        for i, a in enumerate(assets):
            ops = list(a.get("operations") or [])
            target_idx = next(
                (j for j, o in enumerate(ops) if o.get("id") == op_id),
                None,
            )
            if target_idx is None:
                continue
            target = ops[target_idx]
            if target.get("source") != "bot":
                raise NotOwnedError(
                    f"operação {op_id} não foi criada pelo bot — apague pelo app web."
                )
            new_ops = ops[:target_idx] + ops[target_idx + 1:]
            asset = dict(a)
            asset["operations"] = new_ops
            assets[i] = asset
            invest["assets"] = assets
            state["investments"] = invest
            transaction.set(
                ref,
                {"state": state, "updatedAt": _fs.SERVER_TIMESTAMP},
                merge=True,
            )
            return {
                "asset_name": a.get("name", "?"),
                "asset_class": a.get("class", "?"),
                "operation": target,
            }
        return None

    return _txn(db.transaction())


def _compute_asset_metrics(asset: dict) -> dict:
    """Replica computeAssetMetrics() do React: PM via FIFO simplificado,
    posição = qty * currentPrice, P&L = posição - investido."""
    ops = sorted(
        (asset.get("operations") or []),
        key=lambda o: str(o.get("date") or ""),
    )
    qty = 0.0
    total_cost = 0.0
    for op in ops:
        try:
            q = float(op.get("qty") or 0)
            p = float(op.get("price") or 0)
        except (TypeError, ValueError):
            continue
        t = op.get("type")
        if t == "buy":
            qty += q
            total_cost += q * p
        elif t == "sell":
            pm = (total_cost / qty) if qty > 0 else 0
            qty -= q
            total_cost -= q * pm
            if qty < 1e-7:
                qty = 0.0
                total_cost = 0.0
    pm = (total_cost / qty) if qty > 0 else 0.0
    try:
        current_price = float(asset.get("currentPrice") or 0) or pm
    except (TypeError, ValueError):
        current_price = pm
    position = qty * current_price
    invested = qty * pm
    pnl = position - invested
    div_total = sum(
        float(d.get("amount") or 0) for d in (asset.get("dividends") or [])
    )
    return {
        "qty": qty, "pm": pm, "currentPrice": current_price,
        "position": position, "invested": invested, "pnl": pnl,
        "divTotal": div_total,
    }


# ---- Cotação de carteira (B3) ----

# Classes com ticker de bolsa cotável via brapi (sem Tesouro/RF/fundos/cripto).
QUOTABLE_CLASSES = {"acoes", "fiis", "etfs"}


async def get_carteira_tickers(session: AsyncSession, user) -> list[str] | None:
    """Tickers únicos da carteira (classes cotáveis, com posição > 0).
    None se sem uid/SA. [] se não há ativos cotáveis."""
    try:
        if not getattr(user, "firebase_uid", None):
            return None
        db = await _get_db(session)
        state = await _read_state(db, user.firebase_uid)
    except NotConfiguredError:
        return None
    except Exception:
        logger.exception("get_carteira_tickers: failed for user %s", getattr(user, "id", "?"))
        return None
    invest = state.get("investments") or {}
    tickers: set[str] = set()
    for a in (invest.get("assets") or []):
        if (a.get("class") or "") not in QUOTABLE_CLASSES:
            continue
        if _compute_asset_metrics(a)["qty"] <= 0:
            continue
        t = (a.get("ticker") or "").strip().upper()
        if t:
            tickers.add(t)
    return sorted(tickers)


def _update_prices_in_transaction(db, uid: str, prices: dict[str, float]) -> list[dict]:
    """Transaction: atualiza currentPrice+lastPriceUpdate dos assets cujo
    ticker está em `prices`. Retorna a lista de assets (cotáveis, qty>0)
    pós-atualização pra montar a revisão. Bloqueante; via _run_blocking."""
    from datetime import date as _date

    from firebase_admin import firestore as _fs

    ref = db.collection("users").document(uid)
    today_iso = _date.today().isoformat()

    @_fs.transactional
    def _txn(transaction):
        snap = ref.get(transaction=transaction)
        data = snap.to_dict() if snap.exists else {}
        state = dict(data.get("state") or {})
        invest = dict(state.get("investments") or {})
        assets = list(invest.get("assets") or [])
        changed = False
        for i, a in enumerate(assets):
            t = (a.get("ticker") or "").strip().upper()
            if t in prices:
                asset = dict(a)
                asset["currentPrice"] = float(prices[t])
                asset["lastPriceUpdate"] = today_iso
                assets[i] = asset
                changed = True
        if changed:
            invest["assets"] = assets
            state["investments"] = invest
            transaction.set(
                ref,
                {"state": state, "updatedAt": _fs.SERVER_TIMESTAMP},
                merge=True,
            )
        return assets

    return _txn(db.transaction())


async def atualizar_cotacoes_carteira(
    session: AsyncSession, user, prices: dict[str, float],
) -> list[dict]:
    """Persiste as cotações no Firestore (currentPrice) e devolve os assets
    pós-atualização. Levanta NotConfiguredError se sem uid/SA."""
    uid = _require_uid(user)
    db = await _get_db(session)
    return await _run_blocking(_update_prices_in_transaction, db, uid, prices)


def format_carteira_review(assets: list[dict], prices: dict[str, float]) -> str | None:
    """Monta a revisão da carteira como TABELA alinhada (bloco monoespaçado
    <pre> do Telegram): por ativo cotável com posição, mostra investido,
    valor de mercado atual e P&L (% + emoji). Só inclui ativos que receberam
    cotação fresca em `prices`. Retorna None se nada a mostrar."""
    rows: list[tuple[dict, dict]] = []
    for a in assets:
        if (a.get("class") or "") not in QUOTABLE_CLASSES:
            continue
        t = (a.get("ticker") or "").strip().upper()
        if t not in prices:
            continue
        m = _compute_asset_metrics(a)
        if m["qty"] <= 0:
            continue
        rows.append((a, m))
    if not rows:
        return None

    rows.sort(key=lambda x: x[1]["position"], reverse=True)
    total_inv = sum(m["invested"] for _, m in rows)
    total_mkt = sum(m["position"] for _, m in rows)
    total_pnl = total_mkt - total_inv

    def _brl_plain(v: float) -> str:
        # "3.200,00" sem o prefixo R$ (a coluna já é monetária).
        return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    def _pct(pnl: float, base: float) -> str:
        p = (pnl / base * 100) if base > 0 else 0.0
        sign = "+" if pnl >= 0 else "−"
        emoji = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
        return f"{sign}{abs(p):.1f}%".replace(".", ",") + f" {emoji}"

    # Larguras das colunas numéricas (alinhadas à direita).
    tickers = [a.get("ticker", "?") for a, _ in rows] + ["Total"]
    inv_strs = [_brl_plain(m["invested"]) for _, m in rows] + [_brl_plain(total_inv)]
    mkt_strs = [_brl_plain(m["position"]) for _, m in rows] + [_brl_plain(total_mkt)]
    w_tick = max(len("Ativo"), *(len(t) for t in tickers))
    w_inv = max(len("Investido"), *(len(s) for s in inv_strs))
    w_mkt = max(len("Mercado"), *(len(s) for s in mkt_strs))

    out: list[str] = ["<pre>"]
    out.append(f" {'Ativo':<{w_tick}}  {'Investido':>{w_inv}}  {'Mercado':>{w_mkt}}   P&L")
    for (a, m), inv_s, mkt_s in zip(rows, inv_strs[:-1], mkt_strs[:-1]):
        out.append(
            f" {a.get('ticker', '?'):<{w_tick}}  {inv_s:>{w_inv}}  {mkt_s:>{w_mkt}}   "
            f"{_pct(m['pnl'], m['invested'])}"
        )
    sep_len = w_tick + w_inv + w_mkt + 13
    out.append(" " + "─" * sep_len)
    out.append(
        f" {'Total':<{w_tick}}  {inv_strs[-1]:>{w_inv}}  {mkt_strs[-1]:>{w_mkt}}   "
        f"{_pct(total_pnl, total_inv)}"
    )
    out.append("</pre>")
    return "\n".join(out)


# ---- Fim Investimentos ----


# ---- Saldo / Patrimônio (replica Visão Geral do app React) ----

def _get_bank_balance(state: dict) -> float:
    """Saldo bancário ATUAL = soma de TODAS bankTransactions[].amount
    (sem filtro de data). Replica getBankBalance() do store.jsx."""
    return sum(
        float(t.get("amount") or 0)
        for t in (state.get("bankTransactions") or [])
    )


def _get_month_bank_summary(state: dict, today: date) -> dict:
    """Entradas/saídas do mês corrente (somente bankTransactions).
    Retorna {entradas, saidas, saldo}. 'saidas' é positivo (mag),
    'saldo' = entradas - saidas."""
    entradas = 0.0
    saidas = 0.0
    for t in (state.get("bankTransactions") or []):
        try:
            d = datetime.fromisoformat((t.get("date") or "")[:10]).date()
        except ValueError:
            continue
        if d.year != today.year or d.month != today.month:
            continue
        amt = float(t.get("amount") or 0)
        if amt >= 0:
            entradas += amt
        else:
            saidas += -amt
    return {
        "entradas": entradas,
        "saidas": saidas,
        "saldo": entradas - saidas,
    }


def _project_contribution_to_today(
    contrib: dict, holding_rate: float, ipca: float, today: date,
) -> float:
    """Projeta o valor de um aporte do Tesouro pra hoje com juros compostos
    anuais (1+ipca)*(1+r)-1. Replica projectContributionToToday() do
    store.jsx. Per-contribution override em contrib.rate."""
    try:
        amount = float(contrib.get("amount") or 0)
    except (TypeError, ValueError):
        return 0.0
    try:
        start = datetime.fromisoformat((contrib.get("date") or "")[:10]).date()
    except ValueError:
        return amount  # sem data: sem projeção
    # contributionRate: override do aporte se houver, senão a do título
    r_override = contrib.get("rate")
    try:
        r = float(r_override) if r_override is not None else float(holding_rate or 0)
    except (TypeError, ValueError):
        r = float(holding_rate or 0)
    try:
        i = float(ipca or 0)
    except (TypeError, ValueError):
        i = 0.0
    years = max(0.0, (today - start).days / 365.25)
    annual = (1.0 + i) * (1.0 + r) - 1.0
    return amount * ((1.0 + annual) ** years)


def _get_treasury_current_value(state: dict, today: date) -> float:
    """Soma do valor projetado pra hoje de TODOS os aportes em
    treasuryHoldings. Replica holdingCurrentValue() do store.jsx."""
    total = 0.0
    for h in (state.get("treasuryHoldings") or []):
        rate = h.get("rate")
        ipca = h.get("ipcaAssumption")
        for c in (h.get("contributions") or []):
            total += _project_contribution_to_today(c, rate, ipca, today)
    return total


def _get_assets_position(state: dict) -> float:
    """Soma de qty*currentPrice de TODOS os ativos em
    investments.assets (ações/FIIs/ETFs/RF/fundos/cripto)."""
    invest = state.get("investments") or {}
    total = 0.0
    for a in (invest.get("assets") or []):
        m = _compute_asset_metrics(a)
        total += m["position"]
    return total


def _month_label(d: date) -> str:
    meses = [
        "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
        "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
    ]
    return f"{meses[d.month - 1]}/{d.year}"


async def consultar_saldo(
    session: AsyncSession, user, today: date,
) -> str:
    """Retorna a 'Visão Geral' do gerenciador-financeiro: saldo bancário
    atual, entradas/saídas do mês, investimentos e patrimônio total.
    Replica os mesmos cálculos do app React em src/store.jsx."""
    uid = _require_uid(user)
    db = await _get_db(session)
    state = await _read_state(db, uid)

    saldo = _get_bank_balance(state)
    mes = _get_month_bank_summary(state, today)
    tesouro = _get_treasury_current_value(state, today)
    assets = _get_assets_position(state)
    investimentos = tesouro + assets

    sign_mes = "+" if mes["saldo"] >= 0 else "−"
    parts = [
        f"💰 Saldo bancário atual: {_fmt_brl(saldo)}",
        "",
        f"📅 {_month_label(today)}:",
        f"   ➕ Entradas: {_fmt_brl(mes['entradas'])}",
        f"   ➖ Saídas:  {_fmt_brl(mes['saidas'])}",
        f"   = Saldo:   {sign_mes}{_fmt_brl(abs(mes['saldo']))}",
    ]
    if investimentos > 0:
        parts.append("")
        parts.append(f"📊 Investimentos: {_fmt_brl(investimentos)}")
        if tesouro > 0 and assets > 0:
            parts.append(f"   🏛️ Tesouro: {_fmt_brl(tesouro)}")
            parts.append(f"   📈 Carteira: {_fmt_brl(assets)}")
    return "\n".join(parts)


# ---- Fim Saldo / Patrimônio ----


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


async def registrar_operacao_ativo(
    session: AsyncSession,
    user,
    ticker: str,
    classe: str,
    op_type: str,
    qty: float,
    price: float,
    data_iso: str,
    nome: str | None = None,
) -> dict:
    """Registra compra/venda em state.investments.assets respeitando o
    schema do gerenciador-financeiro/src/investimentos.jsx. Casa por
    (ticker, classe); se não existir, cria asset novo com
    currentPrice = price (mesmo comportamento do form React)."""
    uid = _require_uid(user)
    db = await _get_db(session)
    klass = _normalize_asset_class(classe)
    t = (op_type or "").strip().lower()
    if t in ("compra", "buy", "c"):
        t = "buy"
    elif t in ("venda", "sell", "v"):
        t = "sell"
    else:
        raise FinanceiroError(
            f"op_type '{op_type}' inválido (use compra/venda ou buy/sell)"
        )
    return await _run_blocking(
        _set_investment_operation, db, uid,
        ticker=ticker, name=nome, klass=klass,
        op_type=t, qty=float(qty), price=float(price), data_iso=data_iso,
    )


async def apagar_lancamento(
    session: AsyncSession,
    user,
    modulo: str,
    entry_id: str,
) -> dict:
    """Apaga lançamento por id no módulo dado. Retorna o item removido.
    `modulo` ∈ {'banco', 'cartao', 'tesouro', 'investimento'}."""
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
    if mod in ("investimento", "investimentos", "ativo", "asset"):
        res = await _run_blocking(_delete_investment_operation, db, uid, entry_id)
        if res is None:
            raise FinanceiroError(f"operação {entry_id} não encontrada nos investimentos.")
        return {"modulo": "investimento", **res}
    raise FinanceiroError(
        f"módulo '{modulo}' inválido "
        f"(use 'banco', 'cartao', 'tesouro' ou 'investimento')."
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


def _get_card_due_day(state: dict) -> int | None:
    """Dia de VENCIMENTO da fatura (cardDueDay) no settings. 1-31 ou None."""
    s = state.get("settings") or {}
    for key in (
        "cardDueDay", "dueDay", "card_due_day",
        "diaVencimento", "dia_vencimento", "vencimentoCartao",
    ):
        v = s.get(key)
        if isinstance(v, (int, float)) and 1 <= int(v) <= 31:
            return int(v)
    return None


def _card_due_date(due_day: int, today: date) -> date:
    """Próxima data de vencimento (>= hoje) dado o dia do mês, com clamp de
    fim de mês. Função pura (testável sem Firestore)."""
    from calendar import monthrange
    y, m = today.year, today.month
    cand = date(y, m, min(due_day, monthrange(y, m)[1]))
    if cand < today:
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        cand = date(y, m, min(due_day, monthrange(y, m)[1]))
    return cand


async def card_due_soon(session: AsyncSession, user, today: date, lookahead_days: int) -> dict | None:
    """{'due_date', 'month_key'} se o vencimento da fatura cai em até
    lookahead_days; senão None. Defensivo: None sem uid/SA/cardDueDay."""
    try:
        if not getattr(user, "firebase_uid", None):
            return None
        db = await _get_db(session)
        state = await _read_state(db, user.firebase_uid)
    except NotConfiguredError:
        return None
    except Exception:
        logger.exception("card_due_soon: failed for user %s", getattr(user, "id", "?"))
        return None
    due_day = _get_card_due_day(state)
    if due_day is None:
        return None
    due = _card_due_date(due_day, today)
    if 0 <= (due - today).days <= lookahead_days:
        return {"due_date": due, "month_key": due.strftime("%Y-%m")}
    return None


async def last_finance_activity(session: AsyncSession, user) -> date | None:
    """Maior `date` entre bankTransactions e cardEntries; None se nada/sem uid."""
    try:
        if not getattr(user, "firebase_uid", None):
            return None
        db = await _get_db(session)
        state = await _read_state(db, user.firebase_uid)
    except NotConfiguredError:
        return None
    except Exception:
        logger.exception("last_finance_activity: failed for user %s", getattr(user, "id", "?"))
        return None
    latest: date | None = None
    for arr_name in ("bankTransactions", "cardEntries"):
        for it in state.get(arr_name) or []:
            try:
                d = datetime.fromisoformat((it.get("date") or "").replace(" ", "T")[:10]).date()
            except ValueError:
                continue
            if latest is None or d > latest:
                latest = d
    return latest


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


def _fmt_date_br(iso: str) -> str:
    """'2026-05-11' -> '11/05'. Fallback ao texto cru."""
    try:
        return datetime.fromisoformat((iso or "").replace(" ", "T")[:10]).strftime("%d/%m")
    except ValueError:
        return iso or "?"


def confirm_banco(entry: dict) -> str:
    """Confirmação amigável de um lançamento no banco (sem id/ok:)."""
    amt = float(entry.get("amount") or 0)
    sign = "➕" if amt >= 0 else "➖"
    return (
        f"✅ Lançado no banco: {sign} {_fmt_brl(abs(amt))} — "
        f"{entry.get('desc', '?')} · {_fmt_date_br(entry.get('date', ''))} · "
        f"{entry.get('category', 'outros')}"
    )


def confirm_cartao(entry: dict, parcelas: int = 1) -> str:
    total = float(entry.get("amount") or 0)
    par = f" em {parcelas}x" if parcelas and parcelas > 1 else ""
    return (
        f"✅ Compra no cartão: {_fmt_brl(total)}{par} — "
        f"{entry.get('desc', '?')} · {_fmt_date_br(entry.get('date', ''))} · "
        f"{entry.get('category', 'outros')}"
    )


def confirm_tesouro(titulo: str, valor: float, data_iso: str, taxa=None) -> str:
    taxa_s = f" @ {taxa}%" if taxa is not None else ""
    return f"✅ Aporte: {_fmt_brl(valor)} — {titulo} · {_fmt_date_br(data_iso)}{taxa_s}"


def confirm_operacao_ativo(res: dict, op_type: str) -> str:
    """Mensagem de confirmação pra compra/venda de ativo (FII/ação/etc)."""
    op = res.get("operation") or {}
    klass_label = ASSET_CLASSES_LABEL.get(res.get("asset_class", ""), res.get("asset_class", "?"))
    verbo = "Compra" if op_type == "buy" else "Venda"
    qty = float(op.get("qty") or 0)
    price = float(op.get("price") or 0)
    total = qty * price
    novo = " (novo ativo)" if res.get("was_new") else ""
    qty_s = f"{qty:.4f}".rstrip("0").rstrip(".") if qty != int(qty) else str(int(qty))
    return (
        f"✅ {verbo}: {qty_s} × {res.get('asset_name', '?')} "
        f"@ {_fmt_brl(price)} = {_fmt_brl(total)} · "
        f"{klass_label} · {_fmt_date_br(op.get('date', ''))}{novo}"
    )


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
    internal: list[str] = []  # ids dos lançamentos do bot (uso interno do LLM)
    mods = {
        "banco", "cartao", "cartão", "tesouro",
        "investimentos", "investimento", "ativos",
        "tudo",
    }
    mod = (modulo or "tudo").strip().lower()
    if mod not in mods:
        raise FinanceiroError(f"módulo '{modulo}' inválido (use {sorted(mods)}).")

    if mod in ("banco", "tudo"):
        bank = _filter_by_days(state.get("bankTransactions") or [], dias, today_iso)
        if not bank:
            parts.append(f"🏦 Banco (últimos {dias}d): sem lançamentos")
        else:
            lines = [f"🏦 Banco (últimos {dias}d):"]
            saldo = 0.0
            for it in bank[-15:]:
                amt = float(it.get("amount") or 0)
                saldo += amt
                sign = "➕" if amt >= 0 else "➖"
                lines.append(
                    f"• {_fmt_date_br(it.get('date', ''))} — {it.get('desc', '?')} · "
                    f"{sign} {_fmt_brl(abs(amt))} · {it.get('category', '?')}"
                )
                if it.get("source") == "bot":
                    internal.append(f"banco · {it.get('desc', '?')} ({_fmt_date_br(it.get('date', ''))}) → #{it.get('id', '?')}")
            lines.append(f"Saldo do período: {'+' if saldo >= 0 else '−'}{_fmt_brl(abs(saldo))}")
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
            header = f"💳 Cartão (últimos {dias}d, por data de compra)"
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
            header = f"💳 Cartão — {range_label}"

        if not card_items:
            parts.append(f"{header}: sem compras")
        else:
            lines = [header + ":"]
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
                lines.append(
                    f"• {_fmt_date_br(it.get('date', ''))} — {it.get('desc', '?')}{par_label} · "
                    f"{_fmt_brl(value)} · {it.get('category', '?')}"
                )
                if it.get("source") == "bot":
                    internal.append(f"cartao · {it.get('desc', '?')} ({_fmt_date_br(it.get('date', ''))}) → #{it.get('id', '?')}")
            lines.append(f"Total da fatura: {_fmt_brl(total)} ({len(card_items)} itens)")
            parts.append("\n".join(lines))

    if mod in ("tesouro", "tudo", "investimentos", "investimento", "ativos"):
        holdings = state.get("treasuryHoldings") or []
        if not holdings:
            parts.append("🏛️ Tesouro: nenhum título cadastrado")
        else:
            lines = ["🏛️ Tesouro:"]
            for h in holdings:
                name = h.get("name", "?")
                contribs = h.get("contributions") or []
                recent = _filter_by_days(contribs, dias, today_iso)
                total_aportado = sum(float(c.get("amount") or 0) for c in contribs)
                recent_amount = sum(float(c.get("amount") or 0) for c in recent)
                lines.append(f"• {name} — total aportado {_fmt_brl(total_aportado)}")
                if recent:
                    lines.append(
                        f"   últimos {dias}d: +{_fmt_brl(recent_amount)} em {len(recent)} aporte(s):"
                    )
                    for c in recent[-10:]:
                        lines.append(
                            f"     {_fmt_date_br(c.get('date', ''))} +{_fmt_brl(float(c.get('amount') or 0))}"
                        )
                        if c.get("source") == "bot":
                            internal.append(f"tesouro · {name} ({_fmt_date_br(c.get('date', ''))}) → #{c.get('id', '?')}")
            parts.append("\n".join(lines))

    if mod in ("investimentos", "investimento", "ativos", "tudo"):
        invest = state.get("investments") or {}
        assets = invest.get("assets") or []
        if not assets:
            if mod != "tudo":
                parts.append("📊 Investimentos: nenhum ativo cadastrado")
        else:
            # Agrupa por classe (igual painel "Visão geral" do React).
            by_class: dict[str, list[dict]] = {}
            for a in assets:
                klass = a.get("class") or "outros"
                by_class.setdefault(klass, []).append(a)

            lines = ["📊 Investimentos:"]
            grand_position = 0.0
            grand_pnl = 0.0
            grand_div = 0.0
            for klass in ("acoes", "fiis", "etfs", "rf", "fundos", "cripto"):
                lst = by_class.get(klass) or []
                if not lst:
                    continue
                klass_label = ASSET_CLASSES_LABEL.get(klass, klass)
                metrics = [(a, _compute_asset_metrics(a)) for a in lst]
                # Só ativos com posição > 0 entram com detalhe.
                active = [(a, m) for (a, m) in metrics if m["qty"] > 0]
                if not active:
                    continue
                total = sum(m["position"] for _, m in active)
                invested = sum(m["invested"] for _, m in active)
                pnl = total - invested
                div_class = sum(m["divTotal"] for _, m in active)
                grand_position += total
                grand_pnl += pnl
                grand_div += div_class

                pnl_sign = "+" if pnl >= 0 else "−"
                lines.append(
                    f"\n🔹 {klass_label} — {_fmt_brl(total)} "
                    f"(P&L {pnl_sign}{_fmt_brl(abs(pnl))}):"
                )
                for a, m in sorted(active, key=lambda x: x[1]["position"], reverse=True):
                    qty_s = (
                        f"{m['qty']:.4f}".rstrip("0").rstrip(".")
                        if m["qty"] != int(m["qty"]) else str(int(m["qty"]))
                    )
                    lines.append(
                        f"  • {a.get('ticker', '?')} — {qty_s} × "
                        f"{_fmt_brl(m['currentPrice'])} = {_fmt_brl(m['position'])} "
                        f"(PM {_fmt_brl(m['pm'])})"
                    )
                    # ids internos das operações criadas pelo bot
                    for op in (a.get("operations") or []):
                        if op.get("source") == "bot":
                            internal.append(
                                f"investimento · {a.get('ticker', '?')} "
                                f"{op.get('type', '?')} {op.get('qty', 0)} "
                                f"({_fmt_date_br(op.get('date', ''))}) → #{op.get('id', '?')}"
                            )
            if grand_position > 0:
                pnl_sign = "+" if grand_pnl >= 0 else "−"
                lines.append(
                    f"\nTotal: {_fmt_brl(grand_position)} · "
                    f"P&L {pnl_sign}{_fmt_brl(abs(grand_pnl))} · "
                    f"Proventos {_fmt_brl(grand_div)}"
                )
            if len(lines) > 1:
                parts.append("\n".join(lines))

    if internal:
        parts.append(
            "[IDS_INTERNOS — NÃO mostre ao usuário; use SÓ para apagar_lancamento]\n"
            + "\n".join(internal)
        )
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


def _months_between(start: date, end: date):
    """Itera (ano, mês) de start até end inclusive."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1


def _fmt_brl(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


async def analisar_gastos(
    session: AsyncSession,
    user,
    inicio_iso: str,
    fim_iso: str,
    agrupar_por: str = "categoria",
    fonte: str = "tudo",
) -> str:
    """Analítico de gastos num intervalo, agrupado por categoria/mês/semana.

    Considera só SAÍDAS: débitos do banco (amount<0) e gastos do cartão
    (valor da parcela que cai em cada fatura do período). Crédito/receita
    não entra. Pensado pra perguntas tipo 'quanto gastei com alimentação
    em maio', 'maior categoria do trimestre', 'evolução mês a mês'.
    """
    uid = _require_uid(user)
    db = await _get_db(session)
    state = await _read_state(db, uid)

    try:
        inicio = date.fromisoformat(inicio_iso)
        fim = date.fromisoformat(fim_iso)
    except ValueError:
        raise FinanceiroError("datas inválidas (use 'YYYY-MM-DD').")
    if inicio > fim:
        inicio, fim = fim, inicio

    fonte = (fonte or "tudo").strip().lower()
    agrupar_por = (agrupar_por or "categoria").strip().lower()
    if agrupar_por not in ("categoria", "mes", "mês", "semana"):
        raise FinanceiroError("agrupar_por deve ser 'categoria', 'mes' ou 'semana'.")

    closing = _get_card_closing_day(state)
    cat_name = {c["id"]: (c.get("name") or c["id"]) for c in _effective_categories(state)}

    # events: lista de (date, category_id, value)
    events: list[tuple[date, str, float]] = []

    if fonte in ("banco", "tudo"):
        for it in state.get("bankTransactions") or []:
            try:
                d = date.fromisoformat((it.get("date") or "").replace(" ", "T")[:10])
            except ValueError:
                continue
            amt = float(it.get("amount") or 0)
            if amt < 0 and inicio <= d <= fim:
                events.append((d, it.get("category") or "outros", abs(amt)))

    if fonte in ("cartao", "cartão", "tudo"):
        entries = state.get("cardEntries") or []
        for y, m in _months_between(inicio, fim):
            for it in entries:
                info = _entry_in_bill(it, y, m, closing)
                if info:
                    events.append((date(y, m, 1), it.get("category") or "outros", float(info["value"])))

    if not events:
        return f"sem gastos entre {inicio.strftime('%d/%m/%Y')} e {fim.strftime('%d/%m/%Y')}."

    total_geral = sum(v for _, _, v in events)
    header = (
        f"gastos {inicio.strftime('%d/%m/%Y')} → {fim.strftime('%d/%m/%Y')} "
        f"(fonte: {fonte}) — total {_fmt_brl(total_geral)}"
    )
    lines = [header]

    if agrupar_por == "categoria":
        by: dict[str, float] = {}
        for _, cat, v in events:
            by[cat] = by.get(cat, 0.0) + v
        for cat, v in sorted(by.items(), key=lambda x: -x[1]):
            pct = (v / total_geral * 100) if total_geral else 0
            lines.append(f"  • {cat_name.get(cat, cat)}: {_fmt_brl(v)} ({pct:.0f}%)")
    elif agrupar_por in ("mes", "mês"):
        by_m: dict[str, float] = {}
        for d, _, v in events:
            by_m[f"{d.year:04d}-{d.month:02d}"] = by_m.get(f"{d.year:04d}-{d.month:02d}", 0.0) + v
        for key in sorted(by_m):
            lines.append(f"  • {key}: {_fmt_brl(by_m[key])}")
    else:  # semana
        by_w: dict[str, float] = {}
        for d, _, v in events:
            iso = d.isocalendar()
            by_w.setdefault(f"{iso[0]}-S{iso[1]:02d}", 0.0)
            by_w[f"{iso[0]}-S{iso[1]:02d}"] += v
        for key in sorted(by_w):
            lines.append(f"  • {key}: {_fmt_brl(by_w[key])}")

    return "\n".join(lines)
