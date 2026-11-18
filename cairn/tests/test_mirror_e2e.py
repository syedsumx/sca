"""Mirrors (Snowflake gap K, part 2): a PostgreSQL table kept in a lakehouse table through logical replication, with
the real store and catalog and a PostgreSQL 16 server with wal_level = logical.

What is checked:
- the copy at the start, then inserts, updates (a key change too), deletes, a new column, an unchanged TOASTed
  value, NULLs and the PostgreSQL types;
- a TRUNCATE empties the lakehouse table;
- a crash between the lakehouse commit and the slot's advance writes nothing twice;
- pausing keeps the slot, and the status shows the WAL it holds back;
- deleting the mirror drops the slot and the publication;
- a table without a primary key, and a server without logical WAL, are refused."""

import datetime as dt
import os
import subprocess
import time
import uuid

import pytest

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake, port  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker and the store and catalog binaries")


def start_pg(wal_logical: bool):
    p = port()
    name = f"cairn-mirror-pg-{'l' if wal_logical else 'r'}-{os.getpid()}"
    args = ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{p}:5432", "-e", "POSTGRES_PASSWORD=pgpass",
            "postgres:16-alpine"]
    if wal_logical:
        args += ["-c", "wal_level=logical"]
    subprocess.run(args, check=True, capture_output=True)
    url = f"postgresql://postgres:pgpass@127.0.0.1:{p}/postgres"
    import psycopg

    end = time.time() + 60
    while True:
        try:
            psycopg.connect(url, connect_timeout=2).close()
            break
        except psycopg.OperationalError:
            if time.time() > end:
                raise
            time.sleep(0.5)
    return name, url


@pytest.fixture(scope="module")
def world(lake, tmp_path_factory):  # noqa: F811
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.pipelines.mirror import Mirrors
    from cairn.pipelines.streams import Streams

    pg, url = start_pg(True)
    plain, plain_url = start_pg(False)
    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('mirror')}/cairn.db")
    init_db(engine)
    streams = Streams(engine, {}, {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                                   "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                                   "s3.region": "us-east-1", "s3.path-style-access": "true"})
    m = Mirrors(engine, streams, {"crm": url, "plain": plain_url}.get)
    yield {"m": m, "streams": streams, "url": url}
    subprocess.run(["docker", "rm", "-f", "-v", pg, plain], capture_output=True)


def sql(url, *statements):
    import psycopg

    with psycopg.connect(url, autocommit=True) as c:
        out = None
        for s in statements:
            cur = c.execute(s)
            out = cur.fetchall() if cur.description else None
        return out


def rows(world, target, key="id"):
    t = world["streams"]._catalog().load_table(tuple(target.split(".")))
    return sorted(t.scan().to_arrow().to_pylist(), key=lambda r: r[key])


def test_a_table_is_copied_then_kept_in_step(world):
    m, url = world["m"], world["url"]
    big = "x" * 9000                                  # stored out of line (TOAST)
    sql(url, "CREATE TABLE public.customers (id int PRIMARY KEY, name text, score numeric(10,2), active boolean, "
             "born date, seen timestamptz, notes text)",
        "ALTER TABLE public.customers ALTER COLUMN notes SET STORAGE EXTERNAL",
        f"INSERT INTO public.customers VALUES (1, 'Ana', 9.5, true, '1990-04-01', '2026-11-01 10:00:00+00', '{big}'), "
        "(2, 'Bob', NULL, false, NULL, NULL, 'short')")
    out = m.create("urn:li:corpuser:ops", {"name": "customers", "connection": "crm", "table": "public.customers",
                                           "target": "crm_mirror.customers"})
    assert out["status"] == "running" and out["key"] == ["id"], out
    got = rows(world, "crm_mirror.customers")
    assert [(r["id"], r["name"], r["score"], r["active"]) for r in got] == [(1, "Ana", 9.5, True), (2, "Bob", None, False)]
    assert got[0]["born"] == dt.date(1990, 4, 1) and got[0]["notes"] == big
    # Changes since: insert, update (the TOASTed notes untouched), delete, a key change, a new column.
    sql(url, "INSERT INTO public.customers (id, name) VALUES (3, 'Cal')",
        "UPDATE public.customers SET score = 7.25 WHERE id = 1",
        "DELETE FROM public.customers WHERE id = 2",
        "UPDATE public.customers SET id = 30 WHERE id = 3",
        "ALTER TABLE public.customers ADD COLUMN tier text",
        "INSERT INTO public.customers (id, name, tier) VALUES (4, 'Dee', 'gold')")
    assert m.run("customers") >= 4
    got = rows(world, "crm_mirror.customers")
    assert [r["id"] for r in got] == [1, 4, 30]
    assert got[0]["score"] == 7.25 and got[0]["notes"] == big            # the unchanged TOASTed value kept
    assert got[1]["tier"] == "gold" and got[2]["name"] == "Cal"
    assert m.run("customers") == 0                                       # nothing new: nothing written
    status = m.get("customers")
    assert status["rows"] >= 6 and status["last_lsn"] and status["retained_bytes"] is not None


def test_a_crash_between_commit_and_advance_writes_nothing_twice(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE TABLE public.events (id bigint PRIMARY KEY, v int)")
    m.create("urn:li:corpuser:ops", {"name": "events", "connection": "crm", "table": "public.events",
                                     "target": "crm_mirror.events"})
    sql(url, "INSERT INTO public.events SELECT g, g FROM generate_series(1, 50) g")
    # The lakehouse commit happens; the slot is not advanced (the process died).
    real = m._connect

    class NoAdvance:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.conn.close()

        def execute(self, q, *a):
            if "pg_replication_slot_advance" in q:
                raise RuntimeError("crashed before advancing the slot")
            return self.conn.execute(q, *a)

        def cursor(self, *a, **k):
            return self.conn.cursor(*a, **k)

        def __getattr__(self, name):
            return getattr(self.conn, name)
    m._connect = lambda c: NoAdvance(real(c))
    try:
        m.run("events")
    finally:
        m._connect = real
    assert m.get("events")["status"] == "failed"
    assert len(rows(world, "crm_mirror.events")) == 50                    # committed
    sql(url, "UPDATE public.events SET v = -1 WHERE id = 7")
    m.run("events")                                                       # replays the 50, then the update
    got = rows(world, "crm_mirror.events")
    assert len(got) == 50 and got[6]["v"] == -1
    snaps = world["streams"]._catalog().load_table(("crm_mirror", "events")).snapshots()
    assert m.get("events")["status"] == "running" and len(snaps) <= 6     # the replay wrote no extra rows


def test_a_transaction_that_began_first_and_commits_last_is_not_skipped(world):
    import psycopg

    m, url = world["m"], world["url"]
    sql(url, "CREATE TABLE public.overlap (id int PRIMARY KEY)")
    m.create("urn:li:corpuser:ops", {"name": "overlap", "connection": "crm", "table": "public.overlap",
                                     "target": "crm_mirror.overlap"})
    with psycopg.connect(url) as slow, psycopg.connect(url) as fast:
        slow.execute("INSERT INTO public.overlap VALUES (1)")              # written first, committed last
        fast.execute("INSERT INTO public.overlap VALUES (2)")
        fast.commit()
        assert m.run("overlap") == 1
        slow.commit()
    assert m.run("overlap") == 1
    assert [r["id"] for r in rows(world, "crm_mirror.overlap")] == [1, 2]
    m.delete("urn:li:corpuser:ops", "overlap")                              # (the tests below count the publications)


def test_truncate_pause_and_delete(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE TABLE public.carts (id int PRIMARY KEY)", "INSERT INTO public.carts VALUES (1), (2)")
    m.create("urn:li:corpuser:ops", {"name": "carts", "connection": "crm", "table": "public.carts",
                                     "target": "crm_mirror.carts"})
    sql(url, "TRUNCATE public.carts", "INSERT INTO public.carts VALUES (9)")
    m.run("carts")
    assert [r["id"] for r in rows(world, "crm_mirror.carts")] == [9]
    m.set_enabled("urn:li:corpuser:ops", "carts", False)
    sql(url, "INSERT INTO public.carts VALUES (10)")
    assert m.run("carts") == 0 and m.get("carts")["status"] == "paused"
    assert m.get("carts")["retained_bytes"] > 0                           # the slot holds the WAL back
    m.set_enabled("urn:li:corpuser:ops", "carts", True)
    m.run("carts")
    assert [r["id"] for r in rows(world, "crm_mirror.carts")] == [9, 10]
    m.delete("urn:li:corpuser:ops", "carts")
    left = sql(url, "SELECT count(*) FROM pg_replication_slots WHERE slot_name LIKE 'cairn_mirror_%%'",
               "SELECT count(*) FROM pg_publication WHERE pubname LIKE 'cairn_mirror_%%'")
    assert left[0][0] == 2                                                 # the two other mirrors' publications


def test_what_cannot_be_mirrored_is_refused(world):
    from cairn.pipelines.mirror import MirrorError

    m, url = world["m"], world["url"]
    sql(url, "CREATE TABLE public.nokey (v int)")
    with pytest.raises(MirrorError, match="primary key"):
        m.create("urn:li:corpuser:ops", {"name": "nokey", "connection": "crm", "table": "public.nokey",
                                         "target": "crm_mirror.nokey"})
    with pytest.raises(MirrorError, match="wal_level"):
        m.create("urn:li:corpuser:ops", {"name": "plain", "connection": "plain", "table": "public.x",
                                         "target": "crm_mirror.x"})
    for bad in [{"name": "Bad"}, {"name": "ok", "table": "x"}, {"name": "ok", "table": "a.b", "target": "x"}]:
        with pytest.raises(MirrorError):
            m.create("urn:li:corpuser:ops", {"connection": "crm", "table": "public.t", "target": "a.b",
                                             **bad, "name": bad.get("name", "ok") + uuid.uuid4().hex[:4]
                                             if bad.get("name") != "Bad" else "Bad"})
    assert sql(url, "SELECT count(*) FROM pg_publication WHERE pubname LIKE 'cairn_mirror_%%'")[0][0] == 2



def test_a_mirror_never_takes_over_a_table_that_is_there(world):
    """The copy empties its table first and a TRUNCATE empties it again: a mirror whose target exists (someone's
    table under the same name, or another mirror's) is refused and leaves the table and the source untouched."""
    import pyarrow as pa

    from cairn.pipelines.mirror import MirrorError

    m, url = world["m"], world["url"]
    cat = world["streams"]._catalog()
    try:
        cat.create_namespace(("crm_mirror",))
    except Exception:  # noqa: BLE001 - it exists
        pass
    theirs = cat.create_table(("crm_mirror", "ledger"), schema=pa.schema([("id", pa.int64()), ("v", pa.int64())]))
    theirs.append(pa.table({"id": pa.array([1, 2], pa.int64()), "v": pa.array([10, 20], pa.int64())}))
    sql(url, "CREATE TABLE public.ledger (id int PRIMARY KEY, v int)", "INSERT INTO public.ledger VALUES (9, 9)")
    before = sql(url, "SELECT count(*) FROM pg_publication WHERE pubname LIKE 'cairn_mirror_%%'")[0][0]
    with pytest.raises(MirrorError, match="exists") as e:
        m.create("urn:li:corpuser:ops", {"name": "ledger", "connection": "crm", "table": "public.ledger",
                                         "target": "crm_mirror.ledger"})
    assert e.value.status == 409
    assert [r["id"] for r in rows(world, "crm_mirror.ledger")] == [1, 2]
    assert sql(url, "SELECT count(*) FROM pg_publication WHERE pubname LIKE 'cairn_mirror_%%'")[0][0] == before


def test_a_copy_retried_after_a_failure_keeps_only_its_own_rows(world, monkeypatch):
    """The copy dies after its first batch is in the lakehouse; a copied row is then deleted at the source. The
    retried copy starts from an empty table, so the deleted row does not come back."""
    m = world["m"]
    url = world["url"]
    sql(url, "CREATE TABLE public.retried (id int PRIMARY KEY, v int)",
        "INSERT INTO public.retried SELECT g, g FROM generate_series(1, 5) g")
    real, failed = m._write, []

    def write_then_die(r, changes, topic=None):
        n = real(r, changes, topic)
        if topic and topic.endswith(".copy") and not failed:
            failed.append(n)
            raise RuntimeError("the copy died")
        return n
    monkeypatch.setattr(m, "_write", write_then_die)
    out = m.create("urn:li:corpuser:ops", {"name": "retried", "connection": "crm", "table": "public.retried",
                                           "target": "crm_mirror.retried"})
    assert out["status"] == "failed" and failed == [5] and len(rows(world, "crm_mirror.retried")) == 5
    sql(url, "DELETE FROM public.retried WHERE id = 3")
    m.run("retried")
    assert [r["id"] for r in rows(world, "crm_mirror.retried")] == [1, 2, 4, 5]
    assert m.get("retried")["status"] == "running"


def test_copied_rows_and_changed_rows_carry_the_same_values(world):
    """The copy and the change stream give a value the same form, whatever its type: an array, JSON, bytes, an
    address, an interval, a time of day, a generated column, and an unchanged TOASTed value read back by key."""
    m, url = world["m"], world["url"]
    big = "{" + ",".join(str(i) for i in range(3000)) + "}"        # an int[] stored out of line (TOAST)
    vals = "'{1,2}', '{\"b\": \"é\", \"a\": [1]}', '\\x0102', '10.0.0.1', '1 day 02:00', '10:30'"
    sql(url, "CREATE TABLE public.shapes (id int PRIMARY KEY, tags int[], doc jsonb, raw bytea, ip inet, span interval, "
             "at time, g int GENERATED ALWAYS AS (id * 2) STORED, n int, big int[])",
        "ALTER TABLE public.shapes ALTER COLUMN big SET STORAGE EXTERNAL",
        f"INSERT INTO public.shapes (id, tags, doc, raw, ip, span, at, n, big) VALUES (1, {vals}, 0, '{big}')")
    out = m.create("urn:li:corpuser:ops", {"name": "shapes", "connection": "crm", "table": "public.shapes",
                                           "target": "crm_mirror.shapes"})
    assert out["status"] == "running", out
    sql(url, f"INSERT INTO public.shapes (id, tags, doc, raw, ip, span, at, n, big) VALUES (2, {vals}, 1, '{big}')",
        "UPDATE public.shapes SET n = 1 WHERE id = 1")                 # big unchanged: read back by key
    m.run("shapes")
    copied, changed = rows(world, "crm_mirror.shapes")
    assert copied["n"] == 1 and m.get("shapes")["status"] == "running"
    for c in ("tags", "doc", "raw", "ip", "span", "at", "g", "big"):
        assert copied.get(c) == changed.get(c), (c, copied.get(c), changed.get(c))
    m.delete("urn:li:corpuser:ops", "shapes")


def test_a_slot_whose_table_is_idle_still_lets_the_server_recycle_its_log(world):
    """Writes elsewhere make WAL that the slot of a mirror whose table is quiet would otherwise hold back for good:
    pgoutput sends nothing for them, so no transaction of the mirror's ever moves the slot."""
    import psycopg

    m, url = world["m"], world["url"]
    sql(url, "CREATE TABLE public.quiet (id int PRIMARY KEY)", "CREATE TABLE public.busy (id int, v text)")
    m.create("urn:li:corpuser:ops", {"name": "quiet", "connection": "crm", "table": "public.quiet",
                                     "target": "crm_mirror.quiet"})
    for _ in range(5):
        sql(url, "INSERT INTO public.busy SELECT g, repeat('x', 200) FROM generate_series(1, 2000) g")
    written = sql(url, "SELECT pg_current_wal_flush_lsn()::text")[0][0]
    with psycopg.connect(url) as open_txn:                      # a change of its own, not yet committed
        open_txn.execute("INSERT INTO public.quiet VALUES (1)")
        assert m.run("quiet") == 0
        slot = f"cairn_mirror_{m._row('quiet').id}"
        assert sql(url, f"SELECT confirmed_flush_lsn >= '{written}'::pg_lsn FROM pg_replication_slots "
                        f"WHERE slot_name = '{slot}'")[0][0], "the slot did not move past WAL it has nothing in"
        open_txn.commit()
    assert m.run("quiet") == 1                                  # the open transaction was not skipped
    assert [r["id"] for r in rows(world, "crm_mirror.quiet")] == [1]
    m.delete("urn:li:corpuser:ops", "quiet")


def test_truncating_one_partition_leaves_the_others(world):
    """A partitioned table's partitions are published with it, so a TRUNCATE of one partition (how old data is
    often let go) arrives as a truncate of that partition alone: only its rows may leave the lakehouse table."""
    m, url = world["m"], world["url"]
    sql(url, "CREATE TABLE public.sales (id int, year int, PRIMARY KEY (id, year)) PARTITION BY LIST (year)",
        "CREATE TABLE public.sales_2025 PARTITION OF public.sales FOR VALUES IN (2025)",
        "CREATE TABLE public.sales_2026 PARTITION OF public.sales FOR VALUES IN (2026)",
        "INSERT INTO public.sales VALUES (1, 2025), (2, 2026)")
    m.create("urn:li:corpuser:ops", {"name": "sales", "connection": "crm", "table": "public.sales",
                                     "target": "crm_mirror.sales"})
    assert [r["id"] for r in rows(world, "crm_mirror.sales")] == [1, 2]
    sql(url, "INSERT INTO public.sales VALUES (3, 2025), (4, 2026)", "TRUNCATE public.sales_2025",
        "INSERT INTO public.sales VALUES (5, 2026), (6, 2025)")
    m.run("sales")
    m.run("sales")
    assert [r["id"] for r in rows(world, "crm_mirror.sales")] == [2, 4, 5, 6]
    sql(url, "INSERT INTO public.sales VALUES (7, 2026)", "TRUNCATE public.sales")    # the whole table
    m.run("sales")
    m.run("sales")
    assert rows(world, "crm_mirror.sales") == [] and m.get("sales")["status"] == "running"
    m.delete("urn:li:corpuser:ops", "sales")


def test_a_table_whose_changes_do_not_carry_its_key_is_refused(world):
    """Updates and deletes carry the old row's replica identity, which a mirror needs to be its primary key (or
    the whole row). With another index as the identity, a key change would leave the old row behind and a delete
    would carry no key; with none, the publication would make the source's own updates and deletes fail."""
    from cairn.pipelines.mirror import MirrorError

    m, url = world["m"], world["url"]
    before = sql(url, "SELECT count(*) FROM pg_publication")[0][0]
    sql(url, "CREATE TABLE public.coded (id int PRIMARY KEY, code text NOT NULL)",
        "CREATE UNIQUE INDEX coded_code ON public.coded (code)",
        "ALTER TABLE public.coded REPLICA IDENTITY USING INDEX coded_code",
        "CREATE TABLE public.unkeyed (id int PRIMARY KEY)", "ALTER TABLE public.unkeyed REPLICA IDENTITY NOTHING")
    for table in ("coded", "unkeyed"):
        with pytest.raises(MirrorError, match="replica identity"):
            m.create("urn:li:corpuser:ops", {"name": table, "connection": "crm", "table": f"public.{table}",
                                             "target": f"crm_mirror.{table}"})
    assert sql(url, "SELECT count(*) FROM pg_publication")[0][0] == before
    sql(url, "INSERT INTO public.unkeyed VALUES (1)", "DELETE FROM public.unkeyed")   # the source still works
    sql(url, "ALTER TABLE public.coded REPLICA IDENTITY FULL")                         # the whole row will do
    m.create("urn:li:corpuser:ops", {"name": "coded", "connection": "crm", "table": "public.coded",
                                     "target": "crm_mirror.coded"})
    sql(url, "INSERT INTO public.coded VALUES (1, 'a')", "UPDATE public.coded SET id = 10 WHERE id = 1")
    m.run("coded")
    assert [r["id"] for r in rows(world, "crm_mirror.coded")] == [10]
    m.delete("urn:li:corpuser:ops", "coded")
