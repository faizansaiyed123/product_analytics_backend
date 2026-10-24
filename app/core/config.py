"""Application settings, loaded from environment variables (see .env.example)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_DEFAULTS = {"change-me", "change-me-jwt-secret", "change-me-api-key-secret", ""}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    environment: str = "development"  # development | test | production
    database_url: str = "postgresql+asyncpg://analytics:analytics@localhost:5432/analytics"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "change-me-jwt-secret"
    api_key_secret: str = "change-me-api-key-secret"
    cors_origins: str = "http://localhost:5173,http://localhost:8080"
    log_level: str = "INFO"
    log_json: bool = False

    # Auth / cookies
    cookie_secure: bool = False
    session_ttl_days: int = 14
    allow_registration: bool = True
    public_app_url: str = "http://localhost:8080"
    email_verification_ttl_hours: int = 48
    password_reset_ttl_minutes: int = 60
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "analytics@localhost"

    # Ingestion limits
    max_body_bytes: int = 2_000_000
    max_batch_size: int = 500
    max_event_name_length: int = 200
    max_properties_per_event: int = 200
    max_property_value_bytes: int = 8_192
    default_rate_limit_per_second: int = 100
    auth_rate_limit_per_minute: int = 20
    trust_proxy_headers: bool = False
    country_header: str = "CF-IPCountry"

    # Analytics
    query_cache_ttl_seconds: int = 60
    max_query_range_days: int = 800
    query_statement_timeout_ms: int = 30_000

    # Database connection pool (per-process; divide by worker count for a target total)
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    db_pool_recycle: int = 3600

    # Uvicorn worker count (multi-process scaling). 1 = single process.
    uvicorn_workers: int = 1

    # Workers / exports
    export_dir: str = "/tmp/product-analytics-exports"
    export_ttl_hours: int = 72
    webhook_allow_private: bool = False
    metrics_token: str | None = None

    # Seed (development only)
    demo_email: str | None = None
    demo_password: str | None = None

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @field_validator("environment")
    @classmethod
    def _env(cls, v: str) -> str:
        return v.lower()

    def assert_production_safe(self) -> None:
        """Refuse to boot in production with placeholder secrets."""
        if not self.is_production:
            return
        problems = []
        for name in ("jwt_secret", "api_key_secret"):
            val = getattr(self, name)
            if val in _INSECURE_DEFAULTS or len(val) < 32:
                problems.append(f"{name.upper()} must be a random value of at least 32 characters")
        if not self.cookie_secure:
            problems.append("COOKIE_SECURE must be true in production (serve over HTTPS)")
        if problems:
            raise RuntimeError("Insecure production configuration: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
