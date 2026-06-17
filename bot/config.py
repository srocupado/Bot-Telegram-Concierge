from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


Provider = Literal["anthropic", "openai", "gemini"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    bot_token: str = Field(..., alias="BOT_TOKEN")
    access_password: str = Field(..., alias="ACCESS_PASSWORD")

    # Storage
    database_url: str = Field("sqlite+aiosqlite:////app/data/concierge.db", alias="DATABASE_URL")

    # LLM
    ai_provider: Provider = Field("anthropic", alias="AI_PROVIDER")
    anthropic_api_key: str | None = Field(None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field("claude-sonnet-4-6", alias="ANTHROPIC_MODEL")
    openai_api_key: str | None = Field(None, alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4.1", alias="OPENAI_MODEL")
    gemini_api_key: str | None = Field(None, alias="GEMINI_API_KEY")
    gemini_model: str = Field("gemini-2.5-pro", alias="GEMINI_MODEL")
    # Orçamento de "thinking" do agente Gemini (tokens). -1 = automático
    # (padrão, comportamento atual); 0 = desligado (só funciona em flash/
    # flash-lite — acelera muito); N>0 = orçamento fixo (ex.: 128 = pensa
    # pouco, válido também no pro). Use pra reduzir latência das respostas.
    gemini_thinking_budget: int = Field(-1, alias="GEMINI_THINKING_BUDGET")
    # Override opcional só pra entrada de imagens. Quando setado, fotos vão
    # pra esse provider independente do /provider do usuário. Vazio = usa o
    # provider atual do usuário.
    vision_provider: Provider | None = Field(None, alias="VISION_PROVIDER")

    # Trânsito (Google Directions API — replicado do Telegram-Travels)
    google_maps_api_key: SecretStr | None = Field(None, alias="GOOGLE_MAPS_API_KEY")
    home_coords: str | None = Field(None, alias="HOME_COORDS")
    work_coords: str | None = Field(None, alias="WORK_COORDS")
    route_google_maps_url: str | None = Field(None, alias="ROUTE_GOOGLE_MAPS_URL")
    traffic_digest_enabled: bool = Field(True, alias="TRAFFIC_DIGEST_ENABLED")
    traffic_hour: int = Field(7, alias="TRAFFIC_HOUR")
    traffic_minute: int = Field(20, alias="TRAFFIC_MINUTE")

    # Medidas Provisórias
    congress_digest_enabled: bool = Field(True, alias="CONGRESS_DIGEST_ENABLED")

    # Monitor de MPs no Diário Oficial (Inlabs/DOU). Independente do Monitor-de-MP
    # externo — credencial própria do cadastro gratuito em inlabs.in.gov.br.
    dou_mp_hour: int = Field(18, alias="DOU_MP_HOUR")
    # Pesquisa de contexto via web search (Passo 2 das diretrizes).
    # Desligue se a conta não tiver acesso ou se estiver lenta.
    dou_mp_web_research: bool = Field(True, alias="DOU_MP_WEB_RESEARCH")
    # Provider da nota técnica. "gemini" é bem mais barato (Flash + Google
    # Search grounding nativo); "anthropic" usa Claude + web_search.
    dou_mp_provider: Provider = Field("gemini", alias="DOU_MP_PROVIDER")
    dou_mp_gemini_model: str = Field("gemini-2.5-pro", alias="DOU_MP_GEMINI_MODEL")
    # Fallback automático quando o modelo principal estoura cota/429 (free
    # tier do Pro tem limite baixo). Vazio desliga o fallback.
    dou_mp_gemini_model_fallback: str = Field("gemini-2.5-flash", alias="DOU_MP_GEMINI_MODEL_FALLBACK")
    inlabs_email: str | None = Field(None, alias="INLABS_EMAIL")
    inlabs_password: SecretStr | None = Field(None, alias="INLABS_PASSWORD")

    # Cotação de ativos da B3 (brapi.dev). Token do cadastro gratuito em
    # brapi.dev. Sem token, a revisão de carteira do agente proativo é pulada.
    brapi_token: SecretStr | None = Field(None, alias="BRAPI_TOKEN")

    # Agente proativo (opt-in por usuário via /proativo_on). Gate global +
    # janelas de checagem (BRT, CSV) + limiares dos nudges e antecedência.
    proactive_enabled: bool = Field(True, alias="PROACTIVE_ENABLED")
    proactive_hours: str = Field("7,13,19", alias="PROACTIVE_HOURS")
    proactive_briefing_hour: int = Field(7, alias="PROACTIVE_BRIEFING_HOUR")
    proactive_lookahead_hours: int = Field(48, alias="PROACTIVE_LOOKAHEAD_HOURS")
    proactive_workout_idle_days: int = Field(4, alias="PROACTIVE_WORKOUT_IDLE_DAYS")
    proactive_finance_idle_days: int = Field(7, alias="PROACTIVE_FINANCE_IDLE_DAYS")
    proactive_shopping_idle_days: int = Field(5, alias="PROACTIVE_SHOPPING_IDLE_DAYS")
    proactive_nudge_cooldown_days: int = Field(3, alias="PROACTIVE_NUDGE_COOLDOWN_DAYS")
    proactive_use_llm: bool = Field(False, alias="PROACTIVE_USE_LLM")

    # Busca web com leitura de página (tool buscar_web + /buscar). Backend
    # PRIMÁRIO em WEBSEARCH_BACKEND; se falhar e WEBSEARCH_FALLBACK=true, tenta
    # o outro automaticamente. Backends (mesmo contrato search_and_read):
    #  - "searxng":  SearXNG self-hosted (metabusca) + Jina Reader (leitura);
    #                custo zero, exige SEARXNG_URL. (padrão = primário)
    #  - "firecrawl": turnkey (search + scrape num call); gasta créditos.
    # Um backend sem credencial é PULADO (não conta como falha) — então dá pra
    # rodar só com um dos dois configurado.
    websearch_backend: Literal["searxng", "firecrawl"] = Field(
        "searxng", alias="WEBSEARCH_BACKEND"
    )
    websearch_fallback: bool = Field(True, alias="WEBSEARCH_FALLBACK")
    # SearXNG: URL base da instância (ex: http://192.168.1.50:8080).
    searxng_url: str | None = Field(None, alias="SEARXNG_URL")
    # Jina Reader (https://r.jina.ai) lê cada link no backend searxng. Opcional:
    # sem a key funciona no tier gratuito; com ela o rate limit sobe.
    jina_api_key: SecretStr | None = Field(None, alias="JINA_API_KEY")
    # Firecrawl (search + scrape). Sem a key, este backend é pulado.
    firecrawl_api_key: SecretStr | None = Field(None, alias="FIRECRAWL_API_KEY")

    # Travels (busca de voos/hotéis via SerpAPI — porte do Telegram-Travels)
    serpapi_key: SecretStr | None = Field(None, alias="SERPAPI_KEY")
    travels_alert_hour: int = Field(8, alias="TRAVELS_ALERT_HOUR")

    # Scheduler
    scheduler_tick_seconds: int = Field(60, alias="SCHEDULER_TICK_SECONDS")
    timezone: str = Field("America/Sao_Paulo", alias="TIMEZONE")

    # Voz (STT via Gemini multimodal; reutiliza GEMINI_API_KEY)
    voice_enabled: bool = Field(True, alias="VOICE_ENABLED")
    voice_max_seconds: int = Field(120, alias="VOICE_MAX_SECONDS")
    voice_stt_model: str = Field("gemini-3.5-flash", alias="VOICE_STT_MODEL")
    # Provider de transcrição: "gemini" (multimodal, faz conversão p/ comando)
    # ou "openai" (Whisper/gpt-4o-transcribe, transcrição literal).
    voice_stt_provider: str = Field("gemini", alias="VOICE_STT_PROVIDER")
    voice_stt_openai_model: str = Field(
        "gpt-4o-mini-transcribe", alias="VOICE_STT_OPENAI_MODEL"
    )

    # Agente de execução (Claude Code headless via claude-agent-sdk).
    # Owner-only: só o usuário com este ID Telegram vê/usa o recurso.
    # Vazio = agente desabilitado. Os AGENT_* abaixo são defaults iniciais —
    # podem ser sobrepostos em runtime via /agente_config (sem restart).
    owner_telegram_id: int | None = Field(None, alias="OWNER_TELEGRAM_ID")
    agent_model: str = Field("claude-sonnet-4-6", alias="AGENT_MODEL")
    agent_timeout_seconds: int = Field(900, alias="AGENT_TIMEOUT_SECONDS")
    agent_max_turns: int = Field(40, alias="AGENT_MAX_TURNS")
    agent_max_cost_usd: float = Field(1.50, alias="AGENT_MAX_COST_USD")
    agent_session_ttl_minutes: int = Field(30, alias="AGENT_SESSION_TTL_MINUTES")
    agent_workspace: str = Field("/app/workspace", alias="AGENT_WORKSPACE")
    # Fine-grained PAT restrito aos repos que o agente pode tocar (opcional).
    # Entra no env do agente como GH_TOKEN — habilita push/PRs via git/gh.
    agent_github_token: SecretStr | None = Field(None, alias="AGENT_GITHUB_TOKEN")
    # Teto diário (US$) somado das execuções AGENDADAS do agente (cron).
    # 0 = sem teto. Cada execução continua limitada por AGENT_MAX_COST_USD.
    # Contador em memória — zera no restart do bot (aceitável pro uso pessoal).
    agent_cron_daily_budget_usd: float = Field(0.0, alias="AGENT_CRON_DAILY_BUDGET_USD")

    # Notificação ao reiniciar (mensagem '🟢 online' pra usuários autorizados).
    restart_notification_enabled: bool = Field(True, alias="RESTART_NOTIFICATION_ENABLED")

    # Logging
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    @field_validator("home_coords", "work_coords")
    @classmethod
    def _validate_coords(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        parts = v.split(",")
        if len(parts) != 2:
            raise ValueError("coords devem estar no formato 'lat,lng'")
        float(parts[0])
        float(parts[1])
        return v


settings = Settings()  # type: ignore[call-arg]
