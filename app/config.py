"""Application settings, loaded only from environment variables (or a local .env file).

Secrets such as BOT_TOKEN are NEVER hardcoded. Set them in your hosting
provider's dashboard (Render -> Environment) or in a local, git-ignored .env file.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram ---
    bot_token: str = Field(default="", alias="BOT_TOKEN")
    # Public https URL of this service, e.g. https://web3-jobs.onrender.com
    public_base_url: str = Field(default="", alias="PUBLIC_BASE_URL")
    # Random string used to protect the webhook endpoint (Render can generate it).
    webhook_secret: str = Field(default="", alias="WEBHOOK_SECRET")
    # "webhook" (recommended on Render) or "polling" (handy on your own computer).
    bot_mode: str = Field(default="webhook", alias="BOT_MODE")

    # Comma-separated numeric Telegram user IDs allowed to use admin commands.
    admin_ids_raw: str = Field(default="", alias="ADMIN_IDS")
    # Newer name for the same thing. Both are read and merged (union), so old deploys keep working.
    admin_telegram_ids_raw: str = Field(default="", alias="ADMIN_TELEGRAM_IDS")
    # Bot @username without "@". Optional: read from Telegram (getMe) at startup when empty.
    bot_username_env: str = Field(default="", alias="BOT_USERNAME")
    # Mini App short name from BotFather (/newapp). Enables t.me/<bot>/<short>?startapp=... links.
    miniapp_short_name: str = Field(default="", alias="MINIAPP_SHORT_NAME")

    # --- Multi-user limits (admins are unlimited) ---
    user_daily_searches: int = Field(default=3, alias="USER_DAILY_SEARCHES")   # paid search runs/day
    user_daily_drafts: int = Field(default=20, alias="USER_DAILY_DRAFTS")      # message drafts/day
    # Max valid referrals one referrer can collect per hour (extra ones are recorded as invalid).
    referral_hourly_cap: int = Field(default=20, alias="REFERRAL_HOURLY_CAP")
    # Simple in-memory API rate limits (requests per minute).
    rate_limit_user_per_min: int = Field(default=120, alias="RATE_LIMIT_USER_PER_MIN")
    rate_limit_ip_per_min: int = Field(default=300, alias="RATE_LIMIT_IP_PER_MIN")
    # Seconds between last_active_at writes for the same user.
    activity_write_interval: int = Field(default=60, alias="ACTIVITY_WRITE_INTERVAL")
    # Channel the admin may publish to, e.g. @my_web3_jobs or -1001234567890
    channel_id: str = Field(default="", alias="CHANNEL_ID")

    # --- Personal Telegram account (MTProto user client) ---
    # From https://my.telegram.org -> API development tools. Optional here: the owner can
    # also enter them once in the Mini App (stored encrypted).
    tg_api_id: str = Field(default="", alias="TG_API_ID")
    tg_api_hash: str = Field(default="", alias="TG_API_HASH")
    # Key material used to encrypt the personal-account session in the database.
    # If empty, WEBHOOK_SECRET is used. Changing it forces a new login.
    session_enc_key_raw: str = Field(default="", alias="SESSION_ENC_KEY")

    # --- AI (any OpenAI-compatible API; optional, rules/templates are used without it) ---
    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_base_url: str = Field(default="https://generativelanguage.googleapis.com/v1beta/openai", alias="LLM_BASE_URL")
    llm_model: str = Field(default="gemini-2.5-flash", alias="LLM_MODEL")
    # ChatGPT (OpenAI) writes outreach messages when set; also used for scoring if LLM_API_KEY is empty.
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5-mini", alias="OPENAI_MODEL")
    # Grok (xAI) searches X with its built-in x_search tool.
    xai_api_key: str = Field(default="", alias="XAI_API_KEY")
    xai_model: str = Field(default="grok-4.7", alias="XAI_MODEL")

    # --- Discovery providers (all optional) ---
    serper_api_key: str = Field(default="", alias="SERPER_API_KEY")       # Google results via serper.dev
    brave_api_key: str = Field(default="", alias="BRAVE_API_KEY")         # Brave Search API
    x_bearer_token: str = Field(default="", alias="X_BEARER_TOKEN")       # official X API v2
    twitterapi_io_key: str = Field(default="", alias="TWITTERAPI_IO_KEY") # TwitterAPI.io (third party)

    # --- Data ---
    database_url: str = Field(default="sqlite+aiosqlite:///./data/jobs.db", alias="DATABASE_URL")
    fetch_interval_minutes: int = Field(default=60, alias="FETCH_INTERVAL_MINUTES")
    # Drop jobs not seen in any source for this many days.
    job_retention_days: int = Field(default=45, alias="JOB_RETENTION_DAYS")
    # Max Mini App initData age (seconds) before it is rejected.
    initdata_max_age: int = Field(default=86400, alias="INITDATA_MAX_AGE")
    # Turn off background fetching (used by tests).
    disable_scheduler: bool = Field(default=False, alias="DISABLE_SCHEDULER")

    @field_validator("database_url")
    @classmethod
    def _normalise_db_url(cls, v: str) -> str:
        # Render gives "postgres://..." or "postgresql://..."; SQLAlchemy async needs asyncpg.
        if v.startswith("postgres://"):
            v = "postgresql+asyncpg://" + v[len("postgres://"):]
        elif v.startswith("postgresql://"):
            v = "postgresql+asyncpg://" + v[len("postgresql://"):]
        return v

    @property
    def admin_ids(self) -> set[int]:
        out: set[int] = set()
        raw = f"{self.admin_ids_raw},{self.admin_telegram_ids_raw}"
        for part in raw.replace(" ", "").split(","):
            if part.lstrip("-").isdigit():
                out.add(int(part))
        return out

    @property
    def session_enc_key(self) -> str:
        return self.session_enc_key_raw or self.webhook_secret

    @property
    def webapp_url(self) -> str:
        return self.public_base_url.rstrip("/") + "/app/" if self.public_base_url else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
