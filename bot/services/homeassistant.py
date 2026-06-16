"""Integração com Home Assistant (tools ha_*: consultar/controlar a casa).

Owner-only. Usa a API REST do HA com token long-lived:
  - /api/conversation/process → manda frase natural pro Assist (resolve
    nome/área/apelido sozinho). Melhor pra controle SE o Assist estiver
    configurado; se não, retorna erro e o agente cai no REST.
  - /api/states            → lê estado das entidades (consulta + descoberta).
  - /api/services/<d>/<s>  → aciona serviços (ligar luz, trancar, clima).

Pensado pra rodar na MESMA LAN da HA (http://ip:8123). Token = controle total
da casa: nunca logar, nunca expor. Erros viram HAError (o handler traduz).
"""
from __future__ import annotations

import logging
import unicodedata

import httpx

from bot.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT_S = 15.0
_MAX_STATES = 40  # teto de entidades devolvidas (controla tokens)

# Domínios sensíveis: o handler marca pra o agente confirmar antes de acionar.
SENSITIVE_DOMAINS = {"lock", "alarm_control_panel", "cover", "garage_door"}


class HAError(Exception):
    pass


def available() -> bool:
    return bool(settings.homeassistant_url and settings.homeassistant_token)


def _base() -> str:
    return (settings.homeassistant_url or "").rstrip("/")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.homeassistant_token.get_secret_value()}",
        "Content-Type": "application/json",
    }


def _norm(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", (s or "").casefold())
        if not unicodedata.combining(c)
    )


async def _request(method: str, path: str, json: dict | None = None) -> object:
    if not available():
        raise HAError("HOMEASSISTANT_URL/TOKEN não configurados")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.request(method, f"{_base()}{path}", headers=_headers(), json=json)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise HAError(f"falha na requisição ao Home Assistant: {e}") from e
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    return resp.text


# ─────────────────────────── Assist (NL) ──────────────────────────────────

async def assist(texto: str) -> str:
    """Manda a frase pro agente de conversa do HA (Assist). Levanta HAError se
    o Assist não estiver configurado (o agente do bot cai no REST)."""
    data = await _request(
        "POST", "/api/conversation/process",
        json={"text": texto, "language": "pt-BR"},
    )
    if not isinstance(data, dict):
        raise HAError("resposta inesperada do Assist")
    resp = (data.get("response") or {})
    rtype = resp.get("response_type")
    speech = (((resp.get("speech") or {}).get("plain") or {}).get("speech") or "").strip()
    # HA responde "Desculpe, não entendi" quando não há intent/entidade exposta.
    if rtype == "error" or not speech:
        raise HAError(f"Assist não resolveu (resp_type={rtype}, speech={speech!r})")
    return speech


# ─────────────────────────── Consulta REST ────────────────────────────────

async def consultar_estados(busca: str | None = None) -> str:
    """Lê /api/states e filtra por nome/entity_id/domínio (substring). Sem
    filtro, devolve um resumo limitado. Serve pra consulta E pra o agente
    descobrir o entity_id antes de controlar."""
    data = await _request("GET", "/api/states")
    if not isinstance(data, list):
        raise HAError("resposta inesperada de /api/states")

    nf = _norm(busca) if busca else ""
    rows: list[str] = []
    for ent in data:
        if not isinstance(ent, dict):
            continue
        eid = ent.get("entity_id") or ""
        attrs = ent.get("attributes") or {}
        fname = attrs.get("friendly_name") or ""
        if nf and nf not in _norm(eid) and nf not in _norm(fname):
            continue
        state = ent.get("state")
        unit = attrs.get("unit_of_measurement")
        label = fname or eid
        line = f"• {label} ({eid}): {state}"
        if unit:
            line += f" {unit}"
        rows.append(line)
        if len(rows) >= _MAX_STATES:
            rows.append(f"… (mostrando os primeiros {_MAX_STATES})")
            break

    if not rows:
        alvo = f" pra '{busca}'" if busca else ""
        return f"(nenhuma entidade encontrada{alvo})"
    return "\n".join(rows)


# ─────────────────────────── Controle REST ────────────────────────────────

async def chamar_servico(
    domain: str, service: str, entity_id: str | None = None, dados: dict | None = None,
) -> str:
    """Aciona /api/services/<domain>/<service>. dados = parâmetros extras
    (ex: {'brightness_pct': 50}, {'temperature': 22})."""
    domain = (domain or "").strip()
    service = (service or "").strip()
    if not domain or not service:
        return "erro: precisa de domain e service (ex: light / turn_on)"
    body: dict = dict(dados or {})
    if entity_id:
        body["entity_id"] = entity_id

    data = await _request("POST", f"/api/services/{domain}/{service}", json=body)
    # HA devolve a lista de entidades que mudaram de estado.
    changed = data if isinstance(data, list) else []
    alvo = entity_id or "(sem entity_id)"
    if changed:
        # tenta mostrar o novo estado da entidade alvo
        for ent in changed:
            if isinstance(ent, dict) and ent.get("entity_id") == entity_id:
                return f"ok: {domain}.{service} em {alvo} → estado agora '{ent.get('state')}'"
        return f"ok: {domain}.{service} executado ({len(changed)} entidade(s) afetada(s))"
    return f"ok: {domain}.{service} enviado pra {alvo}"
