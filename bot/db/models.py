from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    # Internamente o id do user é o próprio telegram_id (também usado como chat_id em DMs).
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_authorized: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    provider: Mapped[str] = mapped_column(String(32), default="anthropic", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="America/Sao_Paulo", nullable=False)

    # Trânsito (replicado do Telegram-Travels)
    traffic_subscribed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    last_traffic_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    traffic_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    traffic_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Medidas Provisórias (replicado do Telegram-Travels)
    congress_subscribed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    last_congress_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    congress_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    congress_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    tasks: Mapped[list["Task"]] = relationship(back_populates="user", cascade="all,delete-orphan")
    reminders: Mapped[list["Reminder"]] = relationship(back_populates="user", cascade="all,delete-orphan")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    text: Mapped[str] = mapped_column(String(1024), nullable=False)
    done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="tasks")


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    text: Mapped[str] = mapped_column(String(1024), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="reminders")
