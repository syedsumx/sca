"""Trust badges read every monitor's latest status in a fixed number of queries."""

from __future__ import annotations

import uuid

from sqlalchemy import event

from cairn.quality.models import Evaluation, MonitorSpec
from cairn.quality.store import QualityStore

A = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.public.a,PROD)"
B = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.public.b,PROD)"
C = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.public.c,PROD)"


def _eval(store: QualityStore, monitor_id: str, ts_ms: int, status: str) -> None:
    store.add_evaluation(Evaluation(id=uuid.uuid4().hex, monitor_id=monitor_id, ts="", ts_ms=ts_ms,
                                    run_id=uuid.uuid4().hex, metric_value=None, expected_low=None,
                                    expected_high=None, status=status, score=None, message=""), None)


def test_latest_status_per_monitor_in_two_queries(engine):
    store = QualityStore(engine)
    a1 = store.create_monitor(MonitorSpec(name="a1", dataset_urn=A, type="volume"), None)
    a2 = store.create_monitor(MonitorSpec(name="a2", dataset_urn=A, type="freshness"), None)
    b1 = store.create_monitor(MonitorSpec(name="b1", dataset_urn=B, type="volume"), None)
    store.create_monitor(MonitorSpec(name="c1", dataset_urn=C, type="volume"), None)     # never evaluated
    _eval(store, a1.id, 1, "FAIL")
    _eval(store, a1.id, 2, "PASS")          # the latest wins
    _eval(store, a2.id, 5, "WARN")
    _eval(store, b1.id, 3, "FAIL")

    queries = []
    event.listen(engine, "before_cursor_execute", lambda *a: queries.append(a[2]))
    got = store.latest_statuses([A, B, C, "urn:li:dataset:unknown"])
    assert {k: sorted(v) for k, v in got.items()} == {A: ["PASS", "WARN"], B: ["FAIL"]}
    assert len(queries) == 2
    assert store.latest_statuses([]) == {}
