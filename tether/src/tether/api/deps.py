"""FastAPI dependencies."""

from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy.orm import Session

from tether.db.session import session_scope
from tether.embeddings.base import Embedder, get_embedder


def get_session() -> Iterator[Session]:
    with session_scope() as session:
        yield session


@lru_cache
def get_embedder_dep() -> Embedder:
    return get_embedder()


@dataclass(frozen=True)
class Principal:
    """Extension point for authentication and multi-tenancy.

    The MVP has no auth and treats every caller as an anonymous operator. Replace
    :func:`get_principal` with real token validation and scope queries by ``tenant_id``.
    """

    id: str = "anonymous"
    tenant_id: str = "default"


def get_principal() -> Principal:
    return Principal()
