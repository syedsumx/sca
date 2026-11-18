"""The snapshot browser (L59) without Trino: snapshots named by time, with or without an offset."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cairn.pipelines.change_feed import ChangeFeed

SNAPS = [(1, None, datetime(2026, 11, 13, 9, tzinfo=timezone.utc)), (2, 1, datetime(2026, 11, 14, 9, tzinfo=timezone.utc))]


def run(actor, sql, limit):
    if "$refs" in sql:
        return ["snapshot_id"], [[2]], False
    if "$snapshots" in sql:
        return [], [[sid, parent, at, "append", "1", None, None, "1", None, None] for sid, parent, at in SNAPS], False
    return ["id"], [[1]], False


@pytest.mark.parametrize("at, sid", [("2026-11-13T12:00", 1), ("2026-11-14", 1), ("2026-11-14T10:00:00Z", 2),
                                     ("2026-11-14T10:59:00+02:00", 1)])
def test_a_time_without_an_offset_is_utc(at, sid):
    assert ChangeFeed(run).rows("urn:li:corpuser:ana", "sales", "orders", at)["snapshot"] == sid
