"""Test fixtures.

The suite needs Postgres with pgvector. It uses ``TETHER_TEST_DATABASE_URL`` (default
``postgresql+psycopg://tether:tether@localhost:5432/tether_test``). The schema is rebuilt
once per session, and every test starts from empty tables.
"""

import os

TEST_DB_URL = os.environ.get(
    "TETHER_TEST_DATABASE_URL", "postgresql+psycopg://tether:tether@localhost:5432/tether_test"
)
# Must be set before tether modules read their settings.
os.environ["TETHER_DATABASE_URL"] = TEST_DB_URL
os.environ.setdefault("TETHER_EMBEDDER", "fake")
os.environ["TETHER_DELETION_STEP_DELAY"] = "0"
os.environ["TETHER_RUN_WORKER_IN_API"] = "false"

from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from sqlalchemy import Engine, text  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

import tether  # noqa: E402
from tether.cli import alembic_config  # noqa: E402
from tether.db.models import Base  # noqa: E402
from tether.db.session import get_engine, get_session_factory  # noqa: E402
from tether.embeddings.fake import FakeEmbedder  # noqa: E402
from tether.pipelines.run import PipelineResult, run_pipelines  # noqa: E402
from tether.pipelines.seed import seed  # noqa: E402


@pytest.fixture(scope="session")
def engine() -> Engine:
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(alembic_config(TEST_DB_URL), "head")
    return eng


@pytest.fixture(autouse=True)
def clean_db(engine: Engine) -> Iterator[None]:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only"))
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_append_only"))
    tether.configure()
    yield


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return get_session_factory()


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    s = session_factory()
    try:
        yield s
        s.commit()
    finally:
        s.close()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder(int(os.environ.get("TETHER_EMBEDDING_DIM", "384")))


@pytest.fixture
def demo_world(session: Session, embedder: FakeEmbedder) -> PipelineResult:
    """Seeded source data (30 customers, 120 tickets) plus every pipeline run once."""
    seed(session, n_customers=30, n_tickets=120)
    result = run_pipelines(session, embedder)
    session.commit()
    return result
