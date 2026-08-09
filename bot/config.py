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
    # As checagens rodam nas janelas do proativo (PROACTIVE_HOURS) — o antigo
    # DOU_MP_HOUR era config morta de antes do proativo e só aparecia numa
    # mensagem que mentia o horário pro dono.
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
    # Aniversário do dono (DD/MM). Mensagem calorosa 1x/ano, na hora do briefing,
    # INDEPENDENTE do proactive_enabled. Vazio = desligado.
    owner_birthday: str = Field("24/08", alias="OWNER_BIRTHDAY")
    proactive_hours: str = Field("7,13,19", alias="PROACTIVE_HOURS")
    # Minuto do disparo dentro de cada hora-janela (7h05, 13h05, 19h05…).
    # Fora da hora redonda de propósito: hipótese do dono (04/08/2026) de que
    # o Inlabs degrada nos minutos :00 — pico de bots/rotinas do mundo inteiro
    # batendo junto. Custa zero e tira o bot da multidão.
    proactive_minute: int = Field(5, alias="PROACTIVE_MINUTE")
    proactive_briefing_hour: int = Field(7, alias="PROACTIVE_BRIEFING_HOUR")
    # Alerta de chuva na próxima hora (pedido do dono, 04/08/2026): vigia a
    # cada 20min dentro do horário; 1 alerta por evento de chuva (histerese).
    rain_alert_enabled: bool = Field(True, alias="RAIN_ALERT_ENABLED")
    rain_alert_threshold_pct: int = Field(60, alias="RAIN_ALERT_THRESHOLD_PCT")
    rain_alert_start_hour: int = Field(6, alias="RAIN_ALERT_START_HOUR")
    rain_alert_end_hour: int = Field(23, alias="RAIN_ALERT_END_HOUR")
    # Fallback de detecção de MP pelo portal público www.in.gov.br quando o
    # Inlabs falha ("vaga-lume"). Detecção e aviso apenas — baixa e nota
    # continuam exigindo o Inlabs. Ver bot/services/dou_portal.py.
    dou_portal_fallback: bool = Field(True, alias="DOU_PORTAL_FALLBACK")
    # Resumo de fim de semana (dono, 09/08/2026): na última janela proativa de
    # sexta — clima de sáb/dom, lembretes do fds e filmes em cartaz no Cinemark.
    # Este é só o PADRÃO; a troca do dia a dia é pelo comando /fds_cinema
    # (dono, 09/08/2026: em viagem ele não alcança o .env do Pi).
    fds_cinema: str = Field("Iguatemi Brasília", alias="FDS_CINEMA")
    # Rotina noturna (dono, 09/08/2026): ~21h30 LOCAIS — gastos lançados hoje,
    # lembretes e previsão de amanhã, e pergunta se ficou gasto sem lançar.
    # 1x/dia; perdeu o horário (bot fora), sai quando voltar, até a meia-noite.
    night_summary_enabled: bool = Field(True, alias="NIGHT_SUMMARY_ENABLED")
    night_summary_hour: int = Field(21, alias="NIGHT_SUMMARY_HOUR")
    night_summary_minute: int = Field(30, alias="NIGHT_SUMMARY_MINUTE")
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

    # Modelo do RESUMIDOR da memória (compactação do que sai do contexto).
    # Vazio = segue o /provider do usuário, que era o comportamento único: com
    # Opus selecionado, cada compactação (a cada ~5 turnos e a cada TTL) rodava
    # no modelo caro pra uma tarefa mecânica. Formato "provider:modelo"
    # (ex.: "gemini:gemini-3.1-flash-lite", "openai:gpt-4o-mini").
    memory_summary_model: str = Field("", alias="MEMORY_SUMMARY_MODEL")

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

    # Modo tradutor (/tradutor + /tradutor_provider). O provider governa
    # entendimento E voz juntos: openai (Whisper+GPT+TTS, não treina) ou
    # gemini (multimodal+TTS, tier grátis pode treinar). Modelos/vozes são
    # overridáveis por .env caso um nome mude na API.
    translator_tts_provider: str = Field("openai", alias="TRANSLATOR_TTS_PROVIDER")
    translator_tts_openai_model: str = Field("gpt-4o-mini-tts", alias="TRANSLATOR_TTS_OPENAI_MODEL")
    translator_tts_openai_voice: str = Field("alloy", alias="TRANSLATOR_TTS_OPENAI_VOICE")
    translator_openai_chat_model: str = Field("gpt-4o-mini", alias="TRANSLATOR_OPENAI_CHAT_MODEL")
    translator_tts_gemini_model: str = Field("gemini-2.5-flash-preview-tts", alias="TRANSLATOR_TTS_GEMINI_MODEL")
    translator_tts_gemini_voice: str = Field("Kore", alias="TRANSLATOR_TTS_GEMINI_VOICE")
    translator_stt_gemini_model: str = Field("gemini-3.5-flash", alias="TRANSLATOR_STT_GEMINI_MODEL")

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
    # 0 = sem teto (era o DEFAULT, e um cron "a cada 2h" podia gastar
    # 12×AGENT_MAX_COST_USD por dia sem ninguém notar — o único default de
    # custo aberto do projeto). Agora vem com teto; pra soltar de novo é
    # explícito: AGENT_CRON_DAILY_BUDGET_USD=0 no .env.
    # Cada execução continua limitada por AGENT_MAX_COST_USD.
    # Contador em memória — zera no restart do bot (aceitável pro uso pessoal).
    agent_cron_daily_budget_usd: float = Field(5.0, alias="AGENT_CRON_DAILY_BUDGET_USD")

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
