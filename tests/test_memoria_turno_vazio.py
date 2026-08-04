"""Turno assistant VAZIO não pode envenenar a memória do chat.

Bug da auditoria de 03/08/2026 (dois estágios):
1. `deliver_llm_reply` gravava `reply` mesmo vazio (Gemini pós-tool-call
   devolve "") → a Anthropic rejeita content vazio (400) e TODA mensagem
   seguinte falhava até o TTL/(/reset). O scheduled_actions tinha a guarda
   "(sem resposta)"; o caminho ao vivo (chat/voz/foto/PDF) não.
2. O persist descarta o assistant vazio mas gravava o par `user` — após
   restart, o hydrate devolvia dois `user` seguidos → 400 "roles must
   alternate" PERMANENTE (nem restart curava).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.db.models import Base, ChatLog, User
from bot.services import memoria
from bot.services.chat_memory import ChatMemory, memory


class _Msg:
    """Stub mínimo de aiogram.Message pro deliver_llm_reply."""

    def __init__(self, chat_id: int):
        self.chat = SimpleNamespace(id=chat_id)
        self.respostas: list[str] = []

    async def answer(self, text, **kw):
        self.respostas.append(text)


def _ctx():
    return SimpleNamespace(
        direct_html=None, fallback_text=None, financial_logged_ok=False,
        dou_mp_found=None, confirm_clear_shopping=False, request_location=False,
    )


def test_reply_vazio_vira_sem_resposta_na_memoria() -> None:
    from bot.handlers.chat import deliver_llm_reply

    chat_id = 987654321  # id único: memory é global
    msg = _Msg(chat_id)
    asyncio.run(deliver_llm_reply(msg, _ctx(), "", user_text="oi"))

    msgs = memory.get(chat_id)
    memory.reset(chat_id)
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant"]
    assert msgs[1]["content"].strip(), (
        "assistant vazio entrou na memória — envenena a Anthropic (400)"
    )
    assert msgs[1]["content"] == "(sem resposta)"


def test_hydrate_funde_turnos_orfaos_do_mesmo_role() -> None:
    """chat_log com par órfão (user, user, assistant) — legado do bug — tem
    que re-hidratar ALTERNADO, senão toda chamada pós-restart falha."""
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    sm = async_sessionmaker(engine, expire_on_commit=False)
    mem = ChatMemory()

    async def _main():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        agora = datetime.now(timezone.utc)
        async with sm() as s:
            s.add(User(id=1, chat_id=1, is_authorized=True))
            for role, content in (
                ("user", "primeira sem resposta"),   # par órfão do bug
                ("user", "segunda pergunta"),
                ("assistant", "resposta ok"),
            ):
                s.add(ChatLog(user_id=1, role=role, content=content,
                              created_at=agora))
            await s.commit()
        return await memoria.hydrate(sm, mem)

    asyncio.run(_main())
    msgs = mem.get(1)
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant"], f"roles não alternam: {roles}"
    assert "primeira sem resposta" in msgs[0]["content"]
    assert "segunda pergunta" in msgs[0]["content"], "conteúdo do órfão perdido"
