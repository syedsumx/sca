"""Materialized views (cairn.pipelines.matviews) without Trino: what CREATE and DROP MATERIALIZED VIEW do to the
tables they name. The real views, on Trino and Iceberg, are in tests/test_trino_e2e.py."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from cairn.common.db import init_db
from cairn.pipelines.derived import DerivedError, DerivedTables
from cairn.pipelines.matviews import MaterializedViews

ANA = "urn:li:corpuser:ana"


@pytest.fixture()
def mv(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    tables = {("s", "orders")}                            # an ordinary table, not kept by Cairn
    ran: list[tuple[str, str]] = []

    def run(owner, sql):
        ran.append((owner, sql))
        if "information_schema.tables" in sql:
            return [[sum(1 for s, t in tables if f"table_schema = '{s}'" in sql and f"table_name = '{t}'" in sql)]]
        if sql.startswith("CREATE OR REPLACE TABLE"):
            tables.add(tuple(p.strip('"') for p in sql.split()[4].split(".")[1:]))
        return []
    views = MaterializedViews(DerivedTables(engine, run, "lake"), "lake")
    views.ran = ran
    return views


def test_a_materialized_view_does_not_replace_a_table_that_is_there(mv):
    with pytest.raises(DerivedError) as e:
        mv.handle(ANA, "CREATE MATERIALIZED VIEW lake.s.orders AS SELECT 1 AS one")
    assert e.value.status == 409 and "exists" in str(e.value)
    assert not any("CREATE OR REPLACE" in s for _, s in mv.ran)          # the table's rows are still there
    assert mv.derived.list() == []
    with pytest.raises(DerivedError):                                     # IF NOT EXISTS: not a view of Cairn's
        mv.handle(ANA, "CREATE MATERIALIZED VIEW IF NOT EXISTS s.orders AS SELECT 1 AS one")
    assert not any("CREATE OR REPLACE" in s for _, s in mv.ran)
    assert "created" in mv.handle(ANA, "CREATE MATERIALIZED VIEW s.daily AS SELECT 1 AS one")
    assert "exists" in mv.handle(ANA, "CREATE MATERIALIZED VIEW IF NOT EXISTS s.daily AS SELECT 1 AS one")


def test_a_drop_trino_refuses_keeps_the_view_and_says_why(mv):
    from cairn.dataaccess.service import DataAccessError

    mv.handle(ANA, "CREATE MATERIALIZED VIEW s.daily AS SELECT 1 AS one")
    run = mv.derived.run

    def held(owner, sql):                                 # e.g. a legal hold on the view's table
        if sql.startswith("DROP TABLE"):
            raise DataAccessError("Access Denied: Cannot drop table lake.s.daily", 403)
        return run(owner, sql)
    mv.derived.run = held
    with pytest.raises(DerivedError) as e:                # an answer the SQL editor shows, not a server error
        mv.handle(ANA, "DROP MATERIALIZED VIEW s.daily")
    assert e.value.status == 403 and "Cannot drop" in str(e.value)
    assert [d["target"] for d in mv.derived.list()] == ["s.daily"]       # still kept, and still refreshed
