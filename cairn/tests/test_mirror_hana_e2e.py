"""SAP HANA mirrors (L167): a HANA 2.0 (express edition) table kept in a lakehouse table through Cairn's change
triggers, with the real store and catalog. The mirror connects as its own user with SELECT and TRIGGER on the table.
Covered:
- the copy, then inserts, updates (a key change too), deletes and NULLs; TINYINT/INTEGER/BIGINT, DECIMAL kept exact,
  a floating DECIMAL as its exact text, DOUBLE, BOOLEAN, DATE, TIME, SECONDDATE, TIMESTAMP, NVARCHAR with quotes,
  newlines and non-ASCII text, VARBINARY, NCLOB and BLOB;
- a rolled-back transaction leaves nothing; the change table is emptied of what was applied;
- a transaction open while the mirror starts is waited for (HANA creates a trigger once writers have ended);
- a transaction that took its change numbers first and committed after a later one is not lost;
- a composite key (NVARCHAR and INTEGER) with a key change;
- a crash between the lakehouse commit and emptying the change table writes the same rows again, nothing more;
- a column added at the source; a trigger dropped at the source makes the mirror copy the table again;
- deleting the mirror drops its triggers and change table;
- refusals: no primary key, a user without TRIGGER (the grant named), a table the user cannot see; errors never
  show the password.

Locally, ``CAIRN_HANA_CONTAINER`` names a running HANA express container (port 39041 published, SYSTEM's password
in ``CAIRN_HANA_PASSWORD``) to use instead of starting one (a first start takes about four minutes)."""

import datetime as dt
import json
import os
import secrets
import subprocess
import tempfile
import threading
import time
from decimal import Decimal

import pytest

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake, port  # noqa: F401  (fixtures)

IMAGE = "saplabs/hanaexpress:2.00.088.00.20251110.1"
SYSTEM_PASSWORD = os.environ.get("CAIRN_HANA_PASSWORD") or "Hxe" + secrets.token_hex(5) + "A1"
MIRROR_PASSWORD = "Mirror" + secrets.token_hex(4) + "A1"

pytestmark = pytest.mark.skipif(not _ready() or (not os.environ.get("CAIRN_HANA_CONTAINER") and subprocess.run(
    ["docker", "image", "inspect", IMAGE], capture_output=True).returncode != 0),
    reason="needs docker, the HANA express image, and the store and catalog binaries")
dbapi = pytest.importorskip("hdbcli.dbapi")


def start_hana():
    p = port()
    name = f"cairn-mirror-hana-{p}-{os.getpid()}"
    d = tempfile.mkdtemp()
    os.chmod(d, 0o777)
    with open(f"{d}/pw.json", "w") as f:
        json.dump({"master_password": SYSTEM_PASSWORD}, f)
    os.chmod(f"{d}/pw.json", 0o666)
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{p}:39041",
                    "--sysctl", "kernel.shmmax=1073741824", "--sysctl", "net.ipv4.ip_local_port_range=40000 60999",
                    "--sysctl", "kernel.shmmni=4096", "--sysctl", "kernel.shmall=8388608", "-v", f"{d}:/hana/password",
                    IMAGE, "--passwords-url", "file:///hana/password/pw.json", "--agree-to-sap-license"],
                   check=True, capture_output=True)
    end = time.time() + 900
    while "Startup finished" not in subprocess.run(["docker", "logs", name], capture_output=True, text=True).stdout:
        if time.time() > end:
            raise RuntimeError("HANA did not start: " + subprocess.run(["docker", "logs", name], capture_output=True,
                                                                      text=True).stdout[-3000:])
        time.sleep(10)
    return name, p


def hana_port(container):
    out = subprocess.run(["docker", "port", container, "39041"], capture_output=True, text=True, check=True).stdout
    return int(out.strip().splitlines()[0].rsplit(":", 1)[1])


@pytest.fixture(scope="module")
def world(lake, tmp_path_factory):  # noqa: F811
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.pipelines.mirror import Mirrors
    from cairn.pipelines.streams import Streams

    reuse = os.environ.get("CAIRN_HANA_CONTAINER")
    if reuse:
        name, p = reuse, hana_port(reuse)
    else:
        name, p = start_hana()
    schema = "S" + secrets.token_hex(3).upper()
    tag = secrets.token_hex(2).upper()
    w = {"port": p, "schema": schema, "container": name, "mir": f"CAIRNMIR{tag}", "ro": f"CAIRNRO{tag}"}
    sql(w, f"CREATE SCHEMA {schema}",
        f'CREATE USER {w["mir"]} PASSWORD "{MIRROR_PASSWORD}" NO FORCE_FIRST_PASSWORD_CHANGE',
        f'CREATE USER {w["ro"]} PASSWORD "{MIRROR_PASSWORD}" NO FORCE_FIRST_PASSWORD_CHANGE',
        f"GRANT SELECT, TRIGGER ON SCHEMA {schema} TO {w['mir']}")
    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('hanamirror')}/cairn.db")
    init_db(engine)
    streams = Streams(engine, {}, {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                                   "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                                   "s3.region": "us-east-1", "s3.path-style-access": "true"})
    from urllib.parse import quote

    urls = {"hana": f"hana://{w['mir']}:{quote(MIRROR_PASSWORD)}@127.0.0.1:{p}",
            "hanaro": f"hana://{w['ro']}:{quote(MIRROR_PASSWORD)}@127.0.0.1:{p}"}
    audit = []

    class Audit:
        def record(self, *args, **kwargs):
            audit.append(args)
    w.update({"m": Mirrors(engine, streams, urls.get, audit=Audit()), "streams": streams, "engine": engine,
              "audit": audit, "ns": "hana_" + schema.lower()})
    yield w
    if not reuse:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)


def system(world, autocommit=True):
    conn = dbapi.connect(address="127.0.0.1", port=world["port"], user="SYSTEM", password=SYSTEM_PASSWORD)
    conn.setautocommit(autocommit)
    return conn


def sql(world, *statements, commit=True):
    conn = system(world, autocommit=False)
    try:
        cur = conn.cursor()
        for s in statements:
            cur.execute(s.replace("{s}", world["schema"]))
        conn.commit() if commit else conn.rollback()
    finally:
        conn.close()


def query(world, statement, params=()):
    conn = system(world)
    try:
        cur = conn.cursor()
        cur.execute(statement.replace("{s}", world["schema"]), params)
        return cur.fetchall()
    finally:
        conn.close()


def rows(world, target, key="id"):
    from pyiceberg.exceptions import NoSuchTableError

    try:
        t = world["streams"]._catalog().load_table(tuple(target.split(".")))
    except NoSuchTableError:
        return []
    return sorted(t.scan().to_arrow().to_pylist(), key=lambda r: tuple(r[k] for k in key.split(",")))


def create(world, name, table, connection="hana"):
    return world["m"].create("urn:li:corpuser:ops", {"name": name, "connection": connection,
                                                     "table": f"{world['schema']}.{table}",
                                                     "target": f"{world['ns']}.{name}"})


def mid(world, name):
    from sqlalchemy import select

    from cairn.pipelines.mirror import mirrors_table

    with world["engine"].connect() as c:
        return c.execute(select(mirrors_table.c.id).where(mirrors_table.c.name == name)).scalar()


def pending(world, name):
    from cairn.pipelines import mirror_hana

    ct = mirror_hana.names(mid(world, name))[0]
    conn = dbapi.connect(address="127.0.0.1", port=world["port"], user=world["mir"], password=MIRROR_PASSWORD)
    try:
        cur = conn.cursor()
        cur.execute(f'SELECT COUNT(*) FROM "{world["mir"]}"."{ct}"')       # the mirror's user owns it
        return cur.fetchone()[0]
    finally:
        conn.close()


def test_a_hana_table_is_copied_then_kept_in_step(world):
    m = world["m"]
    sql(world, "CREATE COLUMN TABLE {s}.CUSTOMERS (ID INTEGER PRIMARY KEY, NAME NVARCHAR(200), SCORE DECIMAL(12,2), "
               "BIG BIGINT, TINY TINYINT, FLOATING DECIMAL, RATIO DOUBLE, ACTIVE BOOLEAN, BORN DATE, AT_TIME TIME, "
               "SD SECONDDATE, SEEN TIMESTAMP, RAW VARBINARY(8), NOTE NCLOB, PIC BLOB)",
        "INSERT INTO {s}.CUSTOMERS VALUES (1, 'Ana', 9.50, 9007199254740993, 7, 1e40, 0.25, TRUE, '1990-04-01', "
        "'10:30:00', '2026-11-01 10:00:00', '2026-11-01 10:00:00.123456', x'00ff', 'first note', x'0102')",
        "INSERT INTO {s}.CUSTOMERS (ID, NAME) VALUES (2, 'Bob')")
    out = create(world, "hana_customers", "CUSTOMERS")
    assert out["status"] == "running" and out["key"] == ["id"], out
    target = f"{world['ns']}.hana_customers"
    got = rows(world, target)
    assert [(r["id"], r["name"], r["score"]) for r in got] == [(1, "Ana", Decimal("9.50")), (2, "Bob", None)]
    a = got[0]
    assert a["big"] == 9007199254740993 and a["tiny"] == 7 and Decimal(a["floating"]) == Decimal("1e40") and a["ratio"] == 0.25
    assert a["active"] is True and a["born"] == dt.date(1990, 4, 1) and a["at_time"] == "10:30:00"
    assert a["sd"] == dt.datetime(2026, 11, 1, 10) and a["seen"] == dt.datetime(2026, 11, 1, 10, 0, 0, 123456)
    assert a["raw"] == "00ff" and a["note"] == "first note" and a["pic"] == "0102"
    # Changes since, one rolled back; the key of 3 changes to 30, the NCLOB of 1 changes.
    sql(world, "INSERT INTO {s}.CUSTOMERS (ID, NAME) VALUES (3, 'Cal ''the'' " + "\n" + "Ünal')",
        "UPDATE {s}.CUSTOMERS SET SCORE = 7.25, NOTE = 'a longer note' WHERE ID = 1",
        "DELETE FROM {s}.CUSTOMERS WHERE ID = 2")
    sql(world, "UPDATE {s}.CUSTOMERS SET ID = 30 WHERE ID = 3")
    sql(world, "INSERT INTO {s}.CUSTOMERS (ID, NAME) VALUES (99, 'Ghost')", commit=False)
    assert m.run("hana_customers") == 4            # 1 upserted, 2 deleted, 3 deleted, 30 upserted
    got = rows(world, target)
    assert [r["id"] for r in got] == [1, 30], got
    assert got[0]["score"] == Decimal("7.25") and got[0]["note"] == "a longer note"
    assert got[1]["name"] == "Cal 'the' \nÜnal"
    assert pending(world, "hana_customers") == 0                          # what was applied is gone
    assert m.run("hana_customers") == 0
    assert m.get("hana_customers")["status"] == "running"


def test_a_transaction_open_while_the_mirror_starts_lands(world):
    sql(world, "CREATE COLUMN TABLE {s}.EVENTS (ID BIGINT PRIMARY KEY, V INTEGER)", "INSERT INTO {s}.EVENTS VALUES (1, 1)")
    open_conn = system(world, autocommit=False)
    cur = open_conn.cursor()
    cur.execute(f"INSERT INTO {world['schema']}.EVENTS VALUES (2, 2)")
    threading.Timer(5, open_conn.commit).start()
    try:
        out = create(world, "hana_events", "EVENTS")             # its triggers wait for that transaction
        world["m"].run("hana_events")
    finally:
        time.sleep(1)
        open_conn.close()
    assert out["status"] == "running", out
    assert [(r["id"], r["v"]) for r in rows(world, f"{world['ns']}.hana_events")] == [(1, 1), (2, 2)]


def test_a_transaction_committing_after_a_later_one_is_not_lost(world):
    """T1 records its change first and commits last: the run between sees only T2, and T1 arrives on the next."""
    m = world["m"]
    sql(world, "CREATE COLUMN TABLE {s}.ACCOUNTS (ID INTEGER PRIMARY KEY, V INTEGER)",
        "INSERT INTO {s}.ACCOUNTS VALUES (1, 0)",
        "INSERT INTO {s}.ACCOUNTS VALUES (2, 0)")
    create(world, "hana_accounts", "ACCOUNTS")
    t1 = system(world, autocommit=False)
    t1.cursor().execute(f"UPDATE {world['schema']}.ACCOUNTS SET V = 1 WHERE ID = 1")
    sql(world, "UPDATE {s}.ACCOUNTS SET V = 2 WHERE ID = 2")
    m.run("hana_accounts")
    target = f"{world['ns']}.hana_accounts"
    assert [(r["id"], r["v"]) for r in rows(world, target)] == [(1, 0), (2, 2)]
    t1.commit()
    t1.close()
    m.run("hana_accounts")
    assert [(r["id"], r["v"]) for r in rows(world, target)] == [(1, 1), (2, 2)]
    assert pending(world, "hana_accounts") == 0


def test_a_composite_key_and_a_crash_before_the_change_table_is_emptied(world, monkeypatch):
    from cairn.pipelines import mirror_hana

    m = world["m"]
    sql(world, "CREATE COLUMN TABLE {s}.LINES (ORDER_NO NVARCHAR(20), LINE INTEGER, QTY INTEGER, "
               "PRIMARY KEY (ORDER_NO, LINE))", "INSERT INTO {s}.LINES VALUES ('A-1', 1, 5)",
        "INSERT INTO {s}.LINES VALUES ('A-1', 2, 6)")
    out = create(world, "hana_lines", "LINES")
    assert out["key"] == ["order_no", "line"]
    sql(world, "UPDATE {s}.LINES SET LINE = 3 WHERE ORDER_NO = 'A-1' AND LINE = 2",
        "INSERT INTO {s}.LINES VALUES ('B-7', 1, 1)")
    real = mirror_hana.done

    def die(*args, **kwargs):
        raise RuntimeError("the process died before the change table was emptied")
    monkeypatch.setattr(mirror_hana, "done", die)
    assert m.run("hana_lines") == 0 and m.get("hana_lines")["status"] == "failed"
    target = f"{world['ns']}.hana_lines"
    want = [("A-1", 1, 5), ("A-1", 3, 6), ("B-7", 1, 1)]
    assert [(r["order_no"], r["line"], r["qty"]) for r in rows(world, target, "order_no,line")] == want
    monkeypatch.setattr(mirror_hana, "done", real)
    m.run("hana_lines")                                               # the same changes again: the same rows
    assert [(r["order_no"], r["line"], r["qty"]) for r in rows(world, target, "order_no,line")] == want
    assert pending(world, "hana_lines") == 0 and m.get("hana_lines")["status"] == "running"


def test_a_column_added_and_a_trigger_dropped(world):
    from cairn.pipelines import mirror_hana

    m = world["m"]
    sql(world, "CREATE COLUMN TABLE {s}.STOCK (ID INTEGER PRIMARY KEY, QTY INTEGER)",
        *[f"INSERT INTO {{s}}.STOCK VALUES ({i}, {i * 10})" for i in (1, 2, 3)])
    create(world, "hana_stock", "STOCK")
    sql(world, "ALTER TABLE {s}.STOCK ADD (BIN NVARCHAR(10))", "UPDATE {s}.STOCK SET BIN = 'A' WHERE ID = 1")
    m.run("hana_stock")
    target = f"{world['ns']}.hana_stock"
    assert [(r["id"], r["bin"]) for r in rows(world, target)] == [(1, "A"), (2, None), (3, None)]
    # Someone drops the delete trigger; a delete then goes unrecorded. The mirror makes it again and copies afresh.
    trigger = mirror_hana.names(mid(world, "hana_stock"))[1][2]
    sql(world, f'DROP TRIGGER "{world["schema"]}"."{trigger}"', "DELETE FROM {s}.STOCK WHERE ID = 2")
    m.run("hana_stock")
    assert [r["id"] for r in rows(world, target)] == [1, 3]
    assert any(a[1] == "mirror.restarted" for a in world["audit"])
    sql(world, "DELETE FROM {s}.STOCK WHERE ID = 3")                    # recorded again
    m.run("hana_stock")
    assert [r["id"] for r in rows(world, target)] == [1]


def test_deleting_a_mirror_drops_its_triggers_and_change_table(world):
    from cairn.pipelines import mirror_hana

    m = world["m"]
    sql(world, "CREATE COLUMN TABLE {s}.TEMP (ID INTEGER PRIMARY KEY)")
    create(world, "hana_temp", "TEMP")
    ct, triggers = mirror_hana.names(mid(world, "hana_temp"))
    assert len(query(world, "SELECT 1 FROM SYS.TRIGGERS WHERE SUBJECT_TABLE_SCHEMA = '{s}' AND "
                            "SUBJECT_TABLE_NAME = 'TEMP'")) == 3
    m.delete("urn:li:corpuser:ops", "hana_temp")
    assert query(world, "SELECT 1 FROM SYS.TRIGGERS WHERE SUBJECT_TABLE_SCHEMA = '{s}' AND "
                        "SUBJECT_TABLE_NAME = 'TEMP'") == []
    assert query(world, "SELECT 1 FROM SYS.TABLES WHERE TABLE_NAME = ?", (ct,)) == []
    sql(world, "INSERT INTO {s}.TEMP VALUES (1)")                     # the table is the source's own again


def test_refusals(world):
    from cairn.pipelines.mirror import MirrorError

    m, s = world["m"], world["schema"]

    def refused(table, connection="hana"):
        with pytest.raises(MirrorError) as e:
            m.create("urn:li:corpuser:ops", {"name": "hana_refused", "connection": connection,
                                             "table": f"{s}.{table}", "target": f"{world['ns']}.refused"})
        assert e.value.status == 409, e.value
        assert MIRROR_PASSWORD not in str(e.value)
        return str(e.value)

    sql(world, "CREATE COLUMN TABLE {s}.NOKEY (ID INTEGER)")
    assert "no primary key" in refused("NOKEY")
    sql(world, "CREATE COLUMN TABLE {s}.LIMITED (ID INTEGER PRIMARY KEY)",
        f"GRANT SELECT ON {{s}}.LIMITED TO {world['ro']}")
    msg = refused("LIMITED", connection="hanaro")
    assert f"GRANT TRIGGER ON \"{s}\".\"LIMITED\" TO \"{world['ro']}\"" in msg, msg
    sql(world, "CREATE COLUMN TABLE {s}.HIDDEN (ID INTEGER PRIMARY KEY)")
    assert "user can see" in refused("HIDDEN", connection="hanaro")
    assert "hana_refused" not in [x["name"] for x in m.list()]
    assert query(world, "SELECT 1 FROM SYS.TRIGGERS WHERE SUBJECT_TABLE_SCHEMA = '{s}' AND "
                        "SUBJECT_TABLE_NAME IN ('NOKEY', 'LIMITED')") == []
