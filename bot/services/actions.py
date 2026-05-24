"""Action log + undo universal.

Tools mutativas registram o que fizeram via `record_action`. O usuário
pode pedir 'desfaz a última ação' → `undo_last` pega a ação não-revertida
mais recente e a reverte usando os serviços de domínio existentes.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import ActionLog

logger = logging.getLogger(__name__)


async def record_action(
    session: AsyncSession,
    user_id: int,
    kind: str,
    summary: str,
    undo_data: dict,
) -> None:
    """Registra uma ação reversível. Não levanta — falha de log não deve
    quebrar a ação principal (que já foi efetivada)."""
    try:
        entry = ActionLog(
            user_id=user_id,
            kind=kind,
            summary=summary,
            undo_data=json.dumps(undo_data, ensure_ascii=False),
        )
        session.add(entry)
        await session.commit()
    except Exception:
        logger.exception("failed to record action (kind=%s)", kind)
        await session.rollback()


async def _reverse(session: AsyncSession, user, kind: str, data: dict) -> str:
    """Executa a reversão de uma ação. Retorna descrição do que foi
    desfeito. Levanta ValueError se não conseguir."""
    if kind == "tarefa":
        from bot.services.tasks import delete_task

        t = await delete_task(session, user.id, int(data["task_id"]))
        if t is None:
            raise ValueError("tarefa já não existe")
        return f"tarefa #{t.id} ({t.text})"

    if kind == "lembrete":
        from bot.services.reminders import delete_reminder

        r = await delete_reminder(session, user.id, int(data["reminder_id"]))
        if r is None:
            raise ValueError("lembrete já não existe (ou já foi enviado)")
        return f"lembrete #{r.id} ({r.text})"

    if kind == "compras":
        from bot.services.shopping import remove_item

        ids = data.get("item_ids") or []
        removed = []
        for i in ids:
            r = await remove_item(session, user.id, int(i))
            if r is not None:
                removed.append(r.text)
        if not removed:
            raise ValueError("itens já não estão na lista")
        return "itens de compra: " + ", ".join(removed)

    if kind == "financeiro":
        from bot.services.financeiro import FinanceiroError, apagar_lancamento

        try:
            res = await apagar_lancamento(
                session, user, data["modulo"], data["entry_id"],
            )
        except FinanceiroError as e:
            raise ValueError(str(e))
        rem = res.get("removido") or res.get("contribution") or {}
        desc = rem.get("desc") or f"aporte em {res.get('titulo', '?')}"
        return f"lançamento {data['entry_id']} ({res.get('modulo')}: {desc})"

    raise ValueError(f"tipo de ação desconhecido: {kind}")


async def undo_last(session: AsyncSession, user) -> str:
    """Reverte a ação não-revertida mais recente do usuário.
    Retorna mensagem 'ok: ...' ou 'erro/nada: ...'."""
    stmt = (
        select(ActionLog)
        .where(ActionLog.user_id == user.id, ActionLog.undone.is_(False))
        .order_by(ActionLog.id.desc())
        .limit(1)
    )
    entry = (await session.scalars(stmt)).first()
    if entry is None:
        return "nada: não há ação recente pra desfazer"

    try:
        data = json.loads(entry.undo_data)
    except json.JSONDecodeError:
        entry.undone = True
        await session.commit()
        return "erro: dados de undo corrompidos; ação descartada"

    try:
        desc = await _reverse(session, user, entry.kind, data)
    except ValueError as e:
        # Não dá pra reverter (já sumiu, etc). Marca como undone pra não
        # ficar travado e tentar a próxima numa nova chamada.
        entry.undone = True
        await session.commit()
        return f"nada: não consegui desfazer '{entry.summary}' — {e}"
    except Exception:
        logger.exception("undo failed for action %d", entry.id)
        await session.rollback()
        return "erro: falha ao desfazer; tente de novo"

    entry.undone = True
    await session.commit()
    return f"ok: desfeito — {desc}"
