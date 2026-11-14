"""Database mirrors (L166): a whole PostgreSQL schema kept in the lakehouse through logical replication, with the real
store and catalog, PostgreSQL 16 with wal_level = logical (and a PostgreSQL 14 for what older servers cannot do),
and one read through Trino.

What is checked, one test each:
- a schema's tables are copied, then kept in step by a transaction that spans them; transactions that overlap land;
- a new table is picked up, one without a key waits for its key, patterns select tables;
- a column added, renamed and dropped; a column added with a default (the table is copied again);
- a type that widens in place, and one that cannot (the table is rebuilt, and the audit log says so);
- a table renamed, dropped, and created again;
- a TRUNCATE of one table;
- history mode: versions and their intervals, the same after a replay;
- soft deletes: the row kept with its other columns, cleared by a new insert;
- columns excluded at the source and hashed, the copy and the changes alike;
- a re-sync of one table leaves the others' snapshots alone;
- a crash between the commits and the slot's advance writes nothing twice;
- one table failing loses and duplicates nothing in the others, and holds the WAL until it is re-synced;
- deleting the mirror drops its slot and publication;
- what cannot be mirrored is refused."""

import datetime as dt
import hashlib
import hmac
import os
import subprocess
import threading
import time

import pytest

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, cairn, lake, port, trino  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker and the store and catalog binaries")

OPS = "urn:li:corpuser:ops"
HASH_KEY = b"k" * 32


def start_pg(tag: str, image: str = "postgres:16-alpine", logical: bool = True):
    p = port()
    name = f"cairn-dbmirror-pg-{tag}-{os.getpid()}"
    args = ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{p}:5432", "-e", "POSTGRES_PASSWORD=pgpass",
            image]
    if logical:
        args += ["-c", "wal_level=logical", "-c", "max_replication_slots=40", "-c", "max_wal_senders=40"]
    subprocess.run(args, check=True, capture_output=True)
    url = f"postgresql://postgres:pgpass@127.0.0.1:{p}/postgres"
    import psycopg

    end = time.time() + 90
    while True:
        try:
            psycopg.connect(url, connect_timeout=2).close()
            break
        except psycopg.OperationalError:
            if time.time() > end:
                raise
            time.sleep(0.5)
    return name, url


class Audit:
    def __init__(self):
        self.records = []

    def record(self, *a, **k):
        self.records.append(a)

    def actions(self, name):
        return [(a[1].split(".", 1)[1], a[4]) for a in self.records if a[3] == f"urn:li:databaseMirror:{name}"]


@pytest.fixture(scope="module")
def world(lake, tmp_path_factory):  # noqa: F811
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.pipelines.mirror_db import DatabaseMirrors
    from cairn.pipelines.streams import Streams

    pg, url = start_pg("l")
    plain, plain_url = start_pg("r", logical=False)
    old, old_url = start_pg("o", image="postgres:14-alpine")
    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('dbmirror')}/cairn.db")
    init_db(engine)
    streams = Streams(engine, {}, {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                                   "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                                   "s3.region": "us-east-1", "s3.path-style-access": "true"})
    urls = {"crm": url, "plain": plain_url, "old": old_url, "lakehouse": "trino://x"}
    audit = Audit()
    m = DatabaseMirrors(engine, streams, urls.get, audit, hash_key=lambda: HASH_KEY)
    yield {"m": m, "streams": streams, "url": url, "old_url": old_url, "audit": audit, "engine": engine, "urls": urls}
    subprocess.run(["docker", "rm", "-f", pg, plain, old], capture_output=True)


def sql(url, *statements):
    import psycopg

    with psycopg.connect(url, autocommit=True) as c:
        out = None
        for s in statements:
            cur = c.execute(s)
            out = cur.fetchall() if cur.description else None
        return out


def table(world, target):
    return world["streams"]._catalog().load_table(tuple(target.split(".")))


def rows(world, target, key=("id",)):
    return sorted(table(world, target).scan().to_arrow().to_pylist(), key=lambda r: tuple(r[k] for k in key))


def mirror(world, schema, **body):
    """A mirror of ``schema`` named after it, into the namespace ``m_<schema>``."""
    return world["m"].create(OPS, {"name": schema, "connection": "crm", "schema": schema,
                                   "target_namespace": f"m_{schema}", **body})


def state(out, name):
    return next(t for t in out["mirrored"] if t["table"] == name)


def published(world, name):
    return {r[0]: r[1] for r in sql(world["url"], "SELECT tablename::text, attnames::text[] FROM pg_publication_tables "
                                                  f"WHERE schemaname = '{name}'")}


class NoAdvance:
    """A connection that dies where the slot would be advanced: the commits are in the lakehouse, the slot is not."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.conn.close()

    def execute(self, q, *a):
        if isinstance(q, str) and "pg_replication_slot_advance" in q:
            raise RuntimeError("crashed before advancing the slot")
        return self.conn.execute(q, *a)

    def __getattr__(self, name):
        return getattr(self.conn, name)


def crash_before_advance(m, name):
    real = m._connect
    m._connect = lambda c: NoAdvance(real(c))
    try:
        m.run(name)
    finally:
        m._connect = real


def test_a_schema_is_copied_then_kept_in_step_across_its_tables(world):
    m, url = world["m"], world["url"]
    big = "x" * 9000                                  # stored out of line (TOAST)
    sql(url, "CREATE SCHEMA shop",
        "CREATE TABLE shop.customers (id int PRIMARY KEY, name text, score numeric(10,2), active boolean, born date, "
        "seen timestamptz, notes text, small smallint, ratio real)",
        "ALTER TABLE shop.customers ALTER COLUMN notes SET STORAGE EXTERNAL",
        f"INSERT INTO shop.customers VALUES (1, 'Ana', 9.5, true, '1990-04-01', '2026-11-01 10:00:00+00', '{big}', 7, "
        "1.5), (2, 'Bob', NULL, false, NULL, NULL, 'short', NULL, NULL)",
        "CREATE TABLE shop.orders (id bigint PRIMARY KEY, customer_id int, total double precision)",
        "INSERT INTO shop.orders VALUES (10, 1, 20.5), (11, 2, 5)",
        "CREATE TABLE shop.order_lines (order_id bigint, line_no int, sku text, PRIMARY KEY (order_id, line_no))",
        "INSERT INTO shop.order_lines VALUES (10, 1, 'pen'), (10, 2, 'ink'), (11, 1, 'cap')",
        "CREATE TABLE shop.empty_one (id int PRIMARY KEY, v int)")
    out = mirror(world, "shop")
    assert out["status"] == "running", out
    assert {t["table"]: t["status"] for t in out["mirrored"]} == {
        "customers": "running", "orders": "running", "order_lines": "running", "empty_one": "running"}
    assert state(out, "order_lines")["key"] == ["order_id", "line_no"]
    got = rows(world, "m_shop.customers")
    assert [(r["id"], r["name"], r["score"], r["active"], r["small"], r["ratio"]) for r in got] == \
        [(1, "Ana", 9.5, True, 7, 1.5), (2, "Bob", None, False, None, None)]
    assert got[0]["born"] == dt.date(1990, 4, 1) and got[0]["notes"] == big
    assert got[0]["seen"] == dt.datetime(2026, 11, 1, 10, tzinfo=dt.timezone.utc)
    # Types are the source's, not guessed from the rows: an empty table and all-null columns have them too.
    types = {f.name: str(f.field_type) for f in table(world, "m_shop.customers").schema().fields}
    assert (types["id"], types["small"], types["ratio"], types["score"], types["born"], types["seen"]) == \
        ("int", "int", "float", "double", "date", "timestamptz")
    assert {f.name: str(f.field_type) for f in table(world, "m_shop.empty_one").schema().fields} == {"id": "int", "v": "int"}
    assert len(rows(world, "m_shop.order_lines", ("order_id", "line_no"))) == 3

    # One transaction that spans the tables: all of it lands, each table in its own commit.
    sql(url, "BEGIN",
        "INSERT INTO shop.customers (id, name) VALUES (3, 'Cal')",
        "UPDATE shop.customers SET score = 7.25 WHERE id = 1",
        "INSERT INTO shop.orders VALUES (12, 3, 99)",
        "DELETE FROM shop.orders WHERE id = 11",
        "UPDATE shop.orders SET id = 100 WHERE id = 10",
        "DELETE FROM shop.order_lines WHERE order_id = 11",
        "UPDATE shop.order_lines SET sku = 'quill' WHERE order_id = 10 AND line_no = 1",
        "INSERT INTO shop.order_lines VALUES (12, 1, 'pad')",
        "COMMIT")
    assert m.run("shop") == 9                                            # the key change is a delete and an insert
    got = rows(world, "m_shop.customers")
    assert [r["id"] for r in got] == [1, 2, 3]
    assert got[0]["score"] == 7.25 and got[0]["notes"] == big            # the unchanged TOASTed value kept
    assert [(r["id"], r["total"]) for r in rows(world, "m_shop.orders")] == [(12, 99.0), (100, 20.5)]
    assert [(r["order_id"], r["line_no"], r["sku"]) for r in rows(world, "m_shop.order_lines", ("order_id", "line_no"))] == \
        [(10, 1, "quill"), (10, 2, "ink"), (12, 1, "pad")]
    assert m.run("shop") == 0                                            # nothing new: nothing written
    status = m.get("shop")
    assert status["rows"] >= 16 and status["last_lsn"] and status["retained_bytes"] is not None
    assert [x["name"] for x in m.list()] == ["shop"]

    # Transactions that overlap: one that began first and commits last is not mistaken for one already applied.
    import psycopg

    with psycopg.connect(url) as slow, psycopg.connect(url) as fast:
        slow.execute("INSERT INTO shop.orders VALUES (200, 1, 1)")
        fast.execute("INSERT INTO shop.orders VALUES (201, 1, 2)")
        fast.commit()
        assert m.run("shop") == 1
        slow.commit()
    assert m.run("shop") == 1
    assert [r["id"] for r in rows(world, "m_shop.orders")] == [12, 100, 200, 201]

    # A composite key with many rows in one transaction (a balanced filter, not a deep one).
    sql(url, "INSERT INTO shop.order_lines SELECT 500, g, 'bulk' FROM generate_series(1, 3000) g")
    assert m.run("shop") == 3000
    sql(url, "UPDATE shop.order_lines SET sku = 'bulk2' WHERE order_id = 500")
    assert m.run("shop") == 3000
    lines = rows(world, "m_shop.order_lines", ("order_id", "line_no"))
    assert len(lines) == 3003 and sum(r["sku"] == "bulk2" for r in lines) == 3000


def test_a_mirrored_table_reads_through_trino(world, trino):  # noqa: F811
    assert trino("SELECT id, name, score FROM lake.m_shop.customers ORDER BY id") == [
        {"id": 1, "name": "Ana", "score": 7.25}, {"id": 2, "name": "Bob", "score": None},
        {"id": 3, "name": "Cal", "score": None}]
    assert trino("SELECT count(*) AS n FROM lake.m_shop.order_lines WHERE sku = 'bulk2'") == [{"n": 3000}]


def test_new_tables_are_picked_up_and_one_without_a_key_waits_for_it(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA grow", "CREATE TABLE grow.a (id int PRIMARY KEY, v text)", "INSERT INTO grow.a VALUES (1, 'a')",
        "CREATE TABLE grow.tmp_load (id int PRIMARY KEY)")
    out = mirror(world, "grow", exclude=["tmp_*"])
    assert [t["table"] for t in out["mirrored"]] == ["a"]
    sql(url, "CREATE TABLE grow.b (id int PRIMARY KEY, v text)", "INSERT INTO grow.b VALUES (1, 'early')",
        "CREATE TABLE grow.nokey (id int, v text)", "INSERT INTO grow.nokey VALUES (1, 'n')")
    m.run("grow")
    out = m.get("grow")
    assert state(out, "b")["status"] == "running" and state(out, "nokey")["status"] == "skipped"
    assert "primary key" in state(out, "nokey")["reason"]
    assert set(published(world, "grow")) == {"a", "b"}
    sql(url, "INSERT INTO grow.b VALUES (2, 'late')", "UPDATE grow.nokey SET v = 'still fine at the source'")
    m.run("grow")
    assert [(r["id"], r["v"]) for r in rows(world, "m_grow.b")] == [(1, "early"), (2, "late")]
    sql(url, "ALTER TABLE grow.nokey ADD PRIMARY KEY (id)", "INSERT INTO grow.nokey VALUES (2, 'm')")
    m.run("grow")
    assert state(m.get("grow"), "nokey")["status"] == "running"
    assert [r["v"] for r in rows(world, "m_grow.nokey")] == ["still fine at the source", "m"]
    # The selection can change: a table left out leaves the publication, its lakehouse table stays.
    out = m.update(OPS, "grow", {"exclude": ["tmp_*", "a"], "tables": {"b": {"enabled": False}}})
    assert state(out, "a")["status"] == "skipped" and state(out, "b")["status"] == "skipped"
    assert set(published(world, "grow")) == {"nokey"}
    sql(url, "INSERT INTO grow.a VALUES (2, 'missed while out')")
    m.run("grow")
    assert len(rows(world, "m_grow.a")) == 1
    out = m.update(OPS, "grow", {"exclude": ["tmp_*"], "tables": {}})    # back in: copied again, so nothing is missed
    assert state(out, "a")["status"] == "running" and len(rows(world, "m_grow.a")) == 2
    assert [a for a, _ in world["audit"].actions("grow")].count("table_added") == 3


def test_columns_added_renamed_and_dropped(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA cols", "CREATE TABLE cols.t (id int PRIMARY KEY, a text, b int)",
        "INSERT INTO cols.t VALUES (1, 'one', 10), (2, 'two', 20)")
    mirror(world, "cols")
    first_copy = state(m.get("cols"), "t")["copy_lsn"]
    sql(url, "ALTER TABLE cols.t ADD COLUMN c text", "INSERT INTO cols.t VALUES (3, 'three', 30, 'new')")
    m.run("cols")
    assert [(r["id"], r["c"]) for r in rows(world, "m_cols.t")] == [(1, None), (2, None), (3, "new")]
    # Renamed between two changes of one tick: the change from before the rename lands under the new name.
    sql(url, "UPDATE cols.t SET a = 'before' WHERE id = 1", "ALTER TABLE cols.t RENAME COLUMN a TO label",
        "UPDATE cols.t SET label = 'after' WHERE id = 2")
    m.run("cols")
    got = rows(world, "m_cols.t")
    assert "a" not in got[0] and [r["label"] for r in got] == ["before", "after", "three"]
    # Dropped: the lakehouse column stays, null from then on.
    sql(url, "UPDATE cols.t SET b = 11 WHERE id = 1", "ALTER TABLE cols.t DROP COLUMN b",
        "INSERT INTO cols.t VALUES (4, 'four', 'x')")
    m.run("cols")
    assert [(r["id"], r["b"]) for r in rows(world, "m_cols.t")] == [(1, 11), (2, 20), (3, 30), (4, None)]
    out = state(m.get("cols"), "t")
    assert out["status"] == "running" and out["copy_lsn"] == first_copy    # all in place: never copied again
    # Added with a default: the rows already there have the value at the source, so the table is copied again.
    sql(url, "ALTER TABLE cols.t ADD COLUMN d int DEFAULT 7")
    m.run("cols")
    out = state(m.get("cols"), "t")
    assert [r["d"] for r in rows(world, "m_cols.t")] == [7, 7, 7, 7]
    assert out["status"] == "running" and out["copy_lsn"] != first_copy and "default" in out["reason"]
    assert ("table_rebuilt", {"table": "t", "target": "m_cols.t", "reason": out["reason"]}) in world["audit"].actions("cols")


def test_a_type_that_widens_is_changed_in_place_and_one_that_cannot_rebuilds_the_table(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA typ", "CREATE TABLE typ.t (id int PRIMARY KEY, n int, f real, code int)",
        "INSERT INTO typ.t VALUES (1, 1, 1.5, 100), (2, 2, 2.5, 200)")
    mirror(world, "typ")
    first_copy = state(m.get("typ"), "t")["copy_lsn"]
    sql(url, "ALTER TABLE typ.t ALTER COLUMN n TYPE bigint, ALTER COLUMN f TYPE double precision",
        f"INSERT INTO typ.t VALUES (3, {2 ** 40}, 0.1, 300)")
    m.run("typ")
    types = {f.name: str(f.field_type) for f in table(world, "m_typ.t").schema().fields}
    assert types["n"] == "long" and types["f"] == "double"
    assert [(r["n"], r["f"]) for r in rows(world, "m_typ.t")] == [(1, 1.5), (2, 2.5), (2 ** 40, 0.1)]
    assert state(m.get("typ"), "t")["copy_lsn"] == first_copy             # widened in place
    assert not [a for a, _ in world["audit"].actions("typ") if a == "table_rebuilt"]
    # int to text is no widening: the table is rebuilt with the new type, and says so.
    sql(url, "UPDATE typ.t SET code = 101 WHERE id = 1",
        "ALTER TABLE typ.t ALTER COLUMN code TYPE text USING 'c' || code", "INSERT INTO typ.t VALUES (4, 4, 4, 'c400')")
    m.run("typ")
    out = state(m.get("typ"), "t")
    assert out["status"] == "running" and "type of code changed (int to string)" in out["reason"]
    assert {f.name: str(f.field_type) for f in table(world, "m_typ.t").schema().fields}["code"] == "string"
    assert [(r["id"], r["code"]) for r in rows(world, "m_typ.t")] == [(1, "c101"), (2, "c200"), (3, "c300"), (4, "c400")]
    assert [d["reason"] for a, d in world["audit"].actions("typ") if a == "table_rebuilt"] == [out["reason"]]
    sql(url, "UPDATE typ.t SET code = 'c2' WHERE id = 2")                 # and it follows on from the rebuild
    assert m.run("typ") == 1 and rows(world, "m_typ.t")[1]["code"] == "c2"


def test_a_table_renamed_dropped_and_created_again(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA ren", "CREATE TABLE ren.t (id int PRIMARY KEY, v text)", "INSERT INTO ren.t VALUES (1, 'a')",
        "CREATE TABLE ren.gone (id int PRIMARY KEY, v text)", "INSERT INTO ren.gone VALUES (1, 'kept')")
    mirror(world, "ren")
    sql(url, "INSERT INTO ren.t VALUES (2, 'b')", "ALTER TABLE ren.t RENAME TO t_v2", "INSERT INTO ren.t_v2 VALUES (3, 'c')")
    m.run("ren")
    out = state(m.get("ren"), "t_v2")
    assert out["target"] == "m_ren.t" and out["status"] == "running"       # the lakehouse table is kept
    assert [r["v"] for r in rows(world, "m_ren.t")] == ["a", "b", "c"]
    assert ("table_renamed", {"from": "t", "to": "t_v2", "target": "m_ren.t"}) in world["audit"].actions("ren")
    sql(url, "INSERT INTO ren.gone VALUES (2, 'last words')", "DROP TABLE ren.gone")
    m.run("ren")
    out = state(m.get("ren"), "gone")
    assert out["status"] == "source_dropped"
    assert [r["v"] for r in rows(world, "m_ren.gone")] == ["kept", "last words"]   # kept, with its last changes
    assert set(published(world, "ren")) == {"t_v2"}
    with pytest.raises(Exception, match="source_dropped"):
        m.resync(OPS, "ren", "gone")
    sql(url, "CREATE TABLE ren.gone (id int PRIMARY KEY, v text)", "INSERT INTO ren.gone VALUES (9, 'again')")
    m.run("ren")
    assert state(m.get("ren"), "gone")["status"] == "running"
    assert [r["v"] for r in rows(world, "m_ren.gone")] == ["again"]        # the new table took its place


def test_truncate_empties_only_that_table(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA trunc", "CREATE TABLE trunc.carts (id int PRIMARY KEY)", "INSERT INTO trunc.carts VALUES (1), (2)",
        "CREATE TABLE trunc.users (id int PRIMARY KEY)", "INSERT INTO trunc.users VALUES (1), (2)")
    mirror(world, "trunc")
    sql(url, "INSERT INTO trunc.carts VALUES (3)", "INSERT INTO trunc.users VALUES (3)", "TRUNCATE trunc.carts",
        "INSERT INTO trunc.carts VALUES (9)")
    m.run("trunc")
    assert [r["id"] for r in rows(world, "m_trunc.carts")] == [9]
    assert [r["id"] for r in rows(world, "m_trunc.users")] == [1, 2, 3]


def versions(world, target, key):
    return sorted((r for r in table(world, target).scan().to_arrow().to_pylist() if r["id"] == key),
                  key=lambda r: r["_cairn_valid_from"])


def test_history_mode_keeps_every_version_with_its_interval(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA hist", "CREATE TABLE hist.prices (id int PRIMARY KEY, price numeric, note text)",
        "INSERT INTO hist.prices VALUES (1, 10, 'a'), (2, 20, 'b')",
        "CREATE TABLE hist.plain (id int PRIMARY KEY, v int)", "INSERT INTO hist.plain VALUES (1, 1)")
    before = sql(url, "SELECT clock_timestamp()")[0][0]
    out = mirror(world, "hist", tables={"prices": {"history": True}})
    assert state(out, "prices")["history"] and not state(out, "plain")["history"]
    assert "_cairn_current" not in rows(world, "m_hist.plain")[0]
    copied = rows(world, "m_hist.prices")
    assert all(r["_cairn_current"] and r["_cairn_valid_to"] is None and r["_cairn_valid_from"] >= before for r in copied)
    sql(url, "INSERT INTO hist.prices VALUES (3, 30, 'c')")
    sql(url, "UPDATE hist.prices SET price = 11 WHERE id = 1")
    sql(url, "UPDATE hist.prices SET price = 12 WHERE id = 1")
    sql(url, "UPDATE hist.prices SET note = note WHERE id = 3")            # changes nothing: no version
    sql(url, "DELETE FROM hist.prices WHERE id = 2")
    after = sql(url, "SELECT clock_timestamp()")[0][0]
    crash_before_advance(m, "hist")                                         # committed, the slot not advanced
    assert m.get("hist")["status"] == "failed"

    def check():
        one = versions(world, "m_hist.prices", 1)
        assert [(v["price"], v["_cairn_current"]) for v in one] == [(10.0, False), (11.0, False), (12.0, True)]
        assert one[0]["_cairn_valid_to"] == one[1]["_cairn_valid_from"]    # each ends where the next begins
        assert one[1]["_cairn_valid_to"] == one[2]["_cairn_valid_from"] and one[2]["_cairn_valid_to"] is None
        assert before <= one[0]["_cairn_valid_from"] < one[1]["_cairn_valid_from"] < one[2]["_cairn_valid_from"] <= after
        two = versions(world, "m_hist.prices", 2)
        assert len(two) == 1 and not two[0]["_cairn_current"] and two[0]["note"] == "b"   # deleted: closed, kept
        assert two[0]["_cairn_valid_from"] < two[0]["_cairn_valid_to"] <= after
        three = versions(world, "m_hist.prices", 3)
        assert len(three) == 1 and three[0]["_cairn_current"] and three[0]["_cairn_valid_to"] is None
        return rows(world, "m_hist.prices", ("id", "_cairn_valid_from"))
    first = check()
    assert m.run("hist") == 0 and m.get("hist")["status"] == "running"      # the replay: nothing again
    assert check() == first
    # Several changes of a row in one transaction are one version; a key that returns starts a new one.
    sql(url, "BEGIN", "UPDATE hist.prices SET price = 13 WHERE id = 1", "UPDATE hist.prices SET price = 14 WHERE id = 1",
        "INSERT INTO hist.prices VALUES (2, 21, 'back')", "COMMIT")
    m.run("hist")
    assert [v["price"] for v in versions(world, "m_hist.prices", 1)] == [10.0, 11.0, 12.0, 14.0]
    assert [(v["price"], v["_cairn_current"]) for v in versions(world, "m_hist.prices", 2)] == [(20.0, False), (21.0, True)]
    # A re-sync of a history table reconciles it with the source: its history stays.
    sql(url, "UPDATE hist.prices SET price = 15 WHERE id = 1")
    m.resync(OPS, "hist", "prices")
    assert [v["price"] for v in versions(world, "m_hist.prices", 1)] == [10.0, 11.0, 12.0, 14.0, 15.0]
    assert m.run("hist") == 0                                               # the update is in the copy: not again
    assert len(versions(world, "m_hist.prices", 1)) == 5
    # A TRUNCATE closes every current version.
    sql(url, "TRUNCATE hist.prices")
    m.run("hist")
    got = rows(world, "m_hist.prices", ("id", "_cairn_valid_from"))
    assert len(got) == 8 and not any(r["_cairn_current"] for r in got) and all(r["_cairn_valid_to"] for r in got)


def test_soft_deletes_keep_the_row_and_a_new_insert_clears_them(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA soft", "CREATE TABLE soft.users (id int PRIMARY KEY, name text, plan text)",
        "INSERT INTO soft.users VALUES (1, 'Ana', 'pro'), (2, 'Bob', 'free')")
    out = mirror(world, "soft", defaults={"soft_delete": True})
    assert state(out, "users")["soft_delete"]
    assert [(r["id"], r["_cairn_deleted"]) for r in rows(world, "m_soft.users")] == [(1, False), (2, False)]
    sql(url, "DELETE FROM soft.users WHERE id = 2", "INSERT INTO soft.users VALUES (3, 'Cal', 'pro')",
        "INSERT INTO soft.users VALUES (4, 'Dee', 'free')", "DELETE FROM soft.users WHERE id = 4")
    m.run("soft")
    got = {r["id"]: r for r in rows(world, "m_soft.users")}
    assert (got[2]["name"], got[2]["plan"], got[2]["_cairn_deleted"]) == ("Bob", "free", True)   # its columns intact
    assert isinstance(got[2]["_cairn_deleted_at"], dt.datetime)
    assert (got[4]["name"], got[4]["_cairn_deleted"]) == ("Dee", True)       # inserted and deleted in one tick
    assert not got[1]["_cairn_deleted"] and got[1]["_cairn_deleted_at"] is None and not got[3]["_cairn_deleted"]
    sql(url, "INSERT INTO soft.users VALUES (2, 'Bo', 'pro')")
    m.run("soft")
    got = {r["id"]: r for r in rows(world, "m_soft.users")}
    assert (got[2]["name"], got[2]["_cairn_deleted"], got[2]["_cairn_deleted_at"]) == ("Bo", False, None)
    # A re-sync keeps the deleted rows (the table is reconciled with the source, not emptied).
    m.resync(OPS, "soft", "users")
    got = {r["id"]: r for r in rows(world, "m_soft.users")}
    assert sorted(got) == [1, 2, 3, 4] and got[4]["_cairn_deleted"] and not got[1]["_cairn_deleted"]
    sql(url, "TRUNCATE soft.users")
    m.run("soft")
    assert [r["_cairn_deleted"] for r in rows(world, "m_soft.users")] == [True] * 4


def digest(value):
    return hmac.new(HASH_KEY, value.encode(), hashlib.sha256).hexdigest()


def test_columns_are_excluded_at_the_source_and_hashed_before_anything_is_stored(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA pii",
        "CREATE TABLE pii.people (id int PRIMARY KEY, email text, ssn text, name text, visits int)",
        "INSERT INTO pii.people VALUES (1, 'ana@example.com', '111-11-1111', 'Ana', 3), (2, NULL, '222-22-2222', 'Bob', 1)")
    out = mirror(world, "pii", tables={"people": {"columns": {"exclude": ["ssn"], "hash": ["email", "visits"]}}})
    assert state(out, "people")["excluded"] == ["ssn"] and state(out, "people")["hashed"] == ["email", "visits"]
    assert published(world, "pii") == {"people": ["id", "email", "name", "visits"]}   # ssn never leaves the source
    got = rows(world, "m_pii.people")
    assert "ssn" not in got[0]
    assert got[0]["email"] == digest("ana@example.com") and got[1]["email"] is None
    assert got[0]["visits"] == digest("3")                                   # any type: its text is hashed
    # The same value hashes the same in a change as in the copy, so joins on it still work.
    sql(url, "INSERT INTO pii.people VALUES (3, 'ana@example.com', '333-33-3333', 'Ana again', 3)",
        "UPDATE pii.people SET ssn = '999-99-9999', name = 'Robert' WHERE id = 2")
    m.run("pii")
    got = rows(world, "m_pii.people")
    assert got[2]["email"] == got[0]["email"] == digest("ana@example.com") and got[1]["name"] == "Robert"
    everything = repr(table(world, "m_pii.people").scan().to_arrow().to_pylist())
    assert "example.com" not in everything and "111-11" not in everything and "999-99" not in everything
    # A hashed column keeps being hashed under a new name; a new column joins the published list.
    sql(url, "ALTER TABLE pii.people RENAME COLUMN email TO mail", "ALTER TABLE pii.people ADD COLUMN phone text",
        "INSERT INTO pii.people VALUES (4, 'dee@example.com', '444-44-4444', 'Dee', 2, '555-0100')")
    m.run("pii")
    out = state(m.get("pii"), "people")
    assert out["status"] == "running" and out["hashed"] == ["mail", "visits"] and out["excluded"] == ["ssn"]
    assert published(world, "pii") == {"people": ["id", "mail", "name", "visits", "phone"]}
    got = rows(world, "m_pii.people")
    assert got[3]["mail"] == digest("dee@example.com") and got[3]["phone"] == "555-0100" and "ssn" not in got[3]
    assert got[0]["mail"] == digest("ana@example.com") and "email" not in got[0]
    assert "example.com" not in repr(table(world, "m_pii.people").scan().to_arrow().to_pylist())
    # Excluding a column later takes it out of the publication and out of the lakehouse table. The options still
    # name the hashed column as it was called: it stays hashed.
    m.update(OPS, "pii", {"tables": {"people": {"columns": {"exclude": ["ssn", "name"], "hash": ["email", "visits"]}}}})
    assert published(world, "pii") == {"people": ["id", "mail", "visits", "phone"]}
    got = rows(world, "m_pii.people")
    assert "name" not in got[0] and got[0]["mail"] == digest("ana@example.com")
    sql(url, "INSERT INTO pii.people VALUES (5, 'eve@example.com', '555-55-5555', 'Eve', 9, NULL)")
    m.run("pii")
    assert rows(world, "m_pii.people")[4]["mail"] == digest("eve@example.com")
    with world["engine"].connect() as c:                                     # the key is never stored with the mirror
        from sqlalchemy import text

        kept = repr(c.execute(text("SELECT * FROM cairn_database_mirrors")).all()) + \
            repr(c.execute(text("SELECT * FROM cairn_database_mirror_tables")).all())
    assert HASH_KEY.decode() not in kept


def test_a_resync_of_one_table_leaves_the_others_alone(world):
    from pyiceberg.expressions import EqualTo

    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA re", "CREATE TABLE re.a (id int PRIMARY KEY, v text)", "INSERT INTO re.a VALUES (1, 'a'), (2, 'b')",
        "CREATE TABLE re.b (id int PRIMARY KEY, v text)", "INSERT INTO re.b VALUES (1, 'x')")
    mirror(world, "re")
    table(world, "m_re.a").delete(EqualTo("id", 1))                         # the lakehouse table went wrong
    assert len(rows(world, "m_re.a")) == 1
    other = table(world, "m_re.b").current_snapshot().snapshot_id
    sql(url, "UPDATE re.a SET v = 'b2' WHERE id = 2")
    out = m.resync(OPS, "re", "a")
    assert state(out, "a")["status"] == "running"
    assert [(r["id"], r["v"]) for r in rows(world, "m_re.a")] == [(1, "a"), (2, "b2")]
    assert table(world, "m_re.b").current_snapshot().snapshot_id == other   # untouched
    assert m.run("re") == 0                                                 # the update was in the copy: not again
    sql(url, "UPDATE re.a SET v = 'a3' WHERE id = 1", "INSERT INTO re.b VALUES (2, 'y')")
    assert m.run("re") == 2 and rows(world, "m_re.a")[0]["v"] == "a3"
    with pytest.raises(Exception, match="no table"):
        m.resync(OPS, "re", "nope")
    assert ("table_resync", {"table": "a", "target": "m_re.a"}) in world["audit"].actions("re")


def test_a_crash_between_the_commits_and_the_slots_advance_writes_nothing_twice(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA crash", "CREATE TABLE crash.e1 (id bigint PRIMARY KEY, v int)",
        "CREATE TABLE crash.e2 (id bigint PRIMARY KEY, v int)")
    mirror(world, "crash")
    sql(url, "BEGIN", "INSERT INTO crash.e1 SELECT g, g FROM generate_series(1, 50) g",
        "INSERT INTO crash.e2 SELECT g, g FROM generate_series(1, 50) g", "COMMIT")
    crash_before_advance(m, "crash")
    assert m.get("crash")["status"] == "failed"
    assert len(rows(world, "m_crash.e1")) == 50 and len(rows(world, "m_crash.e2")) == 50      # committed
    snaps = {t: len(table(world, f"m_crash.{t}").snapshots()) for t in ("e1", "e2")}
    sql(url, "UPDATE crash.e1 SET v = -1 WHERE id = 7")
    assert m.run("crash") == 1                                              # replays the 100, writes the update only
    got = rows(world, "m_crash.e1")
    assert len(got) == 50 and got[6]["v"] == -1 and len(rows(world, "m_crash.e2")) == 50
    assert len(table(world, "m_crash.e2").snapshots()) == snaps["e2"]       # the replay wrote nothing to it
    assert m.get("crash")["status"] == "running"


def test_one_table_failing_loses_and_duplicates_nothing_in_the_others(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA part", "CREATE TABLE part.good (id int PRIMARY KEY, v int)",
        "CREATE TABLE part.bad (id int PRIMARY KEY, v int)", "INSERT INTO part.bad VALUES (1, 1)")
    mirror(world, "part")
    world["streams"]._catalog().drop_table(("m_part", "bad"))               # its lakehouse table is gone
    sql(url, "BEGIN", "INSERT INTO part.good VALUES (1, 1)", "INSERT INTO part.bad VALUES (2, 2)", "COMMIT")
    assert m.run("part") == 1
    out = m.get("part")
    assert out["status"] == "degraded" and out["holding"] == ["bad"]
    assert state(out, "bad")["status"] == "failed" and "re-sync" in state(out, "bad")["error"]
    assert state(out, "good")["status"] == "running" and [r["id"] for r in rows(world, "m_part.good")] == [1]
    held, stuck_at = out["retained_bytes"], out["last_lsn"]
    assert held > 0                                                         # the slot waits for the failing table
    snaps = len(table(world, "m_part.good").snapshots())
    sql(url, "INSERT INTO part.good VALUES (2, 2)", "INSERT INTO part.bad VALUES (3, 3)")
    assert m.run("part") == 1                                               # good: its replay skipped, the new row in
    assert [r["id"] for r in rows(world, "m_part.good")] == [1, 2]
    assert len(table(world, "m_part.good").snapshots()) == snaps + 1
    out = m.get("part")
    assert out["retained_bytes"] > held and out["last_lsn"] == stuck_at     # and it holds more WAL each time
    out = m.resync(OPS, "part", "bad")                                      # copied again: nothing of it was lost
    assert state(out, "bad")["status"] == "running" and out["holding"] == [] and out["status"] == "running"
    assert [r["id"] for r in rows(world, "m_part.bad")] == [1, 2, 3]
    assert out["last_lsn"] != stuck_at                                      # and the slot moved on
    sql(url, "UPDATE part.bad SET v = 30 WHERE id = 3")
    assert m.run("part") == 1 and rows(world, "m_part.bad")[2]["v"] == 30
    assert [r["id"] for r in rows(world, "m_part.good")] == [1, 2]


def test_pause_keeps_the_slot_and_delete_drops_slot_and_publication(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA bye", "CREATE TABLE bye.t (id int PRIMARY KEY)", "INSERT INTO bye.t VALUES (1)")
    mirror(world, "bye")

    def left():
        with world["engine"].connect() as c:
            from sqlalchemy import text

            mid = c.execute(text("SELECT id FROM cairn_database_mirrors WHERE name = 'bye'")).scalar()
        return mid, sql(url, f"SELECT (SELECT count(*) FROM pg_replication_slots WHERE slot_name = 'cairn_dbmirror_{mid}'), "
                             f"(SELECT count(*) FROM pg_publication WHERE pubname = 'cairn_dbmirror_{mid}')")[0]
    mid, there = left()
    assert there == (1, 1)
    m.set_enabled(OPS, "bye", False)
    sql(url, "INSERT INTO bye.t VALUES (2)")
    assert m.run("bye") == 0 and m.get("bye")["status"] == "paused" and m.get("bye")["retained_bytes"] > 0
    m.set_enabled(OPS, "bye", True)
    assert m.run("bye") == 1
    m.delete(OPS, "bye")
    assert sql(url, f"SELECT (SELECT count(*) FROM pg_replication_slots WHERE slot_name = 'cairn_dbmirror_{mid}'), "
                    f"(SELECT count(*) FROM pg_publication WHERE pubname = 'cairn_dbmirror_{mid}')")[0] == (0, 0)
    assert [r["id"] for r in rows(world, "m_bye.t")] == [1, 2]              # the lakehouse table is kept
    with pytest.raises(Exception, match="no such"):
        m.get("bye")
    assert [a for a, _ in world["audit"].actions("bye")][-3:] == ["paused", "resumed", "deleted"]


def test_what_cannot_be_mirrored_is_refused(world):
    from cairn.pipelines.mirror import MirrorError
    from cairn.pipelines.mirror_db import DatabaseMirrors

    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA no", "CREATE TABLE no.t (id int PRIMARY KEY, secret text, v text)")
    ok = {"name": "nope", "connection": "crm", "schema": "no", "target_namespace": "m_no"}
    before = sql(url, "SELECT (SELECT count(*) FROM pg_replication_slots), (SELECT count(*) FROM pg_publication)")
    for change, why in [({"name": "Bad"}, "name"), ({"schema": "no; DROP"}, "schema"),
                        ({"target_namespace": "Bad.Name"}, "target_namespace"),
                        ({"connection": "nowhere"}, "no connection"), ({"connection": "lakehouse"}, "not a PostgreSQL"),
                        ({"connection": "plain"}, "wal_level"), ({"schema": "missing"}, "no schema"),
                        ({"include": "everything"}, "lists"),
                        ({"tables": {"t": {"history": True, "soft_delete": True}}}, "history or soft_delete"),
                        ({"tables": {"t": {"columns": {"exclude": ["id"]}}}}, "primary key and cannot be excluded"),
                        ({"tables": {"t": {"columns": {"hash": ["id"]}}}}, "primary key and cannot be hashed"),
                        ({"tables": {"t": {"columns": {"exclude": ["v"], "hash": ["v"]}}}}, "both"),
                        ({"tables": {"t": {"columns": {"exclude": ["sceret"]}}}}, "no column sceret"),
                        ({"tables": {"t": {"colums": {}}}}, "takes")]:
        with pytest.raises(MirrorError, match=why):
            m.create(OPS, {**ok, **change})
    # No hashing key: a mirror that hashes columns is refused, and one whose key went away stops storing them.
    keyless = DatabaseMirrors(world["engine"], world["streams"], world["urls"].get, hash_key=lambda: None)
    with pytest.raises(MirrorError, match="no hashing key"):
        keyless.create(OPS, {**ok, "tables": {"t": {"columns": {"hash": ["secret"]}}}})
    with pytest.raises(MirrorError, match="no hashing key"):
        keyless.create(OPS, {**ok, "tables": {"later": {"columns": {"hash": ["secret"]}}}})
    assert sql(url, "SELECT (SELECT count(*) FROM pg_replication_slots), (SELECT count(*) FROM pg_publication)") == before
    m.create(OPS, {**ok, "tables": {"t": {"columns": {"hash": ["secret"]}}}})
    sql(url, "INSERT INTO no.t VALUES (1, 'hunter2', 'v')")
    keyless.run("nope")
    out = keyless.get("nope")
    assert state(out, "t")["status"] == "skipped" and "no hashing key" in state(out, "t")["reason"]
    assert rows(world, "m_no.t") == []                                      # nothing of it was stored
    m.run("nope")
    assert rows(world, "m_no.t")[0]["secret"] == digest("hunter2")
    # Options cannot move a mirror, and a column list needs PostgreSQL 15.
    with pytest.raises(MirrorError, match="cannot be changed"):
        m.update(OPS, "nope", {"schema": "public"})
    with pytest.raises(MirrorError, match="primary key and cannot be excluded"):
        m.update(OPS, "nope", {"tables": {"t": {"columns": {"exclude": ["id"]}}}})
    sql(world["old_url"], "CREATE SCHEMA o", "CREATE TABLE o.t (id int PRIMARY KEY, secret text)",
        "INSERT INTO o.t VALUES (1, 's')")
    old = {"name": "old", "connection": "old", "schema": "o", "target_namespace": "m_old"}
    with pytest.raises(MirrorError, match="PostgreSQL 15"):
        m.create(OPS, {**old, "tables": {"t": {"columns": {"exclude": ["secret"]}}}})
    out = m.create(OPS, old)                                                # without a column list an older server is fine
    assert state(out, "t")["status"] == "running" and rows(world, "m_old.t")[0]["secret"] == "s"
    sql(world["old_url"], "UPDATE o.t SET secret = 's2'")
    assert m.run("old") == 1 and rows(world, "m_old.t")[0]["secret"] == "s2"
    m.delete(OPS, "old")


def test_the_api_is_mounted_for_people_who_manage_ingestion(world):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from cairn.pipelines.mirror_db import build_database_mirrors_router

    class P:
        def __init__(self, urn):
            self.urn = urn

    def authorize(p, privilege, resource):
        if privilege != "MANAGE_INGESTION" or p.urn != OPS:
            raise HTTPException(403, "not allowed")
    who = {"urn": OPS}
    app = FastAPI()
    app.include_router(build_database_mirrors_router(world["m"], lambda: P(who["urn"]), authorize))
    http = TestClient(app)
    sql(world["url"], "CREATE SCHEMA api", "CREATE TABLE api.t (id int PRIMARY KEY, v text)", "INSERT INTO api.t VALUES (1, 'a')")
    made = http.post("/v1/database-mirrors", json={"name": "api", "connection": "crm", "schema": "api",
                                                   "target_namespace": "m_api"})
    assert made.status_code == 201 and made.json()["mirrored"][0]["status"] == "running", made.text
    assert "api" in [x["name"] for x in http.get("/v1/database-mirrors").json()["database_mirrors"]]
    assert http.get("/v1/database-mirrors/api").json()["include"] == ["*"]
    assert http.put("/v1/database-mirrors/api", json={"defaults": {"soft_delete": True}}).json()["defaults"] == \
        {"soft_delete": True}
    assert "_cairn_deleted" in rows(world, "m_api.t")[0]
    sql(world["url"], "INSERT INTO api.t VALUES (2, 'b')")
    assert http.post("/v1/database-mirrors/api/sync").json() == {"written": 1}
    assert http.post("/v1/database-mirrors/api/tables/t/resync").status_code == 200
    assert http.post("/v1/database-mirrors/api/tables/nope/resync").status_code == 404
    assert http.post("/v1/database-mirrors/api/pause").json()["status"] == "paused"
    assert http.post("/v1/database-mirrors/api/resume").json()["status"] == "running"
    assert http.post("/v1/database-mirrors", json={"name": "api", "connection": "crm", "schema": "api",
                                                   "target_namespace": "m_api"}).status_code == 409
    who["urn"] = "urn:li:corpuser:bob"
    for call in (lambda: http.get("/v1/database-mirrors"), lambda: http.get("/v1/database-mirrors/api"),
                 lambda: http.post("/v1/database-mirrors", json={}), lambda: http.put("/v1/database-mirrors/api", json={}),
                 lambda: http.post("/v1/database-mirrors/api/sync"), lambda: http.post("/v1/database-mirrors/api/pause"),
                 lambda: http.post("/v1/database-mirrors/api/tables/t/resync"),
                 lambda: http.delete("/v1/database-mirrors/api")):
        assert call().status_code == 403
    who["urn"] = OPS
    assert http.delete("/v1/database-mirrors/api").status_code == 204
    assert http.get("/v1/database-mirrors/api").status_code == 404


def test_writers_are_not_held_up_by_a_copy(world):
    """The copy's lock is let go as soon as its snapshot is taken: a writer gets in while rows are still being read."""
    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA busy", "CREATE TABLE busy.t (id int PRIMARY KEY, v int)",
        "INSERT INTO busy.t SELECT g, g FROM generate_series(1, 20000) g")
    wrote = {}
    real = m._write

    def write(t, o, ops, topic=None):                                       # the copy is under way: write at the source
        if "took" not in wrote:
            started = time.time()
            sql(url, "SET lock_timeout = '5s'", "UPDATE busy.t SET v = -1 WHERE id = 20000",
                "INSERT INTO busy.t VALUES (20001, 1)")
            wrote["took"] = time.time() - started
        if topic:                                                           # the copy holds the snapshot's rows only
            wrote["copied"] = wrote.get("copied", 0) + len(ops)
            assert all(op[3]["v"] > 0 for op in ops)
        return real(t, o, ops, topic)
    m._write = write
    try:
        t = threading.Thread(target=lambda: wrote.update(out=mirror(world, "busy")))
        t.start()
        t.join(180)
    finally:
        m._write = real
    assert wrote["took"] < 5 and wrote["copied"] == 20000 and wrote["out"]["status"] == "running"
    got = rows(world, "m_busy.t")                                           # what came after the copy followed it
    assert len(got) == 20001 and got[19999]["v"] == -1
    assert m.run("busy") == 0


def source(world, query):
    return [tuple(r) for r in sql(world["url"], query)]


def test_two_processes_never_run_one_mirror_at_once(world):
    """The API and the scheduler are two processes: each its own DatabaseMirrors, as here."""
    from cairn.pipelines.mirror_db import DatabaseMirrors

    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA twin", "CREATE TABLE twin.t (id int PRIMARY KEY, v int)",
        "INSERT INTO twin.t SELECT g, g FROM generate_series(1, 2000) g")
    mirror(world, "twin")
    other = DatabaseMirrors(world["engine"], world["streams"], world["urls"].get, world["audit"], hash_key=lambda: HASH_KEY)
    copies = {"first": 0, "other": 0}
    under_way = threading.Event()
    real, real_other = m._copy, other._copy

    def slow(*a):
        copies["first"] += 1
        under_way.set()
        time.sleep(3)                                                       # the other process comes by meanwhile
        return real(*a)

    def counted(*a):
        copies["other"] += 1
        return real_other(*a)
    m._copy, other._copy = slow, counted
    try:
        t = threading.Thread(target=lambda: m.resync(OPS, "twin", "t"))
        t.start()
        assert under_way.wait(60)
        sql(url, "DELETE FROM twin.t WHERE id <= 1000", "UPDATE twin.t SET v = -v WHERE id > 1900")
        assert other.run("twin") == 0                                       # it is being run: this one does nothing
        assert other.get("twin")["status"] != "failed"
        t.join(180)
    finally:
        m._copy, other._copy = real, real_other
    assert copies == {"first": 1, "other": 0}
    other.run("twin")                                                       # and once it is free, either may run it
    sql(url, "INSERT INTO twin.t VALUES (5000, 5)")
    assert other.run("twin") == 1 and m.run("twin") == 0
    assert [(r["id"], r["v"]) for r in rows(world, "m_twin.t")] == source(world, "SELECT id, v FROM twin.t ORDER BY id")


def test_a_key_change_keeps_what_the_update_did_not_touch_in_every_mode(world):
    m, url = world["m"], world["url"]
    big = "y" * 9000
    statements = ["CREATE SCHEMA pk"]
    for t in ("plain", "soft", "hist"):
        statements += [f"CREATE TABLE pk.{t} (id int PRIMARY KEY, body text, n int)",
                       f"ALTER TABLE pk.{t} ALTER COLUMN body SET STORAGE EXTERNAL",
                       f"INSERT INTO pk.{t} VALUES (1, '{big}', 1), (5, 'small', 5)"]
    sql(url, *statements)
    mirror(world, "pk", tables={"soft": {"soft_delete": True}, "hist": {"history": True}})
    for t in ("plain", "soft", "hist"):
        sql(url, f"UPDATE pk.{t} SET id = 2 WHERE id = 1",                 # the TOASTed body is not in the change
            f"UPDATE pk.{t} SET id = 6, n = 6 WHERE id = 5")
    m.run("pk")
    assert [(r["id"], r["body"], r["n"]) for r in rows(world, "m_pk.plain")] == [(2, big, 1), (6, "small", 6)]
    soft = {r["id"]: r for r in rows(world, "m_pk.soft")}
    assert sorted(soft) == [1, 2, 5, 6]
    assert soft[1]["_cairn_deleted"] and soft[1]["body"] == big and soft[5]["_cairn_deleted"]
    assert (soft[2]["body"], soft[2]["n"], soft[2]["_cairn_deleted"]) == (big, 1, False)
    assert (soft[6]["body"], soft[6]["n"], soft[6]["_cairn_deleted"]) == ("small", 6, False)
    hist = {r["id"]: r for r in rows(world, "m_pk.hist")}
    assert sorted(hist) == [1, 2, 5, 6]
    assert not hist[1]["_cairn_current"] and hist[1]["body"] == big and not hist[5]["_cairn_current"]
    assert hist[2]["_cairn_current"] and hist[2]["body"] == big and hist[2]["_cairn_valid_from"] == hist[1]["_cairn_valid_to"]
    assert hist[6]["_cairn_current"] and (hist[6]["body"], hist[6]["n"]) == ("small", 6)
    # And a key change together with a change of the large value, and back again.
    for t in ("plain", "soft", "hist"):
        sql(url, f"UPDATE pk.{t} SET id = 1, body = 'short now' WHERE id = 2", f"UPDATE pk.{t} SET id = 3 WHERE id = 1")
    m.run("pk")
    assert [(r["id"], r["body"]) for r in rows(world, "m_pk.plain")] == [(3, "short now"), (6, "small")]
    soft = {r["id"]: r for r in rows(world, "m_pk.soft")}
    assert soft[3]["body"] == "short now" and not soft[3]["_cairn_deleted"] and soft[1]["_cairn_deleted"] and soft[2]["_cairn_deleted"]
    current = [r for r in rows(world, "m_pk.hist", ("id", "_cairn_valid_from")) if r["_cairn_current"]]
    assert [(r["id"], r["body"]) for r in current] == [(3, "short now"), (6, "small")]


def test_a_column_added_just_before_a_copy_is_in_the_copy(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA late", "CREATE TABLE late.t (id int PRIMARY KEY, v text)", "INSERT INTO late.t VALUES (1, 'a'), (2, 'b')")
    real = m._copy

    def after_the_tick_looked(*a):                                          # the columns were read; now they change
        sql(url, "ALTER TABLE late.t ADD COLUMN extra text", "UPDATE late.t SET extra = 'filled'")
        return real(*a)
    m._copy = after_the_tick_looked
    try:
        out = mirror(world, "late")
    finally:
        m._copy = real
    assert state(out, "t")["status"] == "running"
    assert [(r["id"], r["extra"]) for r in rows(world, "m_late.t")] == [(1, "filled"), (2, "filled")]
    copied_at = state(out, "t")["copy_lsn"]
    sql(url, "INSERT INTO late.t VALUES (3, 'c', 'new')")
    assert m.run("late") == 1 and state(m.get("late"), "t")["copy_lsn"] == copied_at     # known already: no rebuild
    assert rows(world, "m_late.t")[2]["extra"] == "new"


def test_a_lakehouse_table_that_is_not_the_mirrors_is_never_touched(world):
    import pyarrow as pa

    from cairn.pipelines.mirror import MirrorError, Mirrors

    m, url = world["m"], world["url"]
    cat = world["streams"]._catalog()
    cat.create_namespace("m_own")
    theirs = cat.create_table(("m_own", "orders"), schema=pa.schema([pa.field("id", pa.int64()), pa.field("note", pa.string())]))
    theirs.append(pa.Table.from_pylist([{"id": 1, "note": "someone else's"}], schema=theirs.schema().as_arrow()))
    sql(url, "CREATE SCHEMA own", "CREATE TABLE own.orders (id int PRIMARY KEY, total int)", "INSERT INTO own.orders VALUES (7, 70)",
        "CREATE TABLE own.items (id int PRIMARY KEY)", "INSERT INTO own.items VALUES (1)")
    out = mirror(world, "own")
    assert state(out, "items")["status"] == "running"                       # the others carry on
    assert state(out, "orders")["status"] == "skipped"
    assert state(out, "orders")["reason"] == "m_own.orders exists and is not this mirror's: choose another namespace or remove it"
    sql(url, "INSERT INTO own.orders VALUES (8, 80)", "TRUNCATE own.orders")
    m.run("own")
    with pytest.raises(MirrorError, match="skipped"):
        m.resync(OPS, "own", "orders")
    kept = table(world, "m_own.orders")
    assert kept.scan().to_arrow().to_pylist() == [{"id": 1, "note": "someone else's"}]
    assert [f.name for f in kept.schema().fields] == ["id", "note"] and len(kept.snapshots()) == 1
    cat.drop_table(("m_own", "orders"))                                     # removed: the mirror may have the name
    sql(url, "INSERT INTO own.orders VALUES (9, 90)")
    m.run("own")
    assert state(m.get("own"), "orders")["status"] == "running"
    assert [(r["id"], r["total"]) for r in rows(world, "m_own.orders")] == [(9, 90)]
    assert table(world, "m_own.orders").properties["cairn.database-mirror.table"] == "orders"

    # Two mirrors into one namespace: never the same table.
    sql(url, "CREATE SCHEMA own2", "CREATE TABLE own2.items (id int PRIMARY KEY)", "INSERT INTO own2.items VALUES (100)",
        "CREATE TABLE own2.extra (id int PRIMARY KEY)", "INSERT INTO own2.extra VALUES (1)")
    second = {"name": "own2", "connection": "crm", "schema": "own2", "target_namespace": "m_own"}
    with pytest.raises(MirrorError, match="m_own.items is already written by database mirror own"):
        m.create(OPS, second)
    out = m.create(OPS, {**second, "exclude": ["items"]})
    assert state(out, "extra")["status"] == "running"
    with pytest.raises(MirrorError, match="already written by database mirror own"):
        m.update(OPS, "own2", {"exclude": []})
    sql(url, "CREATE TABLE own2.orders (id int PRIMARY KEY, total int)", "INSERT INTO own2.orders VALUES (1, 1)")
    m.run("own2")
    assert state(m.get("own2"), "orders")["reason"] == "m_own.orders is already written by database mirror own"
    assert [(r["id"], r["total"]) for r in rows(world, "m_own.orders")] == [(9, 90)]
    assert [r["id"] for r in rows(world, "m_own.items")] == [1]
    # Nor a table a single-table mirror writes.
    single = Mirrors(world["engine"], world["streams"], world["urls"].get)
    sql(url, "CREATE TABLE public.solo (id int PRIMARY KEY)", "CREATE TABLE own.solo (id int PRIMARY KEY)")
    m.run("own")
    assert state(m.get("own"), "solo")["status"] == "running"
    sql(url, "CREATE TABLE own.solo2 (id int PRIMARY KEY)", "CREATE TABLE public.solo2 (id int PRIMARY KEY)")
    single.create(OPS, {"name": "solo2", "connection": "crm", "table": "public.solo2", "target": "m_own.solo2"})
    m.run("own")
    assert state(m.get("own"), "solo2")["reason"] == "m_own.solo2 is already written by mirror solo2"
    single.delete(OPS, "solo2")


def test_a_mirror_that_cannot_be_recorded_leaves_nothing_on_the_source(world):
    from cairn.pipelines.mirror import Mirrors
    from cairn.pipelines.mirror_db import DatabaseMirrors

    url = world["url"]
    sql(url, "CREATE SCHEMA orphan", "CREATE TABLE orphan.t (id int PRIMARY KEY)")

    def left():
        return sql(url, "SELECT (SELECT count(*) FROM pg_replication_slots WHERE slot_name LIKE 'cairn%'), "
                        "(SELECT count(*) FROM pg_publication WHERE pubname LIKE 'cairn%')")[0]

    def no_database():                                                      # the insert of the mirror's row fails
        raise RuntimeError("the metadata database went away")
    before = left()
    whole = DatabaseMirrors(world["engine"], world["streams"], world["urls"].get, clock=no_database)
    with pytest.raises(RuntimeError, match="went away"):
        whole.create(OPS, {"name": "orphan", "connection": "crm", "schema": "orphan", "target_namespace": "m_orphan"})
    assert left() == before
    single = Mirrors(world["engine"], world["streams"], world["urls"].get, clock=no_database)
    with pytest.raises(RuntimeError, match="went away"):
        single.create(OPS, {"name": "orphan1", "connection": "crm", "table": "orphan.t", "target": "m_orphan.t"})
    assert left() == before
    with pytest.raises(Exception, match="no such"):
        world["m"].get("orphan")


def test_a_column_dropped_and_added_again_is_another_column(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA again", "CREATE TABLE again.t (id int PRIMARY KEY, x int, y text)",
        "INSERT INTO again.t VALUES (1, 10, 'a'), (2, 20, 'b')")
    mirror(world, "again")
    sql(url, "ALTER TABLE again.t DROP COLUMN x", "ALTER TABLE again.t ADD COLUMN x int", "UPDATE again.t SET x = 99 WHERE id = 2")
    m.run("again")
    out = state(m.get("again"), "t")
    assert out["status"] == "running" and "dropped and added again" in out["reason"]
    assert [(r["id"], r["x"]) for r in rows(world, "m_again.t")] == [(1, None), (2, 99)]   # not the old column's 10
    # Dropped in one tick and added in a later one: the same.
    sql(url, "ALTER TABLE again.t DROP COLUMN y")
    m.run("again")
    assert [r["y"] for r in rows(world, "m_again.t")] == ["a", "b"]         # kept, as a dropped column is
    sql(url, "ALTER TABLE again.t ADD COLUMN y text", "UPDATE again.t SET y = 'new' WHERE id = 1")
    m.run("again")
    assert [r["y"] for r in rows(world, "m_again.t")] == ["new", None]


def test_changes_are_written_in_batches_and_a_table_that_keeps_failing_is_set_aside(world, monkeypatch):
    import cairn.pipelines.mirror_db as module

    m, url = world["m"], world["url"]
    monkeypatch.setattr(module, "RUN_CHANGES", 50)                          # a run reads about this many changes
    monkeypatch.setattr(module, "BATCH_CHANGES", 5)                         # and commits every so many
    sql(url, "CREATE SCHEMA stall", "CREATE TABLE stall.good (id int PRIMARY KEY, v int)",
        "CREATE TABLE stall.bad (id int PRIMARY KEY, v int)", "INSERT INTO stall.bad VALUES (1, 1)")
    mirror(world, "stall")
    sql(url, *[f"INSERT INTO stall.good VALUES ({i}, {i})" for i in range(1, 61)])   # 60 transactions
    snaps = len(table(world, "m_stall.good").snapshots())
    first = m.run("stall")
    assert 0 < first < 60                                                   # one run does not take them all
    assert len(table(world, "m_stall.good").snapshots()) >= snaps + 2       # and it committed more than once
    while m.run("stall"):
        pass
    assert [r["id"] for r in rows(world, "m_stall.good")] == list(range(1, 61))

    world["streams"]._catalog().drop_table(("m_stall", "bad"))              # bad now fails on every change
    sql(url, "INSERT INTO stall.bad VALUES (2, 2)", "INSERT INTO stall.good VALUES (61, 61)")
    m.run("stall")
    out = m.get("stall")
    assert out["holding"] == ["bad"] and out["status"] == "degraded"
    stuck_at = out["last_lsn"]
    m.run("stall")
    assert m.get("stall")["last_lsn"] == stuck_at                           # a short backlog: Cairn waits for it
    sql(url, "INSERT INTO stall.good SELECT g, g FROM generate_series(62, 200) g", "INSERT INTO stall.bad VALUES (3, 3)")
    m.run("stall")                                                          # more than a run's worth behind it now
    out = m.get("stall")
    assert state(out, "bad")["status"] == "needs_resync" and "re-sync" in state(out, "bad")["reason"]
    assert out["holding"] == [] and out["status"] == "degraded" and out["last_lsn"] != stuck_at   # the slot moved on
    assert ("table_set_aside", {"table": "bad", "target": "m_stall.bad"}) in world["audit"].actions("stall")
    sql(url, "INSERT INTO stall.good VALUES (201, 201)", "INSERT INTO stall.bad VALUES (4, 4)")
    while m.run("stall"):
        pass
    assert [r["id"] for r in rows(world, "m_stall.good")] == list(range(1, 202))    # nothing lost, nothing twice
    assert state(m.get("stall"), "bad")["status"] == "needs_resync"         # not copied behind the operator's back
    out = m.resync(OPS, "stall", "bad")
    assert state(out, "bad")["status"] == "running" and out["status"] == "running"
    assert [r["id"] for r in rows(world, "m_stall.bad")] == [1, 2, 3, 4]


def test_values_read_the_same_from_the_copy_and_from_changes_whatever_the_databases_settings(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE SCHEMA txt",
        "CREATE TABLE txt.codes (id int PRIMARY KEY, code char(8), tag char(4), d date, seen timestamptz, until timestamp, "
        "flag boolean, raw bytea, f real)",
        "INSERT INTO txt.codes VALUES (1, 'ab', 'x', '2026-03-04', '2026-03-04 05:06:07+00', 'infinity', true, '\\x01ff', 0.1)",
        "ALTER DATABASE postgres SET datestyle = 'German, DMY'", "ALTER DATABASE postgres SET extra_float_digits = 0",
        "ALTER DATABASE postgres SET bytea_output = 'escape'")
    try:
        assert sql(url, "SELECT d::text FROM txt.codes")[0][0] == "04.03.2026"    # what a plain session reads now
        out = mirror(world, "txt", tables={"codes": {"history": True, "columns": {"hash": ["code"]}}})
        assert state(out, "codes")["status"] == "running", out
        one = rows(world, "m_txt.codes")[0]
        assert one["code"] == digest("ab") and one["tag"] == "x"            # char(n): without its padding
        assert one["d"] == dt.date(2026, 3, 4) and one["seen"] == dt.datetime(2026, 3, 4, 5, 6, 7, tzinfo=dt.timezone.utc)
        assert one["until"] == dt.datetime.max and one["flag"] is True and one["raw"] == "\\x01ff"
        assert abs(one["f"] - 0.1) < 1e-6
        # An update that changes nothing reads the same from replication as the copy read: no new version.
        sql(url, "UPDATE txt.codes SET code = code, tag = tag, d = d, seen = seen, until = until, raw = raw, f = f",
            "INSERT INTO txt.codes VALUES (2, 'ab', 'x', '2026-12-11', '2026-12-11 01:02:03+00', '-infinity', false, '\\x00', 2.5)")
        assert m.run("txt") == 1
        got = rows(world, "m_txt.codes", ("id", "_cairn_valid_from"))
        assert [r["id"] for r in got] == [1, 2]
        assert got[1]["code"] == got[0]["code"] == digest("ab") and got[1]["tag"] == "x"
        assert got[1]["d"] == dt.date(2026, 12, 11) and got[1]["until"] == dt.datetime.min and got[1]["raw"] == "\\x00"
        # A value the lakehouse type cannot hold fails the table, by name; it is never stored as null.
        sql(url, "INSERT INTO txt.codes (id, d) VALUES (3, '0044-03-15 BC')")
        m.run("txt")
        failed = state(m.get("txt"), "codes")
        assert failed["status"] == "failed" and "a value of d cannot be held as date" in failed["error"]
        assert [r["id"] for r in rows(world, "m_txt.codes", ("id", "_cairn_valid_from"))] == [1, 2]
        sql(url, "DELETE FROM txt.codes WHERE id = 3")
        out = m.resync(OPS, "txt", "codes")
        assert state(out, "codes")["status"] == "running" and out["holding"] == []
    finally:
        sql(url, "ALTER DATABASE postgres RESET datestyle", "ALTER DATABASE postgres RESET extra_float_digits",
            "ALTER DATABASE postgres RESET bytea_output")


def test_a_replica_identity_that_sends_no_primary_key_is_skipped(world):
    m, url = world["m"], world["url"]
    big = "z" * 9000
    sql(url, "CREATE SCHEMA ri",
        "CREATE TABLE ri.byindex (id int PRIMARY KEY, code text NOT NULL UNIQUE, v int)",
        "ALTER TABLE ri.byindex REPLICA IDENTITY USING INDEX byindex_code_key", "INSERT INTO ri.byindex VALUES (1, 'a', 1)",
        "CREATE TABLE ri.nothing (id int PRIMARY KEY, v int)", "ALTER TABLE ri.nothing REPLICA IDENTITY NOTHING",
        "CREATE TABLE ri.whole (id int PRIMARY KEY, body text, v int)", "ALTER TABLE ri.whole REPLICA IDENTITY FULL",
        "ALTER TABLE ri.whole ALTER COLUMN body SET STORAGE EXTERNAL",
        f"INSERT INTO ri.whole VALUES (1, '{big}', 1), (2, 'b', 2), (3, 'c', 3)",
        "CREATE TABLE ri.bypk (id int PRIMARY KEY, v int)", "ALTER TABLE ri.bypk REPLICA IDENTITY USING INDEX bypk_pkey",
        "INSERT INTO ri.bypk VALUES (1, 1), (2, 2)")
    out = mirror(world, "ri")
    assert state(out, "byindex")["status"] == "skipped" and "replica identity is an index" in state(out, "byindex")["reason"]
    assert state(out, "nothing")["status"] == "skipped" and "replica identity is NOTHING" in state(out, "nothing")["reason"]
    assert state(out, "whole")["status"] == "running" and state(out, "bypk")["status"] == "running"
    assert set(published(world, "ri")) == {"whole", "bypk"}
    sql(url, "UPDATE ri.whole SET v = 10 WHERE id = 1", "UPDATE ri.whole SET id = 20 WHERE id = 2", "DELETE FROM ri.whole WHERE id = 3",
        "UPDATE ri.whole SET id = 11 WHERE id = 1",
        "UPDATE ri.bypk SET v = 10 WHERE id = 1", "DELETE FROM ri.bypk WHERE id = 2", "DELETE FROM ri.byindex WHERE id = 1")
    m.run("ri")
    assert m.get("ri")["status"] == "running" and m.get("ri")["holding"] == []
    assert [(r["id"], r["body"], r["v"]) for r in rows(world, "m_ri.whole")] == [(11, big, 10), (20, "b", 2)]
    assert [(r["id"], r["v"]) for r in rows(world, "m_ri.bypk")] == [(1, 10)]
    sql(url, "ALTER TABLE ri.byindex REPLICA IDENTITY DEFAULT", "INSERT INTO ri.byindex VALUES (2, 'b', 2)")
    m.run("ri")
    assert state(m.get("ri"), "byindex")["status"] == "running"
    sql(url, "DELETE FROM ri.byindex WHERE id = 2", "INSERT INTO ri.byindex VALUES (3, 'c', 3)")
    assert m.run("ri") == 2 and [r["id"] for r in rows(world, "m_ri.byindex")] == [3]


def test_errors_never_show_the_connections_url_or_password(world):
    from urllib.parse import quote

    from cairn.pipelines.mirror import MirrorError, Mirrors, safe_error

    m, url = world["m"], world["url"]
    password = "p@ss:w0rd/x! é"
    away = f"postgresql://ops:{quote(password, safe='')}@127.0.0.1:1/crm"
    said = safe_error(RuntimeError(f'invalid dsn "{away}": missing "=" after "{password}"; user ops:{password}'), away)
    assert "w0rd" not in said and "p%40ss" not in said and "***" in said
    hidden = ("w0rd", "p@ss", "p%40ss", "ops:", "postgresql://")

    def clean(text):
        assert text and not any(h in text for h in hidden), text
        return text
    world["urls"]["away"] = away
    world["urls"]["garbled"] = f"postgresql://ops:{password}@[::1/crm"      # not a URL libpq can read
    body = {"name": "scrub", "schema": "scrub", "target_namespace": "m_scrub"}
    with pytest.raises(MirrorError) as e:
        m.create(OPS, {**body, "connection": "away"})
    assert "127.0.0.1:1" in clean(str(e.value)) and e.value.status == 502
    with pytest.raises(MirrorError) as e:
        m.create(OPS, {**body, "connection": "garbled"})
    clean(str(e.value))
    single = Mirrors(world["engine"], world["streams"], world["urls"].get)
    for connection in ("away", "garbled"):
        with pytest.raises(MirrorError) as e:
            single.create(OPS, {"name": "scrub1", "connection": connection, "table": "public.t", "target": "m_scrub.t"})
        clean(str(e.value))
    # A running mirror whose source goes away: what is stored and shown is as clean.
    sql(url, "CREATE SCHEMA scrub", "CREATE TABLE scrub.t (id int PRIMARY KEY)")
    world["urls"]["moving"] = url
    m.create(OPS, {**body, "connection": "moving"})
    try:
        world["urls"]["moving"] = away
        assert m.run("scrub") == 0
        out = m.get("scrub")
        assert out["status"] == "failed" and "127.0.0.1:1" in clean(out["error"]) and out["retained_bytes"] is None
        clean(repr(out))
    finally:
        world["urls"]["moving"] = url
    assert m.run("scrub") == 0 and m.get("scrub")["status"] == "running"
    m.delete(OPS, "scrub")


def test_a_copy_that_dies_half_way_is_done_again_whole(world, monkeypatch):
    import cairn.pipelines.mirror_db as module

    m, url = world["m"], world["url"]
    monkeypatch.setattr(module, "COPY_BATCH", 100)
    sql(url, "CREATE SCHEMA mid", "CREATE TABLE mid.a (id int PRIMARY KEY, v int)",
        "INSERT INTO mid.a SELECT g, g FROM generate_series(1, 350) g",
        "CREATE TABLE mid.h (id int PRIMARY KEY, v int)", "INSERT INTO mid.h SELECT g, g FROM generate_series(1, 350) g")
    real = m._write

    def dies_on_the_second_batch(t, o, ops, topic=None):
        if topic and ops[0][0] == 100:
            raise RuntimeError("the process died")
        return real(t, o, ops, topic)
    m._write = dies_on_the_second_batch
    try:
        out = mirror(world, "mid", tables={"h": {"history": True}})
    finally:
        m._write = real
    for t in ("a", "h"):
        assert state(out, t)["status"] == "failed" and not state(out, t)["copied"] and state(out, t)["copy_lsn"] is None
        assert "the process died" in state(out, t)["error"] and len(rows(world, f"m_mid.{t}")) == 100
    assert out["holding"] == []                                             # a copy that failed holds nothing back
    for t in ("a", "h"):
        sql(url, f"DELETE FROM mid.{t} WHERE id <= 10", f"UPDATE mid.{t} SET v = -1 WHERE id = 50", f"INSERT INTO mid.{t} VALUES (999, 9)")
    m.run("mid")
    out = m.get("mid")
    assert out["status"] == "running" and all(t["status"] == "running" and t["copied"] for t in out["mirrored"])
    assert [(r["id"], r["v"]) for r in rows(world, "m_mid.a")] == source(world, "SELECT id, v FROM mid.a ORDER BY id")
    current = [(r["id"], r["v"]) for r in rows(world, "m_mid.h", ("id", "_cairn_valid_from")) if r["_cairn_current"]]
    assert current == source(world, "SELECT id, v FROM mid.h ORDER BY id")
    assert m.run("mid") == 0
