"""MySQL mirrors (Snowflake gap N, connectors): a MySQL 8.4 table kept in a lakehouse table through the binary log,
with the real store and catalog. Covered:
- the copy, then inserts, updates (a key change too), deletes and MySQL types, in whole transactions;
- a rolled-back transaction leaves no trace;
- a TRUNCATE empties the lakehouse table;
- replaying from an older position after a crash writes nothing twice;
- a table without a primary key, and a server without a row-based log, are refused."""

import datetime as dt
import os
import subprocess
import time

import pytest

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake, port  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker and the store and catalog binaries")
pytest.importorskip("pymysqlreplication")


def start_mysql(*args):
    p = port()
    name = f"cairn-mirror-mysql-{p}-{os.getpid()}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{p}:3306",
                    "-e", "MYSQL_ROOT_PASSWORD=mypass", "-e", "MYSQL_DATABASE=shop", "mysql:8.4",
                    "--server-id=1", "--binlog-row-metadata=FULL", *args], check=True, capture_output=True)
    import pymysql

    end = time.time() + 120
    while True:
        try:
            pymysql.connect(host="127.0.0.1", port=p, user="root", password="mypass", database="shop").close()
            break
        except pymysql.err.OperationalError:
            if time.time() > end:
                raise
            time.sleep(1)
    return name, f"mysql://root:mypass@127.0.0.1:{p}/shop"


@pytest.fixture(scope="module")
def world(lake, tmp_path_factory):  # noqa: F811
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.pipelines.mirror import Mirrors
    from cairn.pipelines.streams import Streams

    my, url = start_mysql()
    stmt, stmt_url = start_mysql("--binlog-format=STATEMENT")
    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('mymirror')}/cairn.db")
    init_db(engine)
    streams = Streams(engine, {}, {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                                   "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                                   "s3.region": "us-east-1", "s3.path-style-access": "true"})
    m = Mirrors(engine, streams, {"shop": url, "old": stmt_url}.get)
    yield {"m": m, "streams": streams, "url": url, "engine": engine}
    subprocess.run(["docker", "rm", "-f", my, stmt], capture_output=True)


def sql(url, *statements, autocommit=True):
    from cairn.pipelines.mirror_mysql import connect

    conn = connect(url)
    try:
        conn.autocommit(autocommit)
        with conn.cursor() as cur:
            for s in statements:
                cur.execute(s)
        if not autocommit:
            conn.rollback()
    finally:
        conn.close()


def rows(world, target):
    t = world["streams"]._catalog().load_table(tuple(target.split(".")))
    return sorted(t.scan().to_arrow().to_pylist(), key=lambda r: r["id"])


def test_a_mysql_table_is_copied_then_kept_in_step(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE TABLE shop.customers (id INT PRIMARY KEY, name VARCHAR(50), score DECIMAL(10,2), active BOOL, "
             "born DATE, seen DATETIME)",
        "INSERT INTO shop.customers VALUES (1, 'Ana', 9.50, 1, '1990-04-01', '2026-11-01 10:00:00'), "
        "(2, 'Bob', NULL, 0, NULL, NULL)")
    out = m.create("urn:li:corpuser:ops", {"name": "customers", "connection": "shop", "table": "shop.customers",
                                           "target": "shop_mirror.customers"})
    assert out["status"] == "running" and out["key"] == ["id"], out
    got = rows(world, "shop_mirror.customers")
    assert [(r["id"], r["name"], r["score"]) for r in got] == [(1, "Ana", 9.5), (2, "Bob", None)]
    assert got[0]["born"] == dt.date(1990, 4, 1) and got[0]["seen"] == dt.datetime(2026, 11, 1, 10, 0)
    # Changes since, in transactions; one rolled back.
    sql(url, "INSERT INTO shop.customers (id, name) VALUES (3, 'Cal')",
        "UPDATE shop.customers SET score = 7.25 WHERE id = 1",
        "DELETE FROM shop.customers WHERE id = 2",
        "UPDATE shop.customers SET id = 30 WHERE id = 3")
    sql(url, "INSERT INTO shop.customers (id, name) VALUES (99, 'Ghost')", autocommit=False)
    assert m.run("customers") >= 4
    got = rows(world, "shop_mirror.customers")
    assert [r["id"] for r in got] == [1, 30] and got[0]["score"] == 7.25 and got[1]["name"] == "Cal"
    assert m.run("customers") == 0
    assert m.get("customers")["last_lsn"].startswith("binlog.")


def test_a_replay_after_a_crash_writes_nothing_twice(world):
    m, url = world["m"], world["url"]
    sql(url, "CREATE TABLE shop.events (id BIGINT PRIMARY KEY, v INT)")
    m.create("urn:li:corpuser:ops", {"name": "events", "connection": "shop", "table": "shop.events",
                                     "target": "shop_mirror.events"})
    before = m.get("events")["last_lsn"]
    sql(url, "INSERT INTO shop.events VALUES " + ", ".join(f"({i}, {i})" for i in range(1, 41)))
    m.run("events")
    assert len(rows(world, "shop_mirror.events")) == 40
    # The process died after the commit and before it recorded its position: it reads the 40 rows again.
    from sqlalchemy import update

    from cairn.pipelines.mirror import mirrors_table
    with world["engine"].begin() as c:
        c.execute(update(mirrors_table).where(mirrors_table.c.name == "events").values(last_lsn=before))
    sql(url, "UPDATE shop.events SET v = -1 WHERE id = 7")
    assert m.run("events") == 1                                        # only the update is new
    got = rows(world, "shop_mirror.events")
    assert len(got) == 40 and got[6]["v"] == -1


def test_truncate_and_refusals(world):
    from cairn.pipelines.mirror import MirrorError

    m, url = world["m"], world["url"]
    sql(url, "CREATE TABLE shop.carts (id INT PRIMARY KEY)", "INSERT INTO shop.carts VALUES (1), (2)")
    m.create("urn:li:corpuser:ops", {"name": "carts", "connection": "shop", "table": "shop.carts",
                                     "target": "shop_mirror.carts"})
    sql(url, "TRUNCATE TABLE shop.carts", "INSERT INTO shop.carts VALUES (9)")
    m.run("carts")
    assert [r["id"] for r in rows(world, "shop_mirror.carts")] == [9]
    m.delete("urn:li:corpuser:ops", "carts")
    sql(url, "CREATE TABLE shop.nokey (v INT)")
    with pytest.raises(MirrorError, match="primary key"):
        m.create("urn:li:corpuser:ops", {"name": "nokey", "connection": "shop", "table": "shop.nokey",
                                         "target": "shop_mirror.nokey"})
    with pytest.raises(MirrorError, match="binlog_format = ROW"):
        m.create("urn:li:corpuser:ops", {"name": "old", "connection": "old", "table": "shop.x",
                                         "target": "shop_mirror.x"})
