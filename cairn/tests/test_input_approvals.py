"""Input-table approvals: what a reviewer approves is what lands (the writes themselves run on real Trino, as the
author, in tests/test_trino_e2e.py; here a stand-in records each statement)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from cairn.common.db import metadata
from cairn.sqlworkspace.input_approvals import MAX_PENDING, ApprovalError, InputApprovals
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
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
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


def test_one_person_cannot_queue_changes_without_end(setup):
    it, trino, tid = setup
    rows = {"rows": [{"target": 1}] * 500}
    for _ in range(MAX_PENDING):
        assert it.approvals.write(ANA, tid, "add", rows)[0] == 202
    with pytest.raises(ApprovalError) as e:
        it.approvals.write(ANA, tid, "add", rows)
    assert e.value.status == 429
    # Others still can, and a decided change makes room again.
    assert it.approvals.write(BOB, tid, "add", rows)[0] == 202
    first = it.approvals.changes(ANA, tid, "pending")[-1]
    it.approvals.decide(BOB, first["id"], False)
    assert it.approvals.write(ANA, tid, "add", rows)[0] == 202


def test_a_change_names_the_version_it_was_made_on_when_the_author_gave_one(setup):
    it, trino, tid = setup
    code, out = it.approvals.write(ANA, tid, "change", {"row_id": ROW, "values": {"target": 120},
                                                       "expected_updated_at": "2026-11-01T09:00:00Z"})
    it.approvals.decide(BOB, out["pending"], True)
    update = [sql for _, sql in trino.ran if sql.startswith("UPDATE")]
    assert "TIMESTAMP '2026-11-01 09:00:00.000000 UTC'" in update[0]


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_an_agent_acting_for_a_reviewer_does_not_decide(setup, decision):
    """Four eyes are a person's: an agent credential (even with scope write) acting for a reviewer neither approves
    nor rejects, as with infrastructure-as-code change requests."""
    pytest.importorskip("httpx")
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from cairn.common.security import Principal
    from cairn.sqlworkspace.input_tables import build_input_tables_router

    it, trino, tid = setup
    _, out = it.approvals.write(ANA, tid, "add", {"rows": [{"target": 1}]})

    def principal(request: Request) -> Principal:
        via = request.headers.get("x-via", "session")
        return Principal(BOB, via, agent="agt_helper" if via == "agent" else None, scopes=("read", "write"))
    app = FastAPI()
    app.include_router(build_input_tables_router(it, principal, lambda p: False))
    c = TestClient(app)
    r = c.post(f"/sql/input-tables/changes/{out['pending']}/{decision}", headers={"x-via": "agent"}, json={})
    assert r.status_code == 403, r.text
    assert it.approvals.changes(BOB, tid, "pending")[0]["id"] == out["pending"]          # still waiting
    assert c.post(f"/sql/input-tables/changes/{out['pending']}/{decision}", json={}).status_code == 200


def test_the_list_shows_a_person_only_the_input_tables_they_may_see(setup):
    """GET /sql/input-tables listed every table, its columns, creator and reviewers, to anyone."""
    it, _, tid = setup
    eve = "urn:li:corpuser:eve"
    assert [t["id"] for t in it.list(ANA)] == [tid]            # her own
    assert [t["id"] for t in it.list(BOB)] == [tid]            # he reviews it
    assert it.list(eve) == []                                  # no catalog check wired: left out
    it.may_see = lambda actor, connection, table: actor == eve and (connection, table) == ("lake", "sales.targets")
    assert [t["id"] for t in it.list(eve)] == [tid]            # the catalog lets her see it
    it.may_see = lambda *a: (_ for _ in ()).throw(RuntimeError("PDP down"))
    assert it.list(eve) == []                                  # fails closed
