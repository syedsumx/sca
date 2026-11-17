"""Dashboard app actions (cairn.sqlworkspace.actions) without Trino: a stand-in records each statement and as whom
it ran (the writes themselves run on real Trino in tests/test_trino_e2e.py)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from cairn.common.db import metadata
from cairn.sqlworkspace.actions import Actions
from cairn.sqlworkspace.input_approvals import InputApprovals
from cairn.sqlworkspace.input_tables import InputTables

ANA, BOB, CAT = "urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:cat"
ROW = "a" * 32


class _Trino:
    def __init__(self):
        self.ran: list[tuple[str, str]] = []

    def __call__(self, connection, actor, sql):
        self.ran.append((actor, sql))
        if sql.startswith("SELECT to_iso8601"):
            return ["_updated_at"], [["2026-11-04T10:00:00.120000Z"]]
        if sql.startswith("UPDATE"):
            return ["rows"], [[1]]
        return [], []


class _Boards:
    """Ana's dashboard, which everyone may open."""

    def dashboard(self, actor, did):
        return {"id": did, "owner": ANA}


@pytest.fixture
def setup():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    metadata.create_all(engine)
    trino = _Trino()
    it = InputTables(engine, trino, None)
    it.approvals = InputApprovals(engine, it, lambda u: [], lambda u: False)
    t = it.create(ANA, {"connection": "lake", "table": "sales.targets",
                        "columns": [{"name": "target", "type": "number"}, {"name": "status", "type": "text"}]})
    trino.ran.clear()
    return Actions(engine, _Boards(), input_tables=it), it, trino, t["id"]


def _writes(trino):
    return [(actor, sql) for actor, sql in trino.ran if sql.startswith(("INSERT", "UPDATE", "DELETE"))]


def test_an_action_on_a_table_without_review_writes_at_once_as_the_person_clicking(setup):
    a, it, trino, tid = setup
    add = a.add(ANA, "d1", {"name": "add_target", "kind": "input_row", "input_table": tid, "fields": ["target"]})
    out = a.run(BOB, "d1", add["id"], {"target": 5})
    assert out["status"] == "ok" and out["rows"]
    assert [actor for actor, _ in _writes(trino)] == [BOB]


def test_an_action_on_a_reviewed_input_table_asks_for_review_instead_of_writing(setup):
    a, it, trino, tid = setup
    add = a.add(ANA, "d1", {"name": "add_target", "kind": "input_row", "input_table": tid, "fields": ["target"]})
    approve = a.add(ANA, "d1", {"name": "approve", "kind": "input_update", "input_table": tid,
                                "set": {"status": "approved"}})
    it.approvals.set(ANA, tid, [CAT])
    # Bob's clicks become change requests that wait for Cat: nothing lands on the table until she approves.
    out = a.run(BOB, "d1", add["id"], {"target": 5})
    assert out["status"] == "pending" and out["pending"]
    out2 = a.run(BOB, "d1", approve["id"], row={"_row_id": ROW})
    assert out2["status"] == "pending" and out2["pending"]
    assert _writes(trino) == []
    pending = it.approvals.changes(BOB, tid, "pending")
    assert sorted(c["kind"] for c in pending) == ["add", "change"] and all(c["author"] == BOB for c in pending)
    it.approvals.decide(CAT, out["pending"], True)
    assert [actor for actor, _ in _writes(trino)] == [BOB]          # written as Bob, once approved
    runs = a.runs(ANA, "d1", add["id"])
    assert [r["status"] for r in runs] == ["pending"]
