"""Runtime configuration. Every value can be overridden with a PLATFORM_* environment variable."""
from datetime import datetime
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLATFORM_", env_file=".env", extra="ignore")

    env: str = "dev"
    database_url: str = "sqlite+aiosqlite:///./data/platform.db"

    # Auth (dev: HS256 tokens minted locally; prod: RS256 validated against the IdP's JWKS)
    jwt_secret: str = "dev-only-jwt-secret-change-me-0123456789"
    jwt_issuer: str = "https://idp.local.example"
    jwt_audience: str = "entitlement-platform"
    enable_dev_endpoints: bool = True

    # Upstreams. None => call the in-process mock upstream app through an ASGI transport.
    upstream_base_url: str | None = None
    mock_data_dir: str = "mock_data"
    upstream_connect_timeout: float = 2.0
    upstream_read_timeout: float = 3.0
    upstream_max_attempts: int = 3
    upstream_backoff_initial: float = 0.2
    upstream_backoff_max: float = 2.0
    breaker_failure_threshold: int = 5
    breaker_reset_seconds: float = 30.0

    # "Business now" used for case age and renewal windows. Pinned for reproducible demos/evals.
    fixed_now: datetime | None = None

    # Rate limiting (token bucket per principal; agent traffic gets its own, smaller bucket)
    rate_limit_per_minute: int = 120
    agent_rate_limit_per_minute: int = 60

    # Webhooks
    webhook_signing_secret: str = "whsec_dev_only_change_me_0123456789"
    webhook_default_subscriber_url: str | None = "http://localhost:8000/v1/webhooks/receiver"
    # Events the default subscription receives. Env value is JSON, e.g. '["case.escalated","case.comment_added"]'.
    webhook_default_event_types: list[str] = ["case.escalated"]
    webhook_tolerance_seconds: int = 300
    webhook_max_attempts: int = 3
    webhook_backoff_initial: float = 0.5
    webhook_timeout_seconds: float = 5.0

    idempotency_ttl_hours: int = 24
    # Browser origins allowed to call the API directly (the React webhook dashboard). Comma-separated.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
