"""Runtime configuration, read from environment variables (prefix ``TETHER_``) or ``.env``."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TETHER_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://tether:tether@localhost:5432/tether"
    embedder: str = "fake"
    embedding_dim: int = 384
    sentence_transformers_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    run_worker_in_api: bool = False
    deletion_step_delay: float = 0.0
    worker_poll_interval: float = 0.5
    openlineage_url: str | None = None
    openlineage_namespace: str = "tether-demo"
    cors_origins: str = "http://localhost:5173,http://localhost:8080"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
