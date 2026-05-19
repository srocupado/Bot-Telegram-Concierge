from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator
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
    anthropic_model: str = Field("claude-opus-4-7", alias="ANTHROPIC_MODEL")
    openai_api_key: str | None = Field(None, alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-4.1", alias="OPENAI_MODEL")
    gemini_api_key: str | None = Field(None, alias="GEMINI_API_KEY")
    gemini_model: str = Field("gemini-2.5-pro", alias="GEMINI_MODEL")

    # Trânsito
    google_maps_api_key: str | None = Field(None, alias="GOOGLE_MAPS_API_KEY")
    home_coords: str | None = Field(None, alias="HOME_COORDS")
    work_coords: str | None = Field(None, alias="WORK_COORDS")

    # Clima
    weather_lat: float | None = Field(None, alias="WEATHER_LAT")
    weather_lon: float | None = Field(None, alias="WEATHER_LON")

    # Scheduler
    scheduler_tick_seconds: int = Field(60, alias="SCHEDULER_TICK_SECONDS")
    timezone: str = Field("America/Sao_Paulo", alias="TIMEZONE")
    traffic_daily_time: str = Field("07:00", alias="TRAFFIC_DAILY_TIME")
    mp_weekly_time: str = Field("08:00", alias="MP_WEEKLY_TIME")

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

    def parsed_coords(self, raw: str) -> tuple[float, float]:
        lat, lng = raw.split(",")
        return float(lat), float(lng)


settings = Settings()  # type: ignore[call-arg]
