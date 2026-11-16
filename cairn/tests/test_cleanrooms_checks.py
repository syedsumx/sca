"""Clean-room template checks and differential privacy, without Trino: the room's runs go to a fake that records the
SQL Cairn would send and returns fixed rows. The end-to-end behaviour on Trino is in tests/test_trino_e2e.py."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from cairn.common.db import init_db
from cairn.dataaccess.cleanrooms import CleanRooms, RoomError

A, B = "urn:li:corpuser:alice", "urn:li:corpuser:bob"
TA, TB = "lakehouse.shop.a", "lakehouse.ads.b"


@pytest.fixture()
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path}/r.db")
    init_db(e)
    return e


class Runs:
    def __init__(self, rows=None):
        self.sql: list[str] = []
        self.rows = rows if rows is not None else [["EU", 12, None]]

    def __call__(self, actor, sql):
        self.sql.append(sql)
        return ["k", "n", "s"], [list(r) for r in self.rows]


def room(engine, privacy=None, runs=None):
    cr = CleanRooms(engine, runs or Runs(), lambda actor, party: actor == party,
                    lambda actor, table: (actor, table) in {(A, TA), (B, TB)})
    body = {"name": "ads", "parties": [A, B], "min_group": 10}
    if privacy:
        body["privacy"] = privacy
    cr.create(A, body)
    cr.contribute(A, "ads", TA)
    cr.contribute(B, "ads", TB)
    return cr


def proposed(cr, sql, **extra):
    cr.propose(A, "ads", {"name": "t", "sql": sql, **extra})
    cr.approve(B, "ads", "t")


# ---- every room ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("privacy", [None, {"epsilon": 1, "budget": 3}])
@pytest.mark.parametrize("column", [
    "(SELECT b.email FROM lakehouse.ads.b b WHERE b.id = :id) AS who",       # a raw value, looked up by argument
    "(SELECT sum(b.spend) FROM lakehouse.ads.b b WHERE b.id = :id) AS spend",  # an "aggregate" of one row
])
def test_a_result_column_is_never_a_subquery(engine, privacy, column):
    """A scalar subquery in the result is answered outside the room's grouping, so neither the k floor nor the
    noise covers it: one approval would let a party read the other's rows one argument at a time."""
    cr = room(engine, privacy)
    with pytest.raises(RoomError) as e:
        cr.propose(A, "ads", {"name": "t", "params": [{"name": "id", "type": "integer"}],
                              "sql": f"SELECT {column}, count(*) AS n FROM lakehouse.shop.a a"})
    assert e.value.status == 400 and "subquer" in str(e.value)
