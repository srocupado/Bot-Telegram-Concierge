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
    # Override opcional só pra entrada de imagens (foto). Quando NULL, segue
    # VISION_PROVIDER do .env (também opcional) e depois cai em `provider`.
    vision_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="America/Sao_Paulo", nullable=False)

    # Trânsito (replicado do Telegram-Travels)
    traffic_subscribed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    last_traffic_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    traffic_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    traffic_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    traffic_alert_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1", nullable=False)
    last_traffic_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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


class UserFact(Base):
    """Fato persistente sobre o usuário, salvo/recuperado via tool do LLM.

    Chave única por (user_id, key) — recriar com mesma key sobrescreve.
    Pensado pra preferências e atributos longos (esposa: Dani, mora em Brasília,
    prefere Vue, alergia: amendoim), não pra histórico de conversa.
    """

    __tablename__ = "user_facts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[str] = mapped_column(String(2048), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class TrafficSample(Base):
    __tablename__ = "traffic_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)  # 0=Mon..6=Sun
    hour: Mapped[int] = mapped_column(Integer, nullable=False)     # 0..23
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    text: Mapped[str] = mapped_column(String(1024), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Quando setado, em vez de mandar `text` como notificação, o scheduler
    # executa uma ação. Valores válidos de command_kind:
    #   'transito_casa', 'transito_trabalho', 'congresso', 'clima', 'chat'.
    # command_args é livre — pro 'chat' é o prompt; pra 'clima' pode ser
    # 'lat,lng'; pra 'transito_*'/'congresso' fica vazio.
    command_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    command_args: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # Recorrência. Quando setado, após o envio o scheduler reagenda o próximo
    # disparo no mesmo HH:MM. Valores aceitos:
    #   'daily', 'weekday' (seg-sex), 'weekend' (sab-dom),
    #   'weekly:mon,wed,fri' (dias específicos),
    #   'monthly' (mesmo dia do mês).
    recurrence: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship(back_populates="reminders")
