"""Configuração em runtime do agente de execução (/agente_config).

Overrides persistidos em JSON (sobrevivem a restart); o .env é só o default
inicial — quem nunca mexer no comando continua usando os valores do .env.
OWNER_TELEGRAM_ID, AGENT_GITHUB_TOKEN e AGENT_WORKSPACE ficam de fora de
propósito (sensíveis demais pra trocar por chat — exigem .env + restart).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from bot.config import settings

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("/app/data/agent_config.json")

# Aliases de modelo aceitos no /agente_config modelo <alias>.
MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
    "haiku": "claude-haiku-4-5-20251001",
}

# chave → (campo do settings, tipo, mín, máx)
_TUNABLES = {
    "modelo": ("agent_model", str, None, None),
    "timeout": ("agent_timeout_seconds", int, 60, 7200),
    "turnos": ("agent_max_turns", int, 5, 200),
    "custo": ("agent_max_cost_usd", float, 0.10, 50.0),
    "ttl": ("agent_session_ttl_minutes", int, 1, 720),
}


class AgentConfig:
    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self._path = path
        self._overrides: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                self._overrides = json.loads(self._path.read_text("utf-8"))
        except Exception:
            logger.exception("agent_config: falha ao ler %s (usando defaults)", self._path)
            self._overrides = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._overrides, ensure_ascii=False, indent=2), "utf-8"
            )
        except Exception:
            logger.exception("agent_config: falha ao salvar %s", self._path)

    def _get(self, key: str):
        field = _TUNABLES[key][0]
        return self._overrides.get(key, getattr(settings, field))

    @property
    def model(self) -> str:
        return self._get("modelo")

    @property
    def timeout_seconds(self) -> int:
        return int(self._get("timeout"))

    @property
    def max_turns(self) -> int:
        return int(self._get("turnos"))

    @property
    def max_cost_usd(self) -> float:
        return float(self._get("custo"))

    @property
    def session_ttl_minutes(self) -> int:
        return int(self._get("ttl"))

    def set(self, key: str, raw: str) -> str:
        """Aplica um override. Retorna mensagem de confirmação; ValueError se inválido."""
        key = key.lower()
        if key not in _TUNABLES:
            opts = ", ".join(_TUNABLES)
            raise ValueError(f"opção inválida. Use: {opts}")
        _field, typ, lo, hi = _TUNABLES[key]
        if key == "modelo":
            value = MODEL_ALIASES.get(raw.lower(), raw)
            if not value.startswith("claude-"):
                aliases = " | ".join(MODEL_ALIASES)
                raise ValueError(f"modelo inválido. Use um alias ({aliases}) ou um id claude-*")
        else:
            try:
                value = typ(raw.replace(",", "."))
            except (TypeError, ValueError):
                raise ValueError(f"'{raw}' não é um número válido pra '{key}'")
            if (lo is not None and value < lo) or (hi is not None and value > hi):
                raise ValueError(f"'{key}' deve estar entre {lo} e {hi}")
        self._overrides[key] = value
        self._save()
        return f"{key} = {value}"

    def reset(self) -> None:
        """Limpa todos os overrides (volta aos valores do .env)."""
        self._overrides = {}
        self._save()

    def summary_html(self) -> str:
        def mark(key: str) -> str:
            return " <i>(override)</i>" if key in self._overrides else ""

        return (
            "⚙️ <b>Config do agente</b>\n"
            f"• modelo: <code>{self.model}</code>{mark('modelo')}\n"
            f"• timeout: <code>{self.timeout_seconds}s</code>{mark('timeout')}\n"
            f"• turnos: <code>{self.max_turns}</code>{mark('turnos')}\n"
            f"• custo máx/tarefa: <code>US$ {self.max_cost_usd:.2f}</code>{mark('custo')}\n"
            f"• ttl da sessão: <code>{self.session_ttl_minutes} min</code>{mark('ttl')}\n\n"
            "Mudar: <code>/agente_config modelo opus|sonnet|haiku</code> · "
            "<code>/agente_config timeout 1800</code> · "
            "<code>/agente_config custo 5</code> · "
            "<code>/agente_config turnos 20</code> · "
            "<code>/agente_config ttl 60</code>\n"
            "Voltar ao .env: <code>/agente_config padrao</code>"
        )


agent_config = AgentConfig()
