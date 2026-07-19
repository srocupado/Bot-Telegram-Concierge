from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
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
    # Modelo Gemini escolhido pelo usuário (/provider gemini pro|flash). NULL =
    # usa GEMINI_MODEL do .env. Só vale quando provider == "gemini".
    gemini_model: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # Modelo de chat escolhido pelo usuário pra Anthropic/OpenAI
    # (/provider anthropic|openai <id>). NULL = usa ANTHROPIC_MODEL/OPENAI_MODEL
    # do .env. Só vale quando o provider efetivo for o respectivo.
    anthropic_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    openai_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Override opcional só pra entrada de imagens (foto). Quando NULL, segue
    # VISION_PROVIDER do .env (também opcional) e depois cai em `provider`.
    vision_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Provider de transcrição de voz (/voice gemini|openai). NULL = segue
    # VOICE_STT_PROVIDER do .env.
    voice_stt_provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Submodelo Gemini de STT (/voice gemini <variante>). NULL = segue
    # VOICE_STT_MODEL do .env. Só vale quando voice_stt_provider="gemini" ou
    # quando o provider efetivo for gemini (default).
    voice_stt_model: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # Modo tradutor: idioma-alvo (NULL = desligado) e motor de voz/tradução
    # ("gemini"|"openai"; NULL = TRANSLATOR_TTS_PROVIDER do .env).
    translator_lang: Mapped[str | None] = mapped_column(String(32), nullable=True)
    translator_tts_provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
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

    # Integração gerenciador-financeiro (Firestore)
    firebase_uid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    awaiting_firebase_json_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Monitor de MPs no Diário Oficial (Inlabs/DOU)
    dou_mp_subscribed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    # Override por usuário do motor da nota técnica (/dou_provider). NULL =
    # segue DOU_MP_PROVIDER/DOU_MP_GEMINI_MODEL do .env. dou_mp_model só vale
    # quando o provider efetivo for gemini.
    dou_mp_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dou_mp_model: Mapped[str | None] = mapped_column(String(48), nullable=True)

    # Agente proativo (opt-in): avisos automáticos (vencimentos, briefing, nudges, MP)
    proactive_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)

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


class ChatLog(Base):
    """Histórico persistente da conversa livre (write-through do contexto em
    RAM). Sobrevive a restart (re-hidrata o contexto dentro do TTL), alimenta
    o resumo rolante e a busca FTS5 (tool buscar_historico). Purge automático
    após MEMORY_RETENTION_DAYS (constante em services/memoria.py)."""

    __tablename__ = "chat_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True, nullable=False
    )


class ChatSummary(Base):
    """Resumo rolante (memória de longo prazo) por usuário. Atualizado em
    background quando turnos saem do contexto em RAM (overflow/TTL); entra
    no system prompt do chat livre. /reset_memoria zera."""

    __tablename__ = "chat_summaries"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
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


class KVSetting(Base):
    """Configurações globais singleton (chave/valor). Usado pra guardar
    o JSON do service account do Firebase, etc. Não é por-usuário porque
    a service account é credencial do projeto inteiro."""

    __tablename__ = "kv_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class ShoppingItem(Base):
    """Item da lista de compras (genérica — mercado, farmácia, ferragem,
    o que for). Persiste entre sessões. `checked=True` significa que o
    usuário marcou como comprado mas ainda não limpou da lista."""

    __tablename__ = "shopping_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    text: Mapped[str] = mapped_column(String(256), nullable=False)
    quantity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ActionLog(Base):
    """Log de ações mutativas do agente, pra suportar 'desfaz a última'.

    `kind` identifica como reverter ('tarefa', 'lembrete', 'compras',
    'financeiro'). `undo_data` é JSON com o necessário pra desfazer
    (ids, módulo, etc). `undone=True` marca já revertido — o undo pega
    sempre a ação não-revertida mais recente do usuário.
    """

    __tablename__ = "action_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(String(512), nullable=False)
    undo_data: Mapped[str] = mapped_column(Text, nullable=False)
    undone: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class DouSeenMP(Base):
    """Dedup do monitor de MPs: registra (usuário, número, ano) já
    notificados, pra não reenviar a mesma MP. Estado 100% local do bot —
    nada compartilhado com o Monitor-de-MP externo."""

    __tablename__ = "dou_seen_mps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    numero: Mapped[str] = mapped_column(String(32), nullable=False)
    ano: Mapped[int] = mapped_column(Integer, nullable=False)
    notified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class ProactiveNotice(Base):
    """Dedup do agente proativo: registra (usuário, kind, key) já avisados,
    pra não repetir aviso. Ex.: kind='venc_rem' key='18:2026-05-27',
    kind='nudge_workout' key='2026-05-26', kind='mp_briefing' key='1362/2026'."""

    __tablename__ = "proactive_notices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (Index("ix_proactive_user_kind_key", "user_id", "kind", "key"),)


class WorkoutLog(Base):
    """Registro de treino do dia. Categorias canônicas: peito, costas,
    pernas, cardio. `groups` é CSV (ex: 'peito,cardio'). Várias entradas
    por dia são permitidas (sessões separadas). Purge semanal apaga
    entradas anteriores ao domingo da semana corrente.
    """

    __tablename__ = "workout_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    groups: Mapped[str] = mapped_column(String(128), nullable=False)
    cardio_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class TravelWatch(Base):
    """Monitor diário de preço de passagem aérea ou hotel via SerpAPI.

    `kind` é 'flight' ou 'hotel'. `params` é JSON com os campos da busca:
      flight: origin_iata, destination_iata (ou destination_iatas: list),
              depart_date, return_date | window_start, window_end, nights,
              adults, travel_class.
      hotel:  location, check_in, check_out | window_start, window_end,
              nights, adults.
    Alerta dispara quando `last_price` < `min_price_seen` ou
    `<= max_price` (se setado).
    """

    __tablename__ = "travel_watches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    params: Mapped[dict] = mapped_column(JSON, nullable=False)
    max_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="BRL", server_default="BRL", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active", index=True, nullable=False)
    summary: Mapped[str] = mapped_column(String(256), default="", server_default="", nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    min_price_seen: Mapped[float | None] = mapped_column(Float, nullable=True)
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class TravelPriceSnapshot(Base):
    __tablename__ = "travel_price_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watch_id: Mapped[int] = mapped_column(Integer, ForeignKey("travel_watches.id"), index=True, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    raw: Mapped[dict] = mapped_column(JSON, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class TravelAlert(Base):
    __tablename__ = "travel_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watch_id: Mapped[int] = mapped_column(Integer, ForeignKey("travel_watches.id"), index=True, nullable=False)
    snapshot_id: Mapped[int] = mapped_column(Integer, ForeignKey("travel_price_snapshots.id"), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
