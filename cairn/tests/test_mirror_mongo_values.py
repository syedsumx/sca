"""MongoDB mirrors: BSON values as lakehouse values, without a server (review finding on L97; the mirror itself is
tested against MongoDB 7 in tests/test_mirror_mongo_e2e.py)."""

from __future__ import annotations

import datetime as dt
import json

import pytest

pytest.importorskip("bson")


def test_embedded_dates_and_bson_types_become_json():
    """Review finding: an embedded document or array holding a date (or a BSON timestamp) failed with "Circular
    reference detected", because the JSON hook returned the date again; the whole change, and the mirror, failed."""
    from bson import Decimal128, ObjectId
    from bson.timestamp import Timestamp

    from cairn.pipelines.mirror_mongo import row

    oid = ObjectId()
    doc = {"_id": oid, "meta": {"created": dt.datetime(2026, 11, 1, 10, 0), "by": oid, "amount": Decimal128("9.50"),
                                "seen": Timestamp(1_790_000_000, 1), "raw": b"\x01\x02"},
           "events": [{"at": dt.datetime(2026, 11, 2, 8, 30, tzinfo=dt.timezone.utc)}, "x", 3]}
    out = row(doc)
    assert out["_id"] == str(oid)
    meta = json.loads(out["meta"])
    assert meta == {"created": "2026-11-01T10:00:00+00:00", "by": str(oid), "amount": 9.5,
                    "seen": dt.datetime.fromtimestamp(1_790_000_000, dt.timezone.utc).isoformat(), "raw": "0102"}
    assert json.loads(out["events"]) == [{"at": "2026-11-02T08:30:00+00:00"}, "x", 3]
