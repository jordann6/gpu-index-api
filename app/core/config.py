from functools import lru_cache

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Every value is overridable by environment."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "gpu-index-api"
    environment: str = "local"
    log_level: str = "INFO"

    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://gpu:gpu@localhost:5432/gpuindex"
    )
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    redis_url: RedisDsn = Field(default="redis://localhost:6379/0")
    cache_ttl_seconds: int = 60

    # Token bucket, applied per API key.
    rate_limit_requests: int = 120
    rate_limit_window_seconds: int = 60

    api_keys: str = "local-dev-key"

    # Bounds the ingestion fan-out so a wide provider list cannot exhaust
    # connections or sockets.
    ingest_concurrency: int = 16

    default_page_size: int = 50
    max_page_size: int = 500

    @property
    def api_key_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
