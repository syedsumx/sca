"""Input-table approvals: what a reviewer approves is what lands (the writes themselves run on real Trino, as the
author, in tests/test_trino_e2e.py; here a stand-in records each statement)."""

import pytest
from sqlalchemy import create_engine

from cairn.common.db import metadata
from cairn.sqlworkspace.input_approvals import InputApprovals
from cairn.sqlworkspace.input_tables import InputTables

ANA, BOB = "urn:li:corpuser:ana", "urn:li:corpuser:bob"
ROW = "a" * 32
STAMP = "2026-11-04T10:00:00.120000Z"


class _Trino:
    """Stands in for Trino: the row's _updated_at as read, and the statements run (as whom)."""

    def __init__(self):
        self.ran: list[tuple[str, str]] = []

    def __call__(self, connection, actor, sql):
        self.ran.append((actor, sql))
        if sql.startswith("SELECT to_iso8601"):
            return ["_updated_at"], [[STAMP]]
        if sql.startswith("UPDATE"):
            return ["rows"], [[1]]
        return [], []


@pytest.fixture
def setup():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    trino = _Trino()
    it = InputTables(engine, trino, None)
    it.approvals = InputApprovals(engine, it, lambda u: [], lambda u: False)
    t = it.create(ANA, {"connection": "lake", "table": "sales.targets", "columns": [{"name": "target", "type": "number"}],
                        "reviewers": [BOB]})
    return it, trino, t["id"]


def test_an_approved_change_does_not_overwrite_an_edit_made_while_it_waited(setup):
    it, trino, tid = setup
    # Ana asks to change the row without saying which version she read.
    code, out = it.approvals.write(ANA, tid, "change", {"row_id": ROW, "values": {"target": 120}})
    assert code == 202
    trino.ran.clear()
    it.approvals.decide(BOB, out["pending"], True)
    update = [sql for _, sql in trino.ran if sql.startswith("UPDATE")]
    assert len(update) == 1 and all(actor == ANA for actor, _ in trino.ran)
    # The row as it was when she asked: an edit made meanwhile is a conflict (409, the change fails), not overwritten.
    assert "\"_updated_at\" = TIMESTAMP '2026-11-04 10:00:00.120000 UTC'" in update[0]


def test_a_change_names_the_version_it_was_made_on_when_the_author_gave_one(setup):
    it, trino, tid = setup
    code, out = it.approvals.write(ANA, tid, "change", {"row_id": ROW, "values": {"target": 120},
                                                       "expected_updated_at": "2026-11-01T09:00:00Z"})
    it.approvals.decide(BOB, out["pending"], True)
    update = [sql for _, sql in trino.ran if sql.startswith("UPDATE")]
    assert "TIMESTAMP '2026-11-01 09:00:00.000000 UTC'" in update[0]
