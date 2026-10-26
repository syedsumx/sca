"""Alembic environment: runs on the connection handed over by cairn.migrations (or a URL)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from cairn.common.db import load_models
from cairn.migrations import include_object

config = context.config
target_metadata = load_models()


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
        render_as_batch=connection.dialect.name == "sqlite",   # SQLite cannot ALTER most things
    )


connection = config.attributes.get("connection")
if connection is not None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()
else:
    url = config.get_main_option("sqlalchemy.url") or __import__("os").environ["CAIRN_DATABASE_URL"]
    with create_engine(url).connect() as conn:
        _configure(conn)
        with context.begin_transaction():
            context.run_migrations()
        conn.commit()
