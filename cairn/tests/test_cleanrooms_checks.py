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


def room(engine, privacy=None, runs=None, name="ads"):
    cr = CleanRooms(engine, runs or Runs(), lambda actor, party: actor == party,
                    lambda actor, table: (actor, table) in {(A, TA), (B, TB)})
    body = {"name": name, "parties": [A, B], "min_group": 10}
    if privacy:
        body["privacy"] = privacy
    cr.create(A, body)
    cr.contribute(A, name, TA)
    cr.contribute(B, name, TB)
    return cr


def proposed(cr, sql, name="ads", **extra):
    cr.propose(A, name, {"name": "t", "sql": sql, **extra})
    cr.approve(B, name, "t")


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


# ---- differential privacy ---------------------------------------------------------------------------------------
DP = {"epsilon": 1, "budget": 3}


@pytest.mark.parametrize("group", ["ROLLUP(a.region)", "CUBE(a.region, a.channel)",
                                   "GROUPING SETS ((a.region), (a.channel))", "a.region, ROLLUP(a.channel)"])
def test_private_rooms_refuse_groupings_that_count_a_person_in_several_groups(engine, group):
    """The noise is scaled for each person in one group of the result. ROLLUP, CUBE and GROUPING SETS put every row
    in several (its region and the total), so each answer would reveal more than its epsilon."""
    cr = room(engine, DP)
    with pytest.raises(RoomError) as e:
        cr.propose(A, "ads", {"name": "t", "sql": "SELECT a.region, a.channel, count(*) AS n FROM lakehouse.shop.a a "
                                                  f"GROUP BY {group}"})
    assert e.value.status == 400 and "GROUPING SETS" in str(e.value)
    # Without privacy the k floor applies to each grouping's rows as usual.
    proposed(room(engine, name="plain"), "SELECT a.region, a.channel, count(*) AS n FROM lakehouse.shop.a a "
                                         f"GROUP BY {group}", "plain")


SPEND = ("SELECT a.region AS k, count(*) AS n, sum(b.spend) AS s FROM lakehouse.shop.a a "
         "JOIN lakehouse.ads.b b ON a.id = b.id GROUP BY a.region")


def test_a_private_sum_of_only_missing_values_is_noised_like_any_other(engine):
    """SQL's sum of only NULLs is NULL. Released as is, it told exactly whether anyone in the group had a value: one
    person's value turned the answer from null into a number. It is a sum of 0, and gets its noise."""
    runs = Runs([["EU", 12, None]])
    cr = room(engine, {"epsilon": 1, "budget": 10}, runs)
    proposed(cr, SPEND, bounds={"b.spend": [0, 100]})
    sums = set()
    for _ in range(3):
        out = cr.run_template(B, "ads", "t")
        assert out["rows"][0][0] == "EU" and isinstance(out["rows"][0][2], float)
        sums.add(out["rows"][0][2])
    assert len(sums) > 1                                      # fresh noise each run
    assert "coalesce" in runs.sql[-1].lower()                 # each clipped value: a missing one counts as 0
