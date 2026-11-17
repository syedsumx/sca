"""Change data feed (cairn.pipelines.change_feed) without Trino: which snapshots it compares. The reads themselves,
as the caller on real Trino and Iceberg, are covered by tests/test_trino_e2e.py."""

from __future__ import annotations

from datetime import datetime, timezone

from cairn.pipelines.change_feed import ChangeFeed

ADMIN = "urn:li:corpuser:admin"
A, B, C = 11, 22, 33          # committed in this order


class FakeTrino:
    def __init__(self, current: int):
        self.current = current
        self.ran: list[str] = []

    def __call__(self, actor, sql, limit):
        self.ran.append(sql)
        if '$snapshots"' in sql:
            snaps = [(A, None, 9), (B, A, 10), (C, B, 11)]
            return [], [[sid, parent, datetime(2026, 11, 1, h, tzinfo=timezone.utc), "append", "3", "0", "0", "3",
                         "0", "0"] for sid, parent, h in snaps], False
        if '$refs"' in sql:
            return ["snapshot_id"], [[self.current]], False
        return ["_change_type", "id"], [], False


def test_changes_run_until_the_current_snapshot_after_a_restore():
    # The table was restored to B after C was committed: C is no longer the table, so changes "until now" end at B.
    feed = ChangeFeed(FakeTrino(current=B), "lakehouse")
    out = feed.changes(ADMIN, "sales", "orders", str(A))
    assert out["until_snapshot"] == B
    assert any("FOR VERSION AS OF 22" in q for q in feed.run.ran)
    assert not any("FOR VERSION AS OF 33" in q for q in feed.run.ran)
    feed.run.ran.clear()
    out = feed.changes(ADMIN, "sales", "orders", str(B))                 # already at the current snapshot
    assert out["changes"] == [] and out["until_snapshot"] == B
    assert not any("FOR VERSION AS OF" in q for q in feed.run.ran)


def test_since_as_a_time_without_a_zone_is_utc():
    # Snapshot times carry a zone; a time given without one is UTC, not a comparison that fails (a 500).
    feed = ChangeFeed(FakeTrino(current=C), "lakehouse")
    assert feed.changes(ADMIN, "sales", "orders", "2026-11-01T10:30:00")["since_snapshot"] == B
    assert feed.changes(ADMIN, "sales", "orders", "2026-11-01T10:30:00Z")["since_snapshot"] == B
    assert feed.changes(ADMIN, "sales", "orders", "2026-11-01T12:30:00+02:00")["since_snapshot"] == B
