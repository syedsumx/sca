"""Snapshot browser and restore (L59) without Trino: a snapshot from before an erasure is not restored, nor shown.
The restore itself, on Trino and Iceberg, is covered by tests/test_trino_e2e.py."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cairn.pipelines.change_feed import ChangeFeed, ChangeFeedError

ADMIN = "urn:li:corpuser:admin"
OLD, DELETED, LATER = 101, 202, 303          # before the erasure, the erasure's own delete, a later append


def ms(h: int) -> int:
    return int(datetime(2026, 11, 1, h, tzinfo=timezone.utc).timestamp() * 1000)


class FakeTrino:
    def __init__(self):
        self.current = LATER
        self.ran: list[str] = []

    def __call__(self, actor, sql, limit):
        self.ran.append(sql)
        if '$snapshots"' in sql:
            snaps = [(OLD, None, 9), (DELETED, OLD, 10), (LATER, DELETED, 12)]
            return [], [[sid, parent, datetime(2026, 11, 1, h, tzinfo=timezone.utc), "append", "3", "0", "0", "3",
                         "0", "0"] for sid, parent, h in snaps], False
        if '$refs"' in sql:
            return ["snapshot_id"], [[self.current]], False
        if "rollback_to_snapshot" in sql:
            self.current = int(sql.rsplit("(", 1)[1].rstrip(")"))
            return [], [], False
        return ["c"], [[1]], False


@pytest.fixture
def feed():
    f = ChangeFeed(FakeTrino(), "lakehouse")
    f.erased = lambda schema, table: ms(10) + 60_000 if (schema, table) == ("sales", "customers") else None
    return f


def test_a_snapshot_from_before_an_erasure_is_not_restored(feed):
    with pytest.raises(ChangeFeedError, match="erased") as e:
        feed.restore(ADMIN, "sales", "customers", str(OLD), "undo yesterday's delete")
    assert e.value.status == 409 and feed.run.current == LATER
    assert not any("rollback_to_snapshot" in q for q in feed.run.ran)
    with pytest.raises(ChangeFeedError, match="erased"):                  # nor by a time before it
        feed.restore(ADMIN, "sales", "customers", "2026-11-01T09:30:00Z", "undo")
    with pytest.raises(ChangeFeedError, match="erased"):                  # nor shown through the browser
        feed.rows(ADMIN, "sales", "customers", str(OLD))


def test_snapshots_after_the_erasure_and_other_tables_restore(feed):
    feed.erased = lambda schema, table: ms(10) - 60_000 if (schema, table) == ("sales", "customers") else None
    out = feed.restore(ADMIN, "sales", "customers", str(DELETED), "back to just after the erasure")
    assert out["to_snapshot"] == DELETED
    other = ChangeFeed(FakeTrino(), "lakehouse")
    other.erased = feed.erased
    assert other.restore(ADMIN, "sales", "orders", str(OLD), "no erasure here")["to_snapshot"] == OLD


def test_erasures_that_cannot_be_checked_refuse_a_restore(feed):
    def broken(schema, table):
        raise RuntimeError("database down")
    feed.erased = broken
    with pytest.raises(ChangeFeedError) as e:
        feed.restore(ADMIN, "sales", "customers", str(DELETED), "x")
    assert e.value.status == 503 and feed.run.current == LATER


def test_records_tell_when_a_tables_personal_data_was_last_erased(tmp_path):
    from sqlalchemy import insert

    from cairn.common.db import get_engine, init_db
    from cairn.governance.privacy import privacy_requests
    from cairn.governance.records import Records

    engine = get_engine(f"sqlite:///{tmp_path}/r.db")
    init_db(engine)
    urn = "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.sales.customers,PROD)"
    other = "urn:li:dataset:(urn:li:dataPlatform:iceberg,lakehouse.sales.orders,PROD)"

    def request(rid, kind, parts):
        with engine.begin() as conn:
            conn.execute(insert(privacy_requests).values(
                id=rid, kind=kind, identifier_kind="email", identifier_hash=rid, identifier_hint="a***",
                state="OPEN", created_by=ADMIN, created_ms=1, due_ms=2, doc={"parts": parts}))
    request("r1", "deletion", [{"dataset": urn, "state": "DONE", "done_ms": 1000, "erasure": {"table": "t"}},
                               {"dataset": other, "state": "PENDING"}])
    request("r2", "deletion", [{"dataset": urn, "state": "DONE", "done_ms": 5000, "erasure": {"table": "t"}}])
    request("r3", "deletion", [{"dataset": other, "state": "HELD", "done_ms": 7000, "holds": ["h1"]}])
    request("r4", "access", [{"dataset": urn, "state": "DONE", "done_ms": 9999}])
    r = Records(engine, None, None)
    assert r.erased_at(urn) == 5000
    assert r.erased_at(other) is None                  # pending, or kept by a hold: nothing erased there yet
    request("r5", "deletion", [{"dataset": other, "state": "DONE", "done_ms": 9000, "note": "erased by its owners"}])
    assert r.erased_at(other) == 9000                  # marked done by hand counts: the rows are gone either way
