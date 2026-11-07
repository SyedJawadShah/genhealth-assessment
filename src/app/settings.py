"""Runtime configuration, read from environment variables (and `.env` in development)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Order intake service"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    git_sha: str = "unknown"
    # Largest request body accepted, in bytes; larger requests get 413 before being read.
    max_request_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    # Swagger UI at /docs and the OpenAPI schema; reviewers use it to try the API.
    docs_enabled: bool = True
    # Source repository linked from the landing page.
    repo_url: str = "https://github.com/SyedJawadShah/genhealth-assessment"

    # Postgres in production (e.g. a Neon URL). Unset: SQLite locally, in-memory in tests, and
    # in production the database routes answer 503 rather than writing to an ephemeral disk.
    database_url: SecretStr | None = None
    # Keys the HMAC that stores bearer tokens. Required in production.
    secret_key: SecretStr | None = None
    token_ttl_hours: int = Field(default=12, gt=0, le=24 * 30)
    # Accounts created at startup, as a JSON object of username to password.
    demo_users: dict[str, SecretStr] = Field(default_factory=dict)

    anthropic_api_key: SecretStr | None = None
    extraction_model: str = "claude-opus-5-5"
    extraction_effort: Effort = "medium"
    extraction_timeout_seconds: float = Field(default=50.0, gt=0, le=600)
    extraction_max_retries: int = Field(default=1, ge=0, le=3)
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0)

    # Fixed-window limits, per process. IP-keyed limits trust X-Forwarded-For from the load
    # balancer; per-user and global limits do not depend on it.
    register_limit_per_hour: int = Field(default=10, gt=0)
    login_limit_per_minute: int = Field(default=10, gt=0)
    upload_limit_per_hour: int = Field(default=100, gt=0)
    upload_global_limit_per_hour: int = Field(default=300, gt=0)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def resolved_database_url(self) -> str | None:
        if self.database_url is not None:
            return self.database_url.get_secret_value()
        if self.app_env == "test":
            return "sqlite://"
        if self.app_env == "development":
            return "sqlite:///local.db"
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
