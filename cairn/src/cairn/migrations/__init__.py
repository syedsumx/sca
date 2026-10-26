"""Schema migrations for the Cairn control-plane database (Alembic).

Every service calls :func:`upgrade` at start (through ``init_db``); a Postgres advisory lock makes
concurrent starts safe. Databases created before migrations existed (``create_all``) are recognised
and stamped at the baseline before upgrading.

    cairn db upgrade | current | check
    alembic -c src/cairn/migrations/alembic.ini revision --autogenerate -m "..."   (developers)
"""

from __future__ import annotations

import logging
import os
import warnings
from contextlib import contextmanager
from typing import Iterator

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

HERE = os.path.dirname(__file__)
# Every service migrates at start: keep Alembic's per-plugin INFO chatter out of the service logs.
logging.getLogger("alembic").setLevel(logging.WARNING)
BASELINE = "0001"
_LOCK_KEY = 0x0CA1A1DB  # advisory lock id for migrations

# Tables Cairn manages outside the migrations (created at runtime with a configurable shape).
UNMANAGED = {"cairn_embeddings"}


def config(connection: Connection | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", HERE)
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def head() -> str:
    return ScriptDirectory.from_config(config()).get_current_head()


def current(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


@contextmanager
def _lock(conn: Connection) -> Iterator[None]:
    if conn.dialect.name != "postgresql":
        yield
        return
    conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _LOCK_KEY})
    try:
        yield
    finally:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})


def upgrade(engine: Engine, target: str = "head") -> str:
    """Bring the schema to ``target``; returns the resulting revision."""
    with engine.connect() as conn:
        with _lock(conn):
            tables = set(inspect(conn).get_table_names())
            if "alembic_version" not in tables and tables - UNMANAGED:
                # Created by create_all before migrations existed: that schema is the baseline.
                command.stamp(config(conn), BASELINE)
            command.upgrade(config(conn), target)
            conn.commit()
            return MigrationContext.configure(conn).get_current_revision()


def check(engine: Engine) -> list:
    """Differences between the database and the models; empty when they match."""
    from alembic.autogenerate import compare_metadata

    from cairn.common.db import load_models

    metadata = load_models()
    with engine.connect() as conn, warnings.catch_warnings():
        # Unmanaged tables (e.g. pgvector columns) are reflected before include_object filters them.
        warnings.filterwarnings("ignore", message="Did not recognize type")
        ctx = MigrationContext.configure(conn, opts={"compare_type": True, "include_object": include_object})
        return compare_metadata(ctx, metadata)


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table" and (name in UNMANAGED or name == "alembic_version"):
        return False
    return True
