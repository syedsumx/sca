"""SIEM export's delivery bookkeeping, without a receiver (tests/test_siem_e2e.py has the real ones)."""

import json

import httpx

from cairn.common.audit import AuditLog
from cairn.common.db import get_engine, init_db


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
