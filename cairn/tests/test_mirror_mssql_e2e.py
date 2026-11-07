"""SQL Server mirrors (Openflow's SQL Server connector, Cairn's way): a SQL Server 2022 table kept in a lakehouse table
through change data capture, with SQL Server Agent running the capture job, and the real store and catalog. Covered:
- the copy, then inserts, updates (a key change too), deletes and SQL Server types, in commit order;
- a rolled-back transaction leaves no trace;
- replaying from an older LSN after a crash leaves the table right (nothing doubled);
- a table without CDC, and one without a primary key, are refused with what to do."""

import datetime as dt
import os
import subprocess
import time

import pytest

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake, port  # noqa: F401  (fixtures)

IMAGE = "mcr.microsoft.com/mssql/server:2022-latest"
PASSWORD = "Cairn-Mirror-2026!"

pytestmark = pytest.mark.skipif(not _ready() or subprocess.run(["docker", "image", "inspect", IMAGE],
                                                                capture_output=True).returncode != 0,
                                reason="needs docker, the SQL Server image, and the store and catalog binaries")
pytest.importorskip("pymssql")


def start_mssql():
    import pymssql

    p = port()
    name = f"cairn-mirror-mssql-{p}-{os.getpid()}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{p}:1433",
                    "-e", "ACCEPT_EULA=Y", "-e", f"MSSQL_SA_PASSWORD={PASSWORD}", "-e", "MSSQL_AGENT_ENABLED=true",
                    "-e", "MSSQL_PID=Developer", IMAGE], check=True, capture_output=True)
    end = time.time() + 180
    while True:
        try:
            pymssql.connect(server="127.0.0.1", port=p, user="sa", password=PASSWORD, login_timeout=5).close()
            break
        except pymssql.Error:
            if time.time() > end:
                raise
            time.sleep(2)
    return name, p


def sql(port_, *statements, database="shop", commit=True):
    import pymssql

    conn = pymssql.connect(server="127.0.0.1", port=port_, user="sa", password=PASSWORD, database=database,
                           autocommit=commit)
    try:
        with conn.cursor() as cur:
            for s in statements:
                cur.execute(s)
        if not commit:
            conn.rollback()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def world(lake, tmp_path_factory):  # noqa: F811
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.pipelines.mirror import Mirrors
    from cairn.pipelines.streams import Streams

    name, p = start_mssql()
    sql(p, "CREATE DATABASE shop", database="master")
    sql(p, "EXEC sys.sp_cdc_enable_db")
    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('msmirror')}/cairn.db")
    init_db(engine)
    streams = Streams(engine, {}, {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                                   "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                                   "s3.region": "us-east-1", "s3.path-style-access": "true"})
    url = f"mssql://sa:{PASSWORD.replace('!', '%21')}@127.0.0.1:{p}/shop"
    m = Mirrors(engine, streams, {"shop": url}.get)
    yield {"m": m, "streams": streams, "port": p, "engine": engine}
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def track(port_, table):
    sql(port_, f"EXEC sys.sp_cdc_enable_table @source_schema = N'dbo', @source_name = N'{table}', @role_name = NULL")


def rows(world, target):
    from pyiceberg.exceptions import NoSuchTableError

    try:
        t = world["streams"]._catalog().load_table(tuple(target.split(".")))
    except NoSuchTableError:                       # an empty source: the table comes with its first rows
        return []
    return sorted(t.scan().to_arrow().to_pylist(), key=lambda r: r["id"])


def until(world, name, target, check, seconds=90):
    """Runs the mirror until the lakehouse table passes ``check`` (CDC's capture job reads the log every few seconds)."""
    end = time.time() + seconds
    while True:
        world["m"].run(name)
        got = rows(world, target)
        if check(got) or time.time() > end:
            return got
        time.sleep(2)


def test_a_sql_server_table_is_copied_then_kept_in_step(world):
    m, p = world["m"], world["port"]
    sql(p, "CREATE TABLE dbo.customers (id INT PRIMARY KEY, name NVARCHAR(50), score DECIMAL(10,2), active BIT, "
           "born DATE, seen DATETIME2)",
        "INSERT INTO dbo.customers VALUES (1, N'Ana', 9.50, 1, '1990-04-01', '2026-11-01 10:00:00'), "
        "(2, N'Bob', NULL, 0, NULL, NULL)")
    track(p, "customers")
    out = m.create("urn:li:corpuser:ops", {"name": "ms_customers", "connection": "shop", "table": "dbo.customers",
                                           "target": "shop_ms.customers"})
    assert out["status"] == "running" and out["key"] == ["id"] and out["last_lsn"].startswith("0x"), out
    got = rows(world, "shop_ms.customers")
    assert [(r["id"], r["name"], r["score"]) for r in got] == [(1, "Ana", 9.5), (2, "Bob", None)]
    assert got[0]["born"] == dt.date(1990, 4, 1) and got[0]["seen"] == dt.datetime(2026, 11, 1, 10, 0)
    assert got[0]["active"] is True
    # Changes since, one rolled back; the key of 3 changes to 30.
    sql(p, "INSERT INTO dbo.customers (id, name) VALUES (3, N'Cal')",
        "UPDATE dbo.customers SET score = 7.25 WHERE id = 1",
        "DELETE FROM dbo.customers WHERE id = 2",
        "UPDATE dbo.customers SET id = 30 WHERE id = 3")
    sql(p, "INSERT INTO dbo.customers (id, name) VALUES (99, N'Ghost')", commit=False)
    got = until(world, "ms_customers", "shop_ms.customers", lambda g: [r["id"] for r in g] == [1, 30])
    assert [r["id"] for r in got] == [1, 30] and got[0]["score"] == 7.25 and got[1]["name"] == "Cal", got
    assert m.run("ms_customers") == 0                                  # nothing new
    assert m.get("ms_customers")["status"] == "running"


def test_a_replay_after_a_crash_leaves_the_table_right(world):
    from sqlalchemy import update

    from cairn.pipelines.mirror import mirrors_table

    m, p = world["m"], world["port"]
    sql(p, "CREATE TABLE dbo.events (id BIGINT PRIMARY KEY, v INT)")
    track(p, "events")
    m.create("urn:li:corpuser:ops", {"name": "ms_events", "connection": "shop", "table": "dbo.events",
                                     "target": "shop_ms.events"})
    before = m.get("ms_events")["last_lsn"]
    sql(p, "INSERT INTO dbo.events VALUES " + ", ".join(f"({i}, {i})" for i in range(1, 41)))
    assert len(until(world, "ms_events", "shop_ms.events", lambda g: len(g) == 40)) == 40
    # The process died after the commit and before it recorded its LSN: it reads the 40 rows again.
    with world["engine"].begin() as c:
        c.execute(update(mirrors_table).where(mirrors_table.c.name == "ms_events").values(last_lsn=before))
    sql(p, "UPDATE dbo.events SET v = -1 WHERE id = 7")
    got = until(world, "ms_events", "shop_ms.events", lambda g: g[6]["v"] == -1)
    assert len(got) == 40 and got[6]["v"] == -1 and sum(r["v"] for r in got) == sum(range(1, 41)) - 7 - 1


def test_refusals(world):
    from cairn.pipelines.mirror import MirrorError

    m, p = world["m"], world["port"]
    sql(p, "CREATE TABLE dbo.untracked (id INT PRIMARY KEY)", "CREATE TABLE dbo.nokey (id INT)")
    with pytest.raises(MirrorError) as e:
        m.create("urn:li:corpuser:ops", {"name": "ms_x", "connection": "shop", "table": "dbo.untracked",
                                         "target": "shop_ms.x"})
    assert e.value.status == 409 and "sp_cdc_enable_table" in str(e.value)
    track(p, "nokey")
    with pytest.raises(MirrorError) as e:
        m.create("urn:li:corpuser:ops", {"name": "ms_y", "connection": "shop", "table": "dbo.nokey",
                                         "target": "shop_ms.y"})
    assert e.value.status == 409 and "no primary key" in str(e.value)
