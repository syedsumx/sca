"""Database plumbing shared by all DHE modules (SQLAlchemy 2.0 Core).

Postgres is the production target; SQLite is supported for tests and the offline
demo. Tables are declared per module on the shared :data:`metadata` object.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import MetaData, create_engine, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.types import Text, TypeDecorator

metadata = MetaData()

_engines: dict[str, Engine] = {}
_lock = threading.Lock()


class JSONText(TypeDecorator):
    """JSON stored as text: portable between SQLite and Postgres."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else json.dumps(value, sort_keys=True, default=str)

    def process_result_value(self, value, dialect):
        return None if value is None else json.loads(value)


def get_engine(url: str) -> Engine:
    with _lock:
        engine = _engines.get(url)
        if engine is None:
            kwargs: dict = {"future": True, "pool_pre_ping": True}
            if url.startswith("sqlite"):
                kwargs["connect_args"] = {"check_same_thread": False}
                if url in ("sqlite://", "sqlite:///:memory:"):
                    from sqlalchemy.pool import StaticPool

                    kwargs["poolclass"] = StaticPool
            engine = create_engine(url, **kwargs)
            if url.startswith("sqlite"):

                @event.listens_for(engine, "connect")
                def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
                    cur = dbapi_conn.cursor()
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA foreign_keys=ON")
                    cur.close()

            _engines[url] = engine
        return engine


def init_db(engine: Engine) -> None:
    # Import table modules so they register on `metadata`.
    from datahub_enterprise.agent import store as _agent_store  # noqa: F401
    from datahub_enterprise.common import audit as _audit  # noqa: F401
    from datahub_enterprise.governance import certification as _cert  # noqa: F401
    from datahub_enterprise.quality import store as _quality_store  # noqa: F401

    metadata.create_all(engine)


@contextmanager
def transaction(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as conn:
        yield conn


@contextmanager
def advisory_lock(engine: Engine, key: int) -> Iterator[bool]:
    """Postgres session-level advisory lock; always 'acquired' on other dialects.

    Used for scheduler leader election. The connection is held for the duration of
    the ``with`` block so the lock lives exactly as long as the leader loop.
    """
    if engine.dialect.name != "postgresql":
        yield True
        return
    conn = engine.connect()
    try:
        acquired = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar())
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
    finally:
        conn.close()


def dialect_name(engine: Optional[Engine]) -> str:
    return engine.dialect.name if engine is not None else "none"
