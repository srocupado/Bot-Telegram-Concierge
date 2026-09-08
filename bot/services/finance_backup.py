"""Backup e restore do gerenciador-financeiro (Firestore) pelo bot.

Roda EM PARALELO com o workflow `nightly-backup` do repo
srocupado/gerenciador-financeiro, de propósito — os dois falham por motivos
diferentes:

  - o Actions morre por repo público sem commit há 60 dias, por cota, ou por
    token/secret revogado;
  - o backup local morre por disco cheio, container parado ou Pi desligado.

Uma cópia só era ponto único de falha. Duas, em horários diferentes (aqui
04:00 BRT; o Actions cai entre 07h e 10h BRT por causa da fila de agendados),
também dão duas fotos por dia em vez de uma.

O formato do arquivo é DELIBERADAMENTE o mesmo do `backup.mjs`:

    {"exportedAt": ..., "project": ..., "count": N, "users": {"<uid>": {...}}}

Assim um artifact baixado do GitHub restaura por aqui, e um arquivo daqui
serve pro "Importar JSON" do app. Ver `extract_state`, que aceita as duas
formas (envelope e state cru) porque o import do app não valida nada e
engolir o arquivo errado zera a tela sem erro nenhum.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.services.financeiro import (
    FinanceiroError,
    NotConfiguredError,
    _get_db,
    _require_uid,
    _run_blocking,
)

logger = logging.getLogger(__name__)

# Seções de topo do state que a UI do gerenciador entende. Serve só pro
# resumo mostrado antes do restore — NÃO filtra a escrita (state desconhecido
# é gravado inteiro; recortar seria perder dado de versão nova do app).
_SECOES = (
    "bankTransactions",
    "cardEntries",
    "treasuryHoldings",
    "investments",
    "customCategories",
    "settings",
)

# Três origens, um só formato de nome — assim o restore tem UM caminho de
# código: o diário, a foto tirada antes de sobrescrever, e o JSON que o dono
# manda no chat (ex.: artifact baixado do GitHub).
_NOME_RE = re.compile(
    r"^financeiro-[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"(-(pre-restore|upload)-[0-9]{6})?\.json$"
)


class BackupError(FinanceiroError):
    """Falha ao gerar, ler ou restaurar um backup."""


def backup_dir() -> Path:
    d = Path(settings.finance_backup_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _serialize(value: Any) -> Any:
    """Timestamp do Firestore / datetime → ISO 8601, recursivo.

    Espelha o `serializeValue` do backup.mjs: sem isso o json.dumps estoura
    em DatetimeWithNanoseconds e o backup do dia inteiro se perde."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    # Firestore devolve tipos próprios (DatetimeWithNanoseconds, GeoPoint,
    # DocumentReference). isoformat cobre o primeiro; o resto vira str em vez
    # de derrubar o backup.
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return iso()
    return str(value)


def _export_blocking(db) -> dict:
    """Lê a coleção `users` inteira. Bloqueante; chamar via _run_blocking."""
    users: dict[str, Any] = {}
    for doc in db.collection("users").stream():
        users[doc.id] = _serialize(doc.to_dict())
    return users


async def export_payload(session: AsyncSession) -> dict:
    """Snapshot da coleção `users` no formato do backup.mjs.

    Levanta NotConfiguredError se a service account não estiver configurada —
    o chamador PRECISA distinguir isso de 'exportou vazio'."""
    db = await _get_db(session)
    users = await _run_blocking(_export_blocking, db)
    project = getattr(db, "project", None) or getattr(db, "_database_string_internal", None)
    return {
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "project": project if isinstance(project, str) else None,
        "count": len(users),
        "users": users,
        "generatedBy": "bot-telegram-concierge",
    }


def _nome_do_dia(hoje: date, *, sufixo: str = "") -> str:
    return f"financeiro-{hoje.isoformat()}{sufixo}.json"


def write_backup(payload: dict, *, hoje: date | None = None, sufixo: str = "") -> Path:
    """Grava o payload em data/backups/financeiro/. Escrita ATÔMICA (tmp +
    rename): um restart no meio não deixa JSON truncado que pareceria um
    backup válido até a hora de restaurar."""
    d = backup_dir()
    alvo = d / _nome_do_dia(hoje or datetime.now(timezone.utc).date(), sufixo=sufixo)
    tmp = alvo.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(alvo)
    return alvo


@dataclass
class BackupFile:
    path: Path
    exported_at: str | None
    count: int
    size: int
    pre_restore: bool

    @property
    def nome(self) -> str:
        return self.path.name


def _peek(path: Path) -> BackupFile:
    exported_at, count = None, 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        exported_at = data.get("exportedAt")
        count = int(data.get("count") or len(data.get("users") or {}))
    except Exception:
        logger.warning("backup ilegível: %s", path.name)
    return BackupFile(
        path=path,
        exported_at=exported_at if isinstance(exported_at, str) else None,
        count=count,
        size=path.stat().st_size,
        pre_restore="pre-restore" in path.name,
    )


def list_backups() -> list[BackupFile]:
    """Backups locais, do mais novo pro mais velho."""
    d = backup_dir()
    arquivos = [p for p in d.glob("financeiro-*.json") if _NOME_RE.match(p.name)]
    arquivos.sort(key=lambda p: p.name, reverse=True)
    return [_peek(p) for p in arquivos]


def resolve_backup(nome: str) -> Path | None:
    """Nome → path, confinado ao backup_dir. Rejeita travessia (../) e
    qualquer nome fora do padrão."""
    limpo = Path(nome.replace("\\", "/")).name.strip()
    if not _NOME_RE.match(limpo):
        return None
    alvo = (backup_dir() / limpo).resolve()
    if alvo.parent != backup_dir().resolve() or not alvo.is_file():
        return None
    return alvo


def purge_old(dias: int | None = None, *, hoje: date | None = None) -> int:
    """Apaga backups mais velhos que `dias`. Devolve quantos saíram.

    Os `-pre-restore-` NÃO são poupados: eles também envelhecem, e manter
    para sempre encheria o cartão do Pi em silêncio."""
    limite = dias if dias is not None else settings.finance_backup_retention_days
    if limite <= 0:
        return 0
    hoje = hoje or datetime.now(timezone.utc).date()
    n = 0
    for b in list_backups():
        m = re.search(r"(\d{4}-\d{2}-\d{2})", b.nome)
        if not m:
            continue
        try:
            d = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if (hoje - d).days > limite:
            b.path.unlink(missing_ok=True)
            n += 1
    return n


def extract_state(payload: Any, uid: str) -> dict:
    """Devolve o `state` a restaurar, aceitando as três formas que chegam:

      1. envelope do backup (bot ou backup.mjs): {"users": {"<uid>": {"state": …}}}
      2. doc de um usuário só: {"state": …}
      3. state cru — o que o botão "Exportar JSON" do app gera.

    Aceitar as três é o que evita o modo de falha do import do app, que faz
    `setState(JSON.parse(arquivo))` sem validar: entregar o envelope inteiro
    lá dá "Dados importados com sucesso" e tela zerada.

    Levanta BackupError quando não dá pra decidir — na dúvida NÃO restaura."""
    if not isinstance(payload, dict):
        raise BackupError("arquivo não é um objeto JSON.")

    users = payload.get("users")
    if isinstance(users, dict):
        if uid in users:
            doc = users[uid]
        elif len(users) == 1:
            (only_uid, doc), = users.items()
            logger.info("restore: backup tem 1 uid (%s) ≠ configurado (%s)", only_uid, uid)
        else:
            raise BackupError(
                f"o backup tem {len(users)} usuários e nenhum é o seu UID "
                f"({uid}). Não dá pra escolher por você."
            )
        if not isinstance(doc, dict):
            raise BackupError("entrada do usuário no backup não é um objeto.")
        state = doc.get("state")
        if not isinstance(state, dict):
            raise BackupError("o usuário no backup não tem `state`.")
        return state

    state = payload.get("state")
    if isinstance(state, dict):
        return state

    # State cru: exige pelo menos uma seção conhecida, senão qualquer JSON
    # aleatório passaria e sobrescreveria o financeiro com lixo.
    if any(k in payload for k in _SECOES):
        return payload

    raise BackupError(
        "não reconheci o formato. Esperado o JSON do backup (com `users`) ou "
        "o \"Exportar JSON\" do app (com bankTransactions/cardEntries/…)."
    )


def resumo_state(state: dict) -> str:
    """Contagem por seção, pro usuário conferir ANTES de confirmar o restore.
    Sem isso a confirmação seria às cegas."""
    partes = []
    for chave, rotulo in (
        ("bankTransactions", "banco"),
        ("cardEntries", "cartão"),
        ("treasuryHoldings", "tesouro"),
        ("customCategories", "categorias"),
    ):
        v = state.get(chave)
        if isinstance(v, list):
            partes.append(f"{len(v)} {rotulo}")
    ativos = (state.get("investments") or {}).get("assets")
    if isinstance(ativos, list):
        partes.append(f"{len(ativos)} ativos")
    return " · ".join(partes) if partes else "nenhuma seção reconhecida"


def _restore_blocking(db, uid: str, state: dict) -> None:
    """Sobrescreve `state` INTEIRO. Bloqueante; chamar via _run_blocking.

    `update` (e não `set(merge=True)`) de propósito: o merge do Firestore é
    PROFUNDO em mapas, então uma seção que existe hoje e não existe no backup
    sobreviveria ao restore — você pediria a foto de ontem e receberia uma
    mistura das duas. `update` troca o campo `state` inteiro.

    Fallback pra `set` quando o doc não existe (update exige documento)."""
    from firebase_admin import firestore as _fs

    ref = db.collection("users").document(uid)
    payload = {"state": state, "updatedAt": _fs.SERVER_TIMESTAMP}
    if ref.get().exists:
        ref.update(payload)
    else:
        ref.set(payload)


async def restore_state(session: AsyncSession, user, state: dict) -> None:
    uid = _require_uid(user)
    db = await _get_db(session)
    await _run_blocking(_restore_blocking, db, uid, state)
    logger.info("financeiro restaurado para uid=%s (%s)", uid, resumo_state(state))


async def run_backup(session: AsyncSession, *, hoje: date | None = None) -> tuple[Path, dict]:
    """Exporta, grava e poda. Devolve (arquivo, payload).

    Exceção sobe pro chamador de propósito: quem chama decide se avisa no
    Telegram (scheduler) ou responde no chat (comando). Engolir aqui viraria
    'backup em silêncio que nunca rodou'."""
    payload = await export_payload(session)
    alvo = write_backup(payload, hoje=hoje)
    removidos = purge_old(hoje=hoje)
    if removidos:
        logger.info("backup financeiro: %d arquivo(s) antigo(s) removido(s)", removidos)
    return alvo, payload


async def snapshot_pre_restore(session: AsyncSession) -> Path | None:
    """Foto do estado ATUAL antes de sobrescrever. Best-effort: se falhar, o
    chamador decide — mas o restore não deve prosseguir às cegas, então a
    falha é devolvida como None pra virar aviso explícito."""
    try:
        payload = await export_payload(session)
    except Exception:
        logger.exception("snapshot pré-restore falhou")
        return None
    agora = datetime.now(timezone.utc)
    return write_backup(
        payload, hoje=agora.date(), sufixo=f"-pre-restore-{agora.strftime('%H%M%S')}"
    )
