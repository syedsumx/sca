"""Runtime configuration, sourced from ``DHE_*`` environment variables.

Pydantic models are used rather than pydantic-settings to keep the dependency set
small; :func:`load_settings` does the environment mapping explicitly so every knob
is greppable.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal, Optional

from pydantic import BaseModel, Field


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value is not None else default


class GmsSettings(BaseModel):
    url: str = "http://localhost:8080"
    token: Optional[str] = None
    frontend_url: Optional[str] = None
    timeout_seconds: float = 30.0


class AuthzSettings(BaseModel):
    mode: Literal["additive", "authoritative"] = "additive"
    policy_dir: Optional[str] = None
    import_native_policies: bool = True
    decision_cache_ttl_seconds: int = 30
    root_users: list[str] = Field(default_factory=lambda: ["urn:li:corpuser:datahub"])


class AiSettings(BaseModel):
    llm_provider: Literal["anthropic", "ollama", "none"] = "none"
    model: str = "claude-opus-5-5"
    model_docgen: Optional[str] = None
    model_nlq: Optional[str] = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    server_side_fallback: bool = True
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    embedding_provider: Literal["hashing", "ollama", "datahub"] = "hashing"
    embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 384
    audit_prompts: bool = False


class GatewaySettings(BaseModel):
    key_dir: str = "./.dhe/gateway-keys"
    public_url: str = "http://localhost:8820"
    max_clock_skew_seconds: int = 300
    task_lease_seconds: int = 300
    max_batch_bytes: int = 4 * 1024 * 1024


class Settings(BaseModel):
    database_url: str = "sqlite:///./dhe.db"
    service_token: Optional[str] = None
    kafka_bootstrap: str = "localhost:9092"
    schema_registry_url: str = "http://localhost:8081"
    gms: GmsSettings = Field(default_factory=GmsSettings)
    authz: AuthzSettings = Field(default_factory=AuthzSettings)
    ai: AiSettings = Field(default_factory=AiSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    # Development-only escape hatch: trust an X-DHE-Actor header without validating
    # a DataHub token. Never enable in production.
    dev_trust_actor_header: bool = False


def load_settings() -> Settings:
    root_users = _env("DHE_ROOT_USERS")
    return Settings(
        database_url=_env("DHE_DATABASE_URL", "sqlite:///./dhe.db"),
        service_token=_env("DHE_SERVICE_TOKEN"),
        kafka_bootstrap=_env("DHE_KAFKA_BOOTSTRAP", "localhost:9092"),
        schema_registry_url=_env("DHE_SCHEMA_REGISTRY_URL", "http://localhost:8081"),
        gms=GmsSettings(
            url=_env("DHE_GMS_URL", "http://localhost:8080"),
            token=_env("DHE_GMS_TOKEN"),
            frontend_url=_env("DHE_FRONTEND_URL"),
        ),
        authz=AuthzSettings(
            mode=_env("DHE_AUTHZ_MODE", "additive"),
            policy_dir=_env("DHE_POLICY_DIR"),
            import_native_policies=_env_bool("DHE_IMPORT_NATIVE_POLICIES", True),
            decision_cache_ttl_seconds=_env_int("DHE_DECISION_CACHE_TTL", 30),
            root_users=root_users.split(",") if root_users else ["urn:li:corpuser:datahub"],
        ),
        ai=AiSettings(
            llm_provider=_env("DHE_LLM_PROVIDER", "none"),
            model=_env("DHE_AI_MODEL", "claude-opus-5-5"),
            model_docgen=_env("DHE_AI_MODEL_DOCGEN"),
            model_nlq=_env("DHE_AI_MODEL_NLQ"),
            effort=_env("DHE_AI_EFFORT", "high"),
            server_side_fallback=_env_bool("DHE_AI_SERVER_SIDE_FALLBACK", True),
            ollama_url=_env("DHE_OLLAMA_URL", "http://localhost:11434"),
            ollama_model=_env("DHE_OLLAMA_MODEL", "llama3.1"),
            embedding_provider=_env("DHE_EMBEDDING_PROVIDER", "hashing"),
            embedding_model=_env("DHE_EMBEDDING_MODEL", "nomic-embed-text"),
            embedding_dim=_env_int("DHE_EMBEDDING_DIM", 384),
            audit_prompts=_env_bool("DHE_AI_AUDIT_PROMPTS", False),
        ),
        gateway=GatewaySettings(
            key_dir=_env("DHE_GATEWAY_KEY_DIR", "./.dhe/gateway-keys"),
            public_url=_env("DHE_GATEWAY_PUBLIC_URL", "http://localhost:8820"),
        ),
        dev_trust_actor_header=_env_bool("DHE_DEV_TRUST_ACTOR_HEADER", False),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
