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


class FakeCdc:
    """A connection whose change function returns ``n`` one-row transactions, counting the rows read."""

    def __init__(self, n: int):
        self.n, self.read = n, 0

    def cursor(self, as_dict=False):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=()):
                self.it = iter(range(conn.n))

            def fetchone(self):
                return (lsn(1, 0, 0), lsn(1, 0, 0), lsn(999, 0, 0))

            def _row(self, i):
                conn.read += 1
                return {"__$start_lsn": lsn(2, i, 0), "__$seqval": lsn(2, i, 0), "__$operation": 2, "id": i}

            def fetchall(self):
                return [self._row(i) for i in self.it]

            def fetchmany(self, size=1):
                return [self._row(i) for _, i in zip(range(size), self.it)]

        return Cur()


def test_changes_are_read_no_further_than_one_run_applies():
    conn = FakeCdc(ms.MAX_ROWS * 5)
    got, end = ms.changes(conn, "dbo_orders", lsn(1, 5, 0), ["id"])
    assert len(got) == ms.MAX_ROWS and end == lsn(2, ms.MAX_ROWS - 1, 0)
    assert conn.read <= ms.MAX_ROWS + 10_000, f"{conn.read} change rows held in memory for one run"
