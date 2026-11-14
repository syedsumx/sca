"""Schema migrations: the migrated schema equals the models, legacy databases are adopted."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import MetaData, create_engine, inspect, text

from cairn import migrations
from cairn.common.db import load_models

URLS = ["sqlite://"]
if os.environ.get("CAIRN_TEST_PG_URL"):      # e.g. postgresql+psycopg://postgres:p@127.0.0.1:55432/cairn
    URLS.append(os.environ["CAIRN_TEST_PG_URL"])


def fresh(url: str):
    engine = create_engine(url)
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    return engine


@pytest.mark.parametrize("url", URLS)
def test_upgrade_from_empty_matches_models(url):
    engine = fresh(url)
    assert migrations.upgrade(engine) == migrations.head()
    assert migrations.check(engine) == []
    tables = set(inspect(engine).get_table_names())
    assert set(load_models().tables) <= tables


@pytest.mark.parametrize("url", URLS)
def test_upgrade_is_idempotent(url):
    engine = fresh(url)
    migrations.upgrade(engine)
    assert migrations.upgrade(engine) == migrations.head()


@pytest.mark.parametrize("url", URLS)
def test_legacy_create_all_database_is_adopted(url):
    baseline = fresh("sqlite://")
    migrations.upgrade(baseline, migrations.BASELINE)
    v01 = set(inspect(baseline).get_table_names()) - {"alembic_version"}
    engine = fresh(url)
    legacy = MetaData()                            # v0.1's schema exactly, as its create_all made it
    legacy.reflect(baseline, only=sorted(v01))
    legacy.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO cairn_bookmarks (actor, urn, created_at_ms) VALUES ('u', 'x', 1)"))
    assert migrations.upgrade(engine) == migrations.head()
    assert migrations.check(engine) == []
    with engine.connect() as conn:                 # existing data is kept
        assert conn.execute(text("SELECT count(*) FROM cairn_bookmarks")).scalar() == 1


def test_unmanaged_runtime_tables_are_ignored():
    engine = fresh("sqlite://")
    migrations.upgrade(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE cairn_embeddings (urn TEXT PRIMARY KEY)"))
    assert migrations.check(engine) == []


def test_every_table_is_in_a_migration():
    """A model change without a migration fails here (and in `cairn db check` on a live stack)."""
    engine = fresh("sqlite://")
    migrations.upgrade(engine)
    assert set(load_models().tables) - set(inspect(engine).get_table_names()) == set()


def test_every_table_module_is_registered():
    """Each module that declares tables on the shared metadata is imported by load_models()."""
    import pathlib
    import re

    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "cairn"
    loader = (src / "common" / "db.py").read_text()
    registered = set(re.findall(r"from cairn\.(\S+) import (\w+) as _", loader))
    registered = {f"{pkg}.{mod}" for pkg, mod in registered}
    declaring = {
        ".".join(p.relative_to(src).with_suffix("").parts)
        for p in src.rglob("*.py")
        if "migrations" not in p.parts and re.search(r"\bTable\(\s*\n?\s*\"", p.read_text())
    }
    assert declaring - registered == set()
