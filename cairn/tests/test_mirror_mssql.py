"""SQL Server mirrors, the parts that need no server: how changes read from change data capture are grouped into
whole transactions and resumed after a run. The end-to-end test against a real SQL Server is
``test_mirror_mssql_e2e.py``."""

import types

from sqlalchemy import create_engine, insert, select

from cairn.pipelines import mirror_mssql as ms
from cairn.pipelines.mirror import Mirrors, mirrors_table


def lsn(vlf: int, block: int, slot: int) -> bytes:
    return vlf.to_bytes(4, "big") + block.to_bytes(4, "big") + slot.to_bytes(2, "big")


class FakeStreams:
    """The streams' writer as a mirror uses it: one commit per write, recording the next offset."""

    def __init__(self):
        self.next = None
        self.rows: list = []

    def offsets(self, stream):
        return {} if self.next is None else {0: self.next}

    def write(self, stream, msgs, reject=None):
        for m in msgs:
            self.next = max(self.next or 0, m.offset + 1)
            self.rows.append(m.value)
        return len(msgs)


def mirror(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/m.db")
    mirrors_table.create(engine)
    streams = FakeStreams()
    m = Mirrors(engine, streams, lambda name: "mssql://sa:pw@127.0.0.1:1433/shop")
    with engine.begin() as c:
        c.execute(insert(mirrors_table).values(
            id="m1", name="orders", connection="shop", source="dbo.orders", target="lake.orders", key="id",
            enabled=True, snapshot_done=True, status="running", error=None, rows=0, last_lsn=ms.lsn_hex(lsn(32, 1, 1)),
            last_commit_ms=None, created_by="urn:li:corpuser:ops", created_ms=0))
    return m, streams, engine


def test_transactions_committed_in_one_log_block_are_all_applied(tmp_path, monkeypatch):
    # Two transactions whose commit LSNs share their log block (group commit) differ only in the slot. The first
    # run applies the first; the second must still apply the second.
    t1, t2 = lsn(32, 256, 2), lsn(32, 256, 5)
    feed = {ms.lsn_hex(lsn(32, 1, 1)): ([(ms.offset(t1), "upsert", {"id": 1, "v": "a"})], t1),
            ms.lsn_hex(t1): ([(ms.offset(t2), "upsert", {"id": 2, "v": "b"})], t2),
            ms.lsn_hex(t2): ([], None)}
    monkeypatch.setattr(ms, "connect", lambda url: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(ms, "check", lambda conn, schema, table: (["id"], "dbo_orders"))
    monkeypatch.setattr(ms, "changes", lambda conn, instance, after, key: feed[ms.lsn_hex(after)])
    m, streams, engine = mirror(tmp_path)
    assert m.run("orders") == 1
    assert m.run("orders") == 1, "the second transaction of the log block was skipped"
    assert [r[1]["id"] for r in streams.rows] == [1, 2]
    with engine.connect() as c:
        assert c.execute(select(mirrors_table.c.last_lsn)).scalar() == ms.lsn_hex(t2)


def test_a_replay_after_a_crash_skips_only_older_log_blocks(tmp_path, monkeypatch):
    # The lakehouse has t1 and t2 (committed), the mirror's own record still says before t1 (a crash between the
    # two). The replay skips what lies in older blocks and writes again what shares the last block: same rows.
    t0, t1, t2 = lsn(32, 100, 1), lsn(32, 256, 2), lsn(32, 256, 5)
    got = [(ms.offset(t0), "upsert", {"id": 0}), (ms.offset(t1), "upsert", {"id": 1}),
           (ms.offset(t2), "upsert", {"id": 2})]
    monkeypatch.setattr(ms, "connect", lambda url: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(ms, "check", lambda conn, schema, table: (["id"], "dbo_orders"))
    monkeypatch.setattr(ms, "changes", lambda conn, instance, after, key: (got, t2))
    m, streams, _ = mirror(tmp_path)
    streams.next = ms.offset(t2) + 1
    m.run("orders")
    assert [r[1]["id"] for r in streams.rows] == [1, 2]
