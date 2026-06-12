"""Memória persistente de conversa — 3 camadas, zero config nova no .env.

1. **chat_log** (SQL): write-through do contexto em RAM. No startup,
   `hydrate()` recarrega o que está dentro do TTL — restart/deploy deixa
   de apagar a conversa.
2. **chat_summaries** (SQL): resumo rolante por usuário, atualizado em
   background quando turnos saem do contexto (overflow/TTL). Entra no
   system prompt via `get_summary()` — memória de longo prazo barata.
3. **busca** (`search_history`): FTS5 quando disponível, fallback LIKE.
   Exposta ao LLM pela tool `buscar_historico`.

Defaults fixos (de propósito — sem sobrecarregar o .env):
retenção de 90 dias, resumo ≤ ~1500 chars. O resumo é gerado pelo MESMO
provider/modelo que o usuário escolheu no /provider (nada hardcoded).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models import ChatLog, ChatSummary, User
from bot.services.chat_memory import ChatMemory, memory
from bot.services.llm.base import ChatMessage

logger = logging.getLogger(__name__)

RETENTION_DAYS = 90
SUMMARY_MAX_CHARS = 1500
_MSG_CAP = 400            # chars de cada mensagem no transcript da compactação
_TRANSCRIPT_CAP = 6000    # chars totais do transcript
_SEARCH_LIMIT = 8
_SEARCH_SNIPPET = 220

_sessionmaker: async_sessionmaker[AsyncSession] | None = None


# --- wiring ---------------------------------------------------------------


def attach(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Liga a persistência na memória RAM (chamar 1x no startup)."""
    global _sessionmaker
    _sessionmaker = sessionmaker
    memory.set_hooks(_schedule_persist, _schedule_compact)


def _spawn(coro) -> None:
    """create_task tolerante: fora de event loop (testes/sync), descarta."""
    try:
        asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()


def _schedule_persist(chat_id: int, role: str, content: str) -> None:
    # Em DM (único modo do bot) chat_id == user_id.
    if _sessionmaker is None or not isinstance(content, str) or not content.strip():
        return
    _spawn(_persist(chat_id, role, content))


def _schedule_compact(chat_id: int, msgs: list[ChatMessage]) -> None:
    if _sessionmaker is None:
        return
    texts = [m for m in msgs if isinstance(m.get("content"), str) and m["content"].strip()]
    if texts:
        _spawn(_compact(chat_id, texts))


# --- camada 1: write-through + hidratação ----------------------------------


async def _persist(user_id: int, role: str, content: str) -> None:
    assert _sessionmaker is not None
    try:
        async with _sessionmaker() as session:
            session.add(ChatLog(user_id=user_id, role=role, content=content))
            await session.commit()
    except Exception:
        logger.warning("memoria: persist falhou (segue só em RAM)", exc_info=True)


async def hydrate(
    sessionmaker: async_sessionmaker[AsyncSession], mem: ChatMemory = memory,
) -> int:
    """No startup: repõe na RAM as conversas ainda dentro do TTL."""
    cutoff = datetime.now(timezone.utc) - mem.ttl
    try:
        async with sessionmaker() as session:
            rows = list((await session.scalars(
                select(ChatLog).where(ChatLog.created_at >= cutoff).order_by(ChatLog.id)
            )).all())
    except Exception:
        logger.warning("memoria: hydrate falhou", exc_info=True)
        return 0
    by_user: dict[int, list[ChatLog]] = {}
    for r in rows:
        by_user.setdefault(r.user_id, []).append(r)
    for uid, items in by_user.items():
        msgs: list[ChatMessage] = [
            {"role": r.role, "content": r.content} for r in items
        ]
        last = items[-1].created_at
        if last.tzinfo is None:  # SQLite devolve naive
            last = last.replace(tzinfo=timezone.utc)
        mem.seed(uid, msgs, last_seen=last)
    if by_user:
        logger.info(
            "memoria: contexto re-hidratado pra %d usuário(s) (%d msgs)",
            len(by_user), len(rows),
        )
    return len(rows)


# --- camada 2: resumo rolante ----------------------------------------------


_SUMMARY_SYSTEM = (
    "Você mantém a memória de longo prazo de um assistente pessoal de "
    "Telegram. Sua única tarefa: atualizar o resumo da relação com o "
    "usuário. Registre APENAS o que tem valor duradouro: fatos sobre o "
    "usuário, decisões tomadas, planos/projetos em andamento, preferências, "
    "pendências. Descarte papo efêmero (cumprimentos, consultas pontuais de "
    "clima/trânsito já respondidas). NÃO invente nada que não esteja no "
    "texto. Formato: bullets curtos '- ...', agrupados por tema quando "
    f"fizer sentido. Máximo ~{SUMMARY_MAX_CHARS} caracteres. Responda SÓ "
    "com o resumo atualizado, sem preâmbulo."
)


def _transcript(msgs: list[ChatMessage]) -> str:
    lines: list[str] = []
    total = 0
    for m in msgs:
        who = "Usuário" if m["role"] == "user" else "Bot"
        body = " ".join(str(m["content"]).split())
        if len(body) > _MSG_CAP:
            body = body[:_MSG_CAP] + "…"
        line = f"{who}: {body}"
        total += len(line)
        if total > _TRANSCRIPT_CAP:
            break
        lines.append(line)
    return "\n".join(lines)


async def _compact(user_id: int, msgs: list[ChatMessage]) -> None:
    """Funde mensagens que saíram do contexto no resumo rolante do usuário.

    Usa o MESMO provider/modelo que o usuário escolheu no /provider — nada
    de modelo hardcoded. Se a chamada falhar, o resumo anterior fica."""
    from bot.services.llm.factory import get_provider

    assert _sessionmaker is not None
    try:
        async with _sessionmaker() as session:
            row = await session.get(ChatSummary, user_id)
            old = row.summary if row else "(vazio)"
            user = await session.get(User, user_id)
        if user is None:
            return
        provider = get_provider(user.provider, gemini_model=user.gemini_model)
        today = datetime.now(timezone.utc).strftime("%d/%m/%Y")
        prompt = (
            f"RESUMO ATUAL:\n{old}\n\n"
            f"NOVOS TRECHOS DA CONVERSA ({today}):\n{_transcript(msgs)}\n\n"
            "Atualize o resumo incorporando o que for duradouro dos novos "
            "trechos. Não duplique; preserve itens antigos ainda relevantes; "
            "remova o que ficou obsoleto."
        )
        new = await provider.chat(
            [{"role": "user", "content": prompt}],
            system=_SUMMARY_SYSTEM, max_tokens=700,
        )
        new = (new or "").strip()[: SUMMARY_MAX_CHARS + 200]
        if not new:
            return
        async with _sessionmaker() as session:
            row = await session.get(ChatSummary, user_id)
            if row is None:
                session.add(ChatSummary(user_id=user_id, summary=new))
            else:
                row.summary = new
            await session.commit()
        logger.info("memoria: resumo atualizado pra user %d (%d chars)", user_id, len(new))
    except Exception:
        logger.warning("memoria: compactação falhou (resumo mantido)", exc_info=True)


async def get_summary(session: AsyncSession, user_id: int) -> str | None:
    try:
        row = await session.get(ChatSummary, user_id)
    except Exception:
        logger.warning("memoria: get_summary falhou", exc_info=True)
        return None
    return row.summary if row and row.summary.strip() else None


# --- camada 3: busca no histórico -------------------------------------------


def _fts_query(termo: str) -> str | None:
    tokens = re.findall(r"\w+", termo, flags=re.UNICODE)
    if not tokens:
        return None
    return " ".join(f'"{t}"' for t in tokens[:8])


async def search_history(
    session: AsyncSession, user_id: int, termo: str, limit: int = _SEARCH_LIMIT,
) -> list[ChatLog]:
    """FTS5 (AND entre termos) com fallback LIKE. Mais recentes primeiro."""
    q = _fts_query(termo)
    if q is None:
        return []
    try:
        result = await session.execute(
            text(
                "SELECT m.id FROM chat_log m "
                "JOIN chat_log_fts f ON f.rowid = m.id "
                "WHERE chat_log_fts MATCH :q AND m.user_id = :uid "
                "ORDER BY m.id DESC LIMIT :lim"
            ),
            {"q": q, "uid": user_id, "lim": limit},
        )
        ids = [r[0] for r in result.fetchall()]
    except Exception:
        # FTS5 ausente/erro de sintaxe → LIKE com AND entre termos.
        tokens = re.findall(r"\w+", termo, flags=re.UNICODE)[:8]
        stmt = select(ChatLog.id).where(ChatLog.user_id == user_id)
        for t in tokens:
            stmt = stmt.where(ChatLog.content.ilike(f"%{t}%"))
        stmt = stmt.order_by(ChatLog.id.desc()).limit(limit)
        ids = list((await session.scalars(stmt)).all())
    if not ids:
        return []
    rows = list((await session.scalars(
        select(ChatLog).where(ChatLog.id.in_(ids)).order_by(ChatLog.id.desc())
    )).all())
    return rows


def format_search_results(rows: list[ChatLog], tz_name: str) -> str:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    lines = []
    for r in rows:
        dt = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
        when = dt.astimezone(tz).strftime("%d/%m/%Y %H:%M")
        who = "usuário" if r.role == "user" else "bot"
        body = " ".join(r.content.split())
        if len(body) > _SEARCH_SNIPPET:
            body = body[:_SEARCH_SNIPPET] + "…"
        lines.append(f"[{when}] {who}: {body}")
    return "\n".join(lines)


# --- manutenção -------------------------------------------------------------


async def purge_old_messages(session: AsyncSession) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    result = await session.execute(delete(ChatLog).where(ChatLog.created_at < cutoff))
    await session.commit()
    return result.rowcount or 0


async def reset_recent(session: AsyncSession, user_id: int) -> int:
    """/reset: apaga do SQL o que ainda re-hidrataria (dentro do TTL), pra
    conversa resetada não ressuscitar num restart."""
    cutoff = datetime.now(timezone.utc) - memory.ttl
    result = await session.execute(
        delete(ChatLog).where(ChatLog.user_id == user_id, ChatLog.created_at >= cutoff)
    )
    await session.commit()
    return result.rowcount or 0


async def clear_summary(session: AsyncSession, user_id: int) -> bool:
    row = await session.get(ChatSummary, user_id)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def clear_all_history(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(delete(ChatLog).where(ChatLog.user_id == user_id))
    row = await session.get(ChatSummary, user_id)
    if row is not None:
        await session.delete(row)
    await session.commit()
    return result.rowcount or 0
