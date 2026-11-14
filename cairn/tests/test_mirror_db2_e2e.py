"""Db2 mirrors (L167): a Db2 12.1 table kept in a lakehouse table through SQL Replication's Capture program, which
reads the database's log, with the real store and catalog. The database archives its logs; a DBA creates Capture's
control tables, registers the tables (asnclp) and runs asncap, as they would; the mirror connects as its own user
with only the grants it needs. Covered:
- the copy, then inserts, updates (a key change too), deletes and NULLs, in whole transactions in commit order;
- SMALLINT/INTEGER/BIGINT, DECIMAL kept exact, DOUBLE, DATE, TIME, TIMESTAMP, CHAR, VARCHAR with quotes and
  newlines, VARCHAR FOR BIT DATA; a CLOB (Capture records only that it changed) read back by key;
- a rolled-back transaction leaves nothing; a transaction open on the table while the mirror starts is waited for
  and lands whole;
- Cairn's row in IBMSNAP_PRUNCNTL follows what the mirror applied (so Capture prunes only that), and goes with the
  mirror;
- a replay after a crash between the lakehouse commit and the mirror's record writes nothing twice;
- a registration Capture stopped (a CAPSTOP signal, as after a cold start) makes the mirror copy the table again;
- refusals: not registered, no primary key, no before image of the key, control tables in another schema, missing
  grants (named); errors never show the password.

Locally, ``CAIRN_DB2_CONTAINER`` names a running Db2 container (database TESTDB, port 50000 published, user db2inst1)
to use instead of starting one (a first start takes several minutes)."""

import datetime as dt
import os
import secrets
import subprocess
import threading
import time
from decimal import Decimal

import pytest

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake, port  # noqa: F401  (fixtures)

IMAGE = "icr.io/db2_community/db2:12.1.2.0"
ADMIN_PASSWORD = "Cairn-Db2-" + secrets.token_hex(4)
MIRROR_PASSWORD = "Mirror-" + secrets.token_hex(4) + "!"
DB = "TESTDB"

pytestmark = pytest.mark.skipif(not _ready() or (not os.environ.get("CAIRN_DB2_CONTAINER") and subprocess.run(
    ["docker", "image", "inspect", IMAGE], capture_output=True).returncode != 0),
    reason="needs docker, the Db2 Community image, and the store and catalog binaries")
ibm_db_dbi = pytest.importorskip("ibm_db_dbi")


def sh(container, script, user="db2inst1", check=True):
    """A shell script in the container, as the instance owner (its Db2 environment loaded)."""
    out = subprocess.run(["docker", "exec", "-i", "-u", user, container, "bash", "-lc", script], capture_output=True,
                         text=True, timeout=600)
    if check and out.returncode != 0:
        raise RuntimeError(f"{script[:200]}: {out.stdout[-2000:]} {out.stderr[-2000:]}")
    return out.stdout


def start_db2():
    p = port()
    name = f"cairn-mirror-db2-{p}-{os.getpid()}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--privileged", "-p", f"127.0.0.1:{p}:50000",
                    "-e", "LICENSE=accept", "-e", "DB2INSTANCE=db2inst1", "-e", f"DB2INST1_PASSWORD={ADMIN_PASSWORD}",
                    "-e", f"DBNAME={DB}", "-e", "ARCHIVE_LOGS=true", "-e", "AUTOCONFIG=false", "-e", "SAMPLEDB=false",
                    IMAGE], check=True, capture_output=True)
    end = time.time() + 900
    while "Setup has completed" not in subprocess.run(["docker", "logs", name], capture_output=True, text=True).stdout:
        if time.time() > end:
            raise RuntimeError("Db2 did not start: " + subprocess.run(["docker", "logs", name], capture_output=True,
                                                                     text=True).stdout[-3000:])
        time.sleep(10)
    return name, p


def db2_port(container):
    out = subprocess.run(["docker", "port", container, "50000"], capture_output=True, text=True, check=True).stdout
    return int(out.strip().splitlines()[0].rsplit(":", 1)[1])


def asnclp(container, *commands):
    script = "\n".join([f"SET SERVER CAPTURE TO DB {DB};", 'SET OUTPUT CAPTURE SCRIPT "/tmp/cairn-reg.sql";',
                        'SET LOG "/tmp/cairn-reg.err";', "SET RUN SCRIPT NOW STOP ON SQL ERROR ON;", *commands])
    out = sh(container, f"cd /tmp && cat > /tmp/cairn.asnclp <<'EOF'\n{script}\nEOF\nasnclp -f /tmp/cairn.asnclp")
    assert "ASN1954E" not in out and "failed" not in out.lower(), out


@pytest.fixture(scope="module")
def world(lake, tmp_path_factory):  # noqa: F811
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.pipelines.mirror import Mirrors
    from cairn.pipelines.streams import Streams

    reuse = os.environ.get("CAIRN_DB2_CONTAINER")
    if reuse:
        name, p = reuse, db2_port(reuse)
    else:
        name, p = start_db2()
    schema = "S" + secrets.token_hex(3).upper()
    # The DBA's part: Capture's control tables, a user for the mirror, and Capture running (reading the log every
    # second and committing what it captured every second, so the test does not wait on the defaults).
    if "IBMSNAP_REGISTER" not in sh(name, f"db2 connect to {DB} >/dev/null; db2 -x \"SELECT TABNAME FROM SYSCAT.TABLES "
                                          "WHERE TABSCHEMA = 'ASN' AND TABNAME = 'IBMSNAP_REGISTER'\"", check=False):
        asnclp(name, "CREATE CONTROL TABLES FOR CAPTURE SERVER;")
    for user in ("cairnmir", "cairnro"):
        sh(name, f"id {user} >/dev/null 2>&1 || useradd -M {user}; echo '{user}:{MIRROR_PASSWORD}' | chpasswd",
           user="root")
    sh(name, f"db2 connect to {DB} >/dev/null; db2 \"CREATE SCHEMA {schema}\"; "
             "db2 \"GRANT CONNECT ON DATABASE TO USER CAIRNMIR, USER CAIRNRO\"; "
             "for t in RESTART REGISTER; do db2 \"GRANT SELECT ON ASN.IBMSNAP_$t TO USER CAIRNMIR\"; done; "
             "db2 \"GRANT INSERT ON ASN.IBMSNAP_SIGNAL TO USER CAIRNMIR\"; "
             "db2 \"GRANT SELECT, INSERT, UPDATE, DELETE ON ASN.IBMSNAP_PRUNCNTL TO USER CAIRNMIR\"")
    sh(name, "pkill -x asncap; sleep 3; cd /tmp && (nohup asncap capture_server=TESTDB capture_schema=ASN "
             "commit_interval=1 sleep_interval=1 > /tmp/asncap.log 2>&1 &) ; sleep 5; pgrep -x asncap", check=False)
    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('db2mirror')}/cairn.db")
    init_db(engine)
    streams = Streams(engine, {}, {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                                   "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                                   "s3.region": "us-east-1", "s3.path-style-access": "true"})
    from urllib.parse import quote

    urls = {"db2": f"db2://cairnmir:{quote(MIRROR_PASSWORD)}@127.0.0.1:{p}/{DB}",
            "db2ro": f"db2://cairnro:{quote(MIRROR_PASSWORD)}@127.0.0.1:{p}/{DB}",
            "db2other": f"db2://cairnmir:{quote(MIRROR_PASSWORD)}@127.0.0.1:{p}/{DB}?capture_schema=ELSEWHERE"}
    audit = []

    class Audit:
        def record(self, *args, **kwargs):
            audit.append(args)
    m = Mirrors(engine, streams, urls.get, audit=Audit())
    yield {"m": m, "streams": streams, "port": p, "engine": engine, "container": name, "schema": schema,
           "audit": audit, "ns": "db2_" + schema.lower()}
    if not reuse:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)


def admin(world):
    return ibm_db_dbi.connect(f"DATABASE={DB};HOSTNAME=127.0.0.1;PORT={world['port']};PROTOCOL=TCPIP;",
                              "db2inst1", ADMIN_PASSWORD if not os.environ.get("CAIRN_DB2_CONTAINER")
                              else os.environ.get("CAIRN_DB2_PASSWORD", ADMIN_PASSWORD))


def sql(world, *statements, commit=True):
    conn = admin(world)
    conn.set_autocommit(False)
    try:
        cur = conn.cursor()
        for s in statements:
            cur.execute(s.replace("{s}", world["schema"]))
        conn.commit() if commit else conn.rollback()
    finally:
        conn.close()


def query(world, statement, params=()):
    conn = admin(world)
    try:
        cur = conn.cursor()
        cur.execute(statement.replace("{s}", world["schema"]), params)
        return cur.fetchall()
    finally:
        conn.close()


def register(world, table, cols="COLS ALL IMAGE BOTH", grant=True):
    s = world["schema"]
    asnclp(world["container"], f"CREATE REGISTRATION ({s}.{table}) DIFFERENTIAL REFRESH STAGE CD{table} {cols};")
    if grant:
        sql(world, f"GRANT SELECT ON {{s}}.{table} TO USER CAIRNMIR", f"GRANT SELECT ON {{s}}.CD{table} TO USER CAIRNMIR")


def rows(world, target, key="id"):
    from pyiceberg.exceptions import NoSuchTableError

    try:
        t = world["streams"]._catalog().load_table(tuple(target.split(".")))
    except NoSuchTableError:
        return []
    return sorted(t.scan().to_arrow().to_pylist(), key=lambda r: r[key])


def until(world, name, target, check, seconds=120):
    """Runs the mirror until the lakehouse table passes ``check`` (Capture reads the log every second)."""
    end = time.time() + seconds
    while True:
        world["m"].run(name)
        got = rows(world, target)
        if check(got) or time.time() > end:
            return got
        time.sleep(1)


def create(world, name, table, connection="db2"):
    m = world["m"]
    out = m.create("urn:li:corpuser:ops", {"name": name, "connection": connection,
                                           "table": f"{world['schema']}.{table}", "target": f"{world['ns']}.{name}"})
    end = time.time() + 120
    while out["status"] == "starting" and time.time() < end:     # Capture processes the start signal
        time.sleep(1)
        m.run(name)
        out = m.get(name)
    return out


def test_a_db2_table_is_copied_then_kept_in_step(world):
    m = world["m"]
    sql(world, "CREATE TABLE {s}.CUSTOMERS (ID INTEGER NOT NULL PRIMARY KEY, NAME VARCHAR(200), "
               "SCORE DECIMAL(12,2), BIG BIGINT, SMALL SMALLINT, RATIO DOUBLE, BORN DATE, "
               "AT_TIME TIME, SEEN TIMESTAMP, CODE CHAR(3), RAW VARCHAR(8) FOR BIT DATA, NOTE CLOB(1M))",
        "INSERT INTO {s}.CUSTOMERS VALUES (1, 'Ana', 9.50, 9007199254740993, 7, 0.25, '1990-04-01', "
        "'10:30:00', '2026-11-01 10:00:00.123456', 'ab', BX'00FF', 'first note'), "
        "(2, 'Bob', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)")
    register(world, "CUSTOMERS", "COLS (ID IMAGE BOTH, NAME IMAGE AFTER, SCORE IMAGE AFTER, BIG IMAGE AFTER, "
                                 "SMALL IMAGE AFTER, RATIO IMAGE AFTER, BORN IMAGE AFTER, "
                                 "AT_TIME IMAGE AFTER, SEEN IMAGE AFTER, CODE IMAGE AFTER, RAW IMAGE AFTER, "
                                 "NOTE IMAGE AFTER)")
    out = create(world, "db2_customers", "CUSTOMERS")
    assert out["status"] == "running" and out["key"] == ["id"] and out["last_lsn"].startswith("0x"), out
    target = f"{world['ns']}.db2_customers"
    got = rows(world, target)
    assert [(r["id"], r["name"], r["score"]) for r in got] == [(1, "Ana", Decimal("9.50")), (2, "Bob", None)]
    a = got[0]
    assert a["big"] == 9007199254740993 and a["small"] == 7 and a["ratio"] == 0.25
    assert a["born"] == dt.date(1990, 4, 1) and a["at_time"] == "10:30:00"
    assert a["seen"] == dt.datetime(2026, 11, 1, 10, 0, 0, 123456) and a["code"] == "ab "
    assert a["raw"] == "00ff" and a["note"] == "first note"
    # Changes since, in two transactions and one rolled back; the key of 3 changes to 30, the CLOB of 1 changes.
    sql(world, "INSERT INTO {s}.CUSTOMERS (ID, NAME) VALUES (3, 'Cal ''the'' " + "\n" + "second')",
        "UPDATE {s}.CUSTOMERS SET SCORE = 7.25, NOTE = 'a longer note' WHERE ID = 1",
        "DELETE FROM {s}.CUSTOMERS WHERE ID = 2")
    sql(world, "UPDATE {s}.CUSTOMERS SET ID = 30 WHERE ID = 3")
    sql(world, "INSERT INTO {s}.CUSTOMERS (ID, NAME) VALUES (99, 'Ghost')", commit=False)
    got = until(world, "db2_customers", target, lambda g: [r["id"] for r in g] == [1, 30])
    assert [r["id"] for r in got] == [1, 30], got
    assert got[0]["score"] == Decimal("7.25") and got[0]["note"] == "a longer note"
    assert got[1]["name"] == "Cal 'the' \nsecond"
    assert m.run("db2_customers") == 0                                       # nothing new
    status = m.get("db2_customers")
    assert status["status"] == "running" and status["rows"] >= 2
    # Cairn's subscription row says how far the mirror has read: Capture prunes up to there and no further.
    from cairn.pipelines import mirror_db2

    from sqlalchemy import select

    from cairn.pipelines.mirror import mirrors_table

    with world["engine"].connect() as c:
        mid = c.execute(select(mirrors_table.c.id).where(mirrors_table.c.name == "db2_customers")).scalar()
    sync = query(world, "SELECT SYNCHPOINT FROM ASN.IBMSNAP_PRUNCNTL WHERE MAP_ID = ?", (mirror_db2.map_id(mid),))
    assert sync and mirror_db2.seq_hex(bytes(sync[0][0])) == status["last_lsn"]


def test_a_transaction_open_while_the_mirror_starts_lands_whole(world):
    """A transaction wrote the table before Capture captured it and commits while the mirror starts: the copy waits
    for it, so its rows are neither lost nor half there."""
    sql(world, "CREATE TABLE {s}.EVENTS (ID BIGINT NOT NULL PRIMARY KEY, V INTEGER)",
        "INSERT INTO {s}.EVENTS VALUES (1, 1)")
    register(world, "EVENTS")
    open_conn = admin(world)
    open_conn.set_autocommit(False)
    cur = open_conn.cursor()
    cur.execute(f"INSERT INTO {world['schema']}.EVENTS VALUES (2, 2), (3, 3)")
    threading.Timer(15, open_conn.commit).start()             # commits while the mirror is starting
    try:
        out = create(world, "db2_events", "EVENTS")
        got = until(world, "db2_events", f"{world['ns']}.db2_events", lambda g: len(g) == 3)
    finally:
        time.sleep(1)
        open_conn.close()
    assert out["status"] == "running", out
    assert [(r["id"], r["v"]) for r in got] == [(1, 1), (2, 2), (3, 3)]


def test_a_replay_after_a_crash_leaves_the_table_right(world):
    from sqlalchemy import update

    from cairn.pipelines.mirror import mirrors_table

    m = world["m"]
    sql(world, "CREATE TABLE {s}.LEDGER (ID BIGINT NOT NULL PRIMARY KEY, V INTEGER)")
    register(world, "LEDGER")
    create(world, "db2_ledger", "LEDGER")
    before = m.get("db2_ledger")["last_lsn"]
    target = f"{world['ns']}.db2_ledger"
    sql(world, "INSERT INTO {s}.LEDGER VALUES " + ", ".join(f"({i}, {i})" for i in range(1, 41)))
    assert len(until(world, "db2_ledger", target, lambda g: len(g) == 40)) == 40
    # The process died after the commit and before it recorded its position: it reads the 40 rows again.
    with world["engine"].begin() as c:
        c.execute(update(mirrors_table).where(mirrors_table.c.name == "db2_ledger").values(last_lsn=before))
    sql(world, "UPDATE {s}.LEDGER SET V = -1 WHERE ID = 7")
    got = until(world, "db2_ledger", target, lambda g: g[6]["v"] == -1)
    assert len(got) == 40 and got[6]["v"] == -1 and sum(r["v"] for r in got) == sum(range(1, 41)) - 7 - 1


def test_a_registration_capture_stopped_copies_the_table_again(world):
    """Capture stops capturing a table (a CAPSTOP signal here; a cold start does the same): changes made meanwhile
    are not in the change table, so the mirror subscribes again and copies the table afresh."""
    m = world["m"]
    sql(world, "CREATE TABLE {s}.STOCK (ID INTEGER NOT NULL PRIMARY KEY, QTY INTEGER)",
        "INSERT INTO {s}.STOCK VALUES (1, 10), (2, 20), (3, 30)")
    register(world, "STOCK")
    create(world, "db2_stock", "STOCK")
    target = f"{world['ns']}.db2_stock"
    assert len(rows(world, target)) == 3
    sql(world, "INSERT INTO ASN.IBMSNAP_SIGNAL (SIGNAL_TIME, SIGNAL_TYPE, SIGNAL_SUBTYPE, SIGNAL_INPUT_IN, "
               "SIGNAL_STATE) VALUES (CURRENT TIMESTAMP, 'CMD', 'CAPSTOP', '{s}.STOCK', 'P')")
    end = time.time() + 60
    while query(world, "SELECT STATE FROM ASN.IBMSNAP_REGISTER WHERE SOURCE_OWNER = '{s}' AND SOURCE_TABLE = "
                       "'STOCK'")[0][0].strip() == "A" and time.time() < end:
        time.sleep(1)
    sql(world, "DELETE FROM {s}.STOCK WHERE ID = 2", "UPDATE {s}.STOCK SET QTY = 11 WHERE ID = 1")   # not captured
    got = until(world, "db2_stock", target, lambda g: [(r["id"], r["qty"]) for r in g] == [(1, 11), (3, 30)])
    assert [(r["id"], r["qty"]) for r in got] == [(1, 11), (3, 30)], got
    assert m.get("db2_stock")["status"] == "running"
    assert any(a[1] == "mirror.restarted" for a in world["audit"])
    sql(world, "INSERT INTO {s}.STOCK VALUES (4, 40)")                         # and it is captured again
    got = until(world, "db2_stock", target, lambda g: len(g) == 3 and g[-1]["id"] == 4)
    assert [r["id"] for r in got] == [1, 3, 4]


def test_deleting_a_mirror_removes_its_subscription(world):
    from cairn.pipelines import mirror_db2

    from sqlalchemy import select

    from cairn.pipelines.mirror import mirrors_table

    m = world["m"]
    sql(world, "CREATE TABLE {s}.TEMP (ID INTEGER NOT NULL PRIMARY KEY)")
    register(world, "TEMP")
    create(world, "db2_temp", "TEMP")
    with world["engine"].connect() as c:
        mid = c.execute(select(mirrors_table.c.id).where(mirrors_table.c.name == "db2_temp")).scalar()
    probe = "SELECT COUNT(*) FROM ASN.IBMSNAP_PRUNCNTL WHERE MAP_ID = ?"
    assert query(world, probe, (mirror_db2.map_id(mid),))[0][0] == 1
    m.delete("urn:li:corpuser:ops", "db2_temp")
    assert query(world, probe, (mirror_db2.map_id(mid),))[0][0] == 0


def test_refusals(world):
    from cairn.pipelines.mirror import MirrorError

    m, s = world["m"], world["schema"]

    def refused(table, connection="db2"):
        with pytest.raises(MirrorError) as e:
            m.create("urn:li:corpuser:ops", {"name": "db2_refused", "connection": connection,
                                             "table": f"{s}.{table}", "target": f"{world['ns']}.refused"})
        assert e.value.status == 409, e.value
        assert MIRROR_PASSWORD not in str(e.value)
        return str(e.value)

    sql(world, "CREATE TABLE {s}.UNREG (ID INTEGER NOT NULL PRIMARY KEY)", "GRANT SELECT ON {s}.UNREG TO USER CAIRNMIR")
    assert "not registered" in (msg := refused("UNREG")) and "CREATE REGISTRATION" in msg and "asncap" in msg
    sql(world, "CREATE TABLE {s}.NOKEY (ID INTEGER NOT NULL)")
    register(world, "NOKEY")
    assert "no primary key" in refused("NOKEY")
    sql(world, "CREATE TABLE {s}.AFTERONLY (ID INTEGER NOT NULL PRIMARY KEY, V INTEGER)")
    register(world, "AFTERONLY", "COLS ALL IMAGE AFTER")
    assert "before image" in refused("AFTERONLY")
    assert "control tables" in refused("UNREG", connection="db2other")
    sql(world, "CREATE TABLE {s}.LIMITED (ID INTEGER NOT NULL PRIMARY KEY)")
    register(world, "LIMITED", grant=False)
    sql(world, "GRANT SELECT ON {s}.LIMITED TO USER CAIRNRO", "GRANT SELECT ON {s}.CDLIMITED TO USER CAIRNRO",
        "GRANT SELECT ON ASN.IBMSNAP_REGISTER TO USER CAIRNRO")
    msg = refused("LIMITED", connection="db2ro")
    assert "GRANT INSERT ON ASN.IBMSNAP_SIGNAL" in msg and "IBMSNAP_PRUNCNTL" in msg and "IBMSNAP_RESTART" in msg, msg
    assert "db2_refused" not in [x["name"] for x in m.list()]
