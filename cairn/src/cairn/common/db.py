"""Database plumbing shared by all Cairn modules (SQLAlchemy 2.0 Core).

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


def load_models() -> MetaData:
    """Import every table module so its tables register on :data:`metadata`."""
    from cairn.agent import store as _agent_store  # noqa: F401
    from cairn.ai import assistant as _assistant  # noqa: F401
    from cairn.ai import usage as _ai_usage  # noqa: F401
    from cairn.collab import service as _collab  # noqa: F401
    from cairn.common import audit as _audit  # noqa: F401
    from cairn.context import miner as _context  # noqa: F401
    from cairn.embed import service as _embed  # noqa: F401
    from cairn.governance import certification as _cert  # noqa: F401
    from cairn.governance import forms as _forms  # noqa: F401
    from cairn.governance import bulk as _bulk  # noqa: F401
    from cairn.governance import tag_sync as _tag_sync  # noqa: F401
    from cairn.quality import contracts as _contracts  # noqa: F401
    from cairn.governance import automations as _automations  # noqa: F401
    from cairn.reports import service as _reports  # noqa: F401
    from cairn.reports import costs as _costs  # noqa: F401
    from cairn.notify import ticketing as _ticketing  # noqa: F401
    from cairn.ai import steward as _steward  # noqa: F401
    from cairn.context import layer as _context_layer  # noqa: F401
    from cairn.common import licence as _licence  # noqa: F401
    from cairn.extensions import service as _extensions  # noqa: F401
    from cairn.governance import ai_systems as _ai_systems  # noqa: F401
    from cairn.marketplace import publish as _publish  # noqa: F401
    from cairn.governance import pdp as _pdp  # noqa: F401
    from cairn.governance import personas as _personas  # noqa: F401
    from cairn.identity import scim as _scim  # noqa: F401
    from cairn.marketplace import service as _marketplace  # noqa: F401
    from cairn.notify import hub as _notify  # noqa: F401
    from cairn.quality import store as _quality_store  # noqa: F401

    return metadata


def init_db(engine: Engine) -> None:
    """Migrate the schema to the current revision (cairn.migrations)."""
    from cairn import migrations

    migrations.upgrade(engine)


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
