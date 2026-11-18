"""SIEM export between notifiers, without a receiver: one notifier ships a sink at a time, for as long as it
ships, and one that lost the sink stops without moving its cursor (the receivers are in test_siem_e2e.py)."""

import json

import httpx
import pytest

from cairn.common import siem as siem_mod
from cairn.common.audit import AuditLog
from cairn.common.db import get_engine, init_db
from cairn.common.siem import SiemExport, siem_cursors


class Receiver:
    """An HTTP sink that accepts everything; ``during(n)`` runs while batch n is in flight."""

    def __init__(self, during=None):
        self.batches: list[list[int]] = []
        self.during = during

    def post(self, url, content, headers):
        seqs = [e["seq"] for e in json.loads(content)]
        if self.during is not None:
            self.during(len(self.batches))
        self.batches.append(seqs)
        return type("R", (), {"status_code": 200, "text": "", "json": lambda self: {}})()


@pytest.fixture
def log(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    audit = AuditLog(engine)
    for i in range(30):
        audit.record("security", "policy.changed", "urn:li:corpuser:ops", None, {"i": i})
    return engine, audit


SINK = [{"name": "lake", "type": "http", "url": "https://collector.example/ingest", "batch": 10}]


def cursor(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(siem_cursors.select().where(siem_cursors.c.sink == "lake")).first().seq


def test_a_notifier_that_lost_the_sink_stops_and_keeps_the_cursor(log):
    engine, audit = log
    other = Receiver()
    b = SiemExport(engine, audit, SINK, http=other)
    seen = []

    def takeover(n):
        if n == 0:                                       # a's lease has run out while its first batch is in flight
            with engine.begin() as conn:
                conn.execute(siem_cursors.update().where(siem_cursors.c.sink == "lake").values(lease_until=0))
            b.ship()                                     # b takes the sink and ships everything
            seen.append(cursor(engine))

    mine = Receiver(during=takeover)
    a = SiemExport(engine, audit, SINK, http=mine)
    a.ship()
    assert seen == [30] and cursor(engine) == 30         # b's cursor stands: a did not move it back
    assert mine.batches == [list(range(1, 11))]          # a stopped after the batch it had in flight
    assert [s for batch in other.batches for s in batch] == list(range(1, 31))


def test_the_lease_is_renewed_while_shipping(log, monkeypatch):
    engine, audit = log
    clock = [10_000_000]
    monkeypatch.setattr(siem_mod, "_now", lambda: clock[0])
    b = SiemExport(engine, audit, SINK, http=Receiver())
    taken = []

    def slow(n):
        clock[0] += 100_000                              # each batch takes 100 s: three outlast one 120 s lease
        taken.append(b._lease("lake", clock[0]))

    a = SiemExport(engine, audit, SINK, http=Receiver(during=slow))
    out = a.ship()
    assert out["lake"] == {"sent": 30, "seq": 30}
    assert taken == [False, False, False]                # nobody else could take the sink meanwhile


def test_a_notifier_whose_lease_ran_out_does_not_move_the_cursor_back(tmp_path):
    """A slow sink can outlast a notifier's lease; another notifier then ships from the cursor. The first must
    not move the cursor back over what the second sent (that would send entries again, out of order)."""
    from cairn.common.siem import SiemExport, siem_cursors
    from sqlalchemy import select, update

    engine = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    audit = AuditLog(engine)
    for i in range(4):
        audit.record("security", "policy.changed", "urn:li:corpuser:ops", None, {"i": i})
    posts: list[tuple[str, list[int]]] = []

    class Http:
        def __init__(self, who):
            self.who = who

        def post(self, url, content, headers):
            posts.append((self.who, [e["seq"] for e in json.loads(content)]))
            if self.who == "a" and len(posts) == 1:          # a's lease runs out while the sink is slow
                with engine.begin() as conn:
                    conn.execute(update(siem_cursors).values(lease_until=0))
                assert b.ship()["lake"] == {"sent": 4, "seq": 4}
            return httpx.Response(200, json={})

    sinks = [{"name": "lake", "type": "http", "url": "http://lake/", "batch": 2}]
    a = SiemExport(engine, audit, sinks, http=Http("a"))
    b = SiemExport(engine, audit, sinks, http=Http("b"))
    a.ship()
    with engine.connect() as conn:
        assert conn.execute(select(siem_cursors.c.seq)).scalar() == 4
    assert posts == [("a", [1, 2]), ("b", [1, 2]), ("b", [3, 4])]
