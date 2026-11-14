"""Oracle mirrors (L167): an Oracle Database 23ai Free table kept in a lakehouse table through LogMiner, with the real
store and catalog. The database runs in ARCHIVELOG mode with supplemental logging, as a DBA would set it up; the
mirror connects as its own user with only the grants it needs. Covered:
- the copy (a flashback query), then inserts, updates, a key change, deletes and NULLs, in whole transactions;
- NUMBER with and without a scale, DATE, TIMESTAMP, TIMESTAMP WITH TIME ZONE (to UTC), VARCHAR2 with quotes and
  newlines (long enough for LogMiner to split its statement), RAW, BINARY_DOUBLE, and a CLOB larger than 4000
  bytes kept when only another column changes;
- a transaction of several rows arriving whole, and a rolled-back one leaving nothing;
- a replay after a crash between the lakehouse commit and the mirror's record, writing nothing twice;
- pause and resume; a column added while paused (the changes logged before it are read from their statements);
- other DDL failing the mirror; a TRUNCATE emptying the table;
- archived logs gone while paused, failing the mirror with what to do;
- refusals: no primary key, supplemental logging off (with the statements to run), grants missing (named), a
  database in NOARCHIVELOG mode; errors never show the password."""

import datetime as dt
import os
import secrets
import subprocess
import time

import pytest

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake, port  # noqa: F401  (fixtures)

IMAGE = "gvenzl/oracle-free:23.26.3-slim"
UTC = dt.timezone.utc

pytestmark = pytest.mark.skipif(not _ready() or subprocess.run(["docker", "image", "inspect", IMAGE],
                                                                capture_output=True).returncode != 0,
                                reason="needs docker, the Oracle Free image, and the store and catalog binaries")
oracledb = pytest.importorskip("oracledb")

# What a DBA runs once: ARCHIVELOG (LogMiner reads archived logs) and minimal supplemental logging. The image runs
# this on its first start.
INIT = """#!/bin/bash
mkdir -p /opt/oracle/oradata/arch
sqlplus -s / as sysdba <<SQL
WHENEVER SQLERROR EXIT FAILURE
ALTER SYSTEM SET log_archive_dest_1='LOCATION=/opt/oracle/oradata/arch' SCOPE=SPFILE;
SHUTDOWN IMMEDIATE
STARTUP MOUNT
ALTER DATABASE ARCHIVELOG;
ALTER DATABASE OPEN;
ALTER PLUGGABLE DATABASE ALL OPEN;
ALTER DATABASE ADD SUPPLEMENTAL LOG DATA;
EXIT
SQL
"""

# The mirror's own user: what LogMiner and the copy need, nothing more.
GRANTS = ["GRANT CREATE SESSION, LOGMINING, SELECT ANY TRANSACTION TO cairn", "GRANT EXECUTE ON SYS.DBMS_LOGMNR TO cairn"] \
    + [f"GRANT SELECT ON SYS.{v} TO cairn" for v in ("V_$LOGMNR_CONTENTS", "V_$LOG", "V_$LOGFILE", "V_$ARCHIVED_LOG",
                                                    "V_$DATABASE", "V_$TRANSACTION")]


def sysdba(name, script):
    """SQL as SYSDBA inside the container (operating-system authentication: no password anywhere)."""
    r = subprocess.run(["docker", "exec", "-i", name, "sqlplus", "-s", "/", "as", "sysdba"], capture_output=True,
                       text=True, timeout=600, input="WHENEVER SQLERROR EXIT FAILURE\n" + script + "\nEXIT\n")
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    return r.stdout


@pytest.fixture(scope="module")
def oracle(tmp_path_factory):
    d = tmp_path_factory.mktemp("oracle")
    (d / "init").mkdir()
    (d / "init" / "01-archivelog.sh").write_text(INIT)
    (d / "init" / "01-archivelog.sh").chmod(0o755)
    password = "C" + secrets.token_hex(12)              # generated per run, passed in a file
    (d / "password").write_text(password)
    (d / "password").chmod(0o644)
    p = port()
    name = f"cairn-mirror-oracle-{p}-{os.getpid()}"
    # The data files on a tmpfs: quick, and nothing left on disk.
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{p}:1521",
                    "--tmpfs", "/opt/oracle/oradata:rw,size=4g,mode=1777", "-v", f"{d / 'init'}:/container-entrypoint-initdb.d:ro",
                    "-v", f"{d / 'password'}:/run/secrets/oracle-password:ro",
                    "-e", "ORACLE_PASSWORD_FILE=/run/secrets/oracle-password", IMAGE], check=True, capture_output=True)
    try:
        end = time.time() + 600
        while "DATABASE IS READY TO USE" not in subprocess.run(["docker", "logs", name], capture_output=True,
                                                                text=True).stdout:
            if time.time() > end:
                raise AssertionError(subprocess.run(["docker", "logs", "--tail", "60", name], capture_output=True,
                                                    text=True).stdout)
            time.sleep(3)
        dsn = f"127.0.0.1:{p}/FREEPDB1"
        while True:                                     # a real connection, as the mirror will make
            try:
                oracledb.connect(user="system", password=password, dsn=dsn).close()
                break
            except oracledb.Error:
                if time.time() > end:
                    raise
                time.sleep(3)
        sys_ = oracledb.connect(user="sys", password=password, dsn=dsn, mode=oracledb.AUTH_MODE_SYSDBA)
        mirror_pw = "M" + secrets.token_hex(12)
        with sys_.cursor() as cur:
            cur.execute(f'CREATE USER shop IDENTIFIED BY "{password}" QUOTA UNLIMITED ON users')
            cur.execute("GRANT CREATE SESSION, CREATE TABLE TO shop")
            cur.execute(f'CREATE USER cairn IDENTIFIED BY "{mirror_pw}"')
            for g in GRANTS:
                cur.execute(g)
            cur.execute(f'CREATE USER nopriv IDENTIFIED BY "{mirror_pw}"')
            cur.execute("GRANT CREATE SESSION TO nopriv")
        sys_.close()
        yield {"name": name, "port": p, "dsn": dsn, "password": password, "mirror_pw": mirror_pw}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def sql(o, *statements, commit=True):
    conn = oracledb.connect(user="shop", password=o["password"], dsn=o["dsn"])
    try:
        with conn.cursor() as cur:
            for s in statements:
                if isinstance(s, tuple):
                    cur.execute(*s)
                else:
                    cur.execute(s)
        conn.commit() if commit else conn.rollback()
    finally:
        conn.close()


def table(o, ddl, name, logging="ALL"):
    """A table of the application's, set up for a mirror: supplemental logging, and the mirror's grants on it."""
    sql(o, ddl)
    if logging:
        sql(o, f"ALTER TABLE {name} ADD SUPPLEMENTAL LOG DATA ({logging}) COLUMNS")
    sql(o, f"GRANT SELECT ON {name} TO cairn", f"GRANT FLASHBACK ON {name} TO cairn",
        f"GRANT SELECT ON {name} TO nopriv")


@pytest.fixture(scope="module")
def world(lake, oracle, tmp_path_factory):  # noqa: F811
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.pipelines.mirror import Mirrors
    from cairn.pipelines.streams import Streams

    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('oramirror')}/cairn.db")
    init_db(engine)
    streams = Streams(engine, {}, {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                                   "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                                   "s3.region": "us-east-1", "s3.path-style-access": "true"})
    base = f"127.0.0.1:{oracle['port']}/FREEPDB1"
    urls = {"shop": f"oracle://cairn:{oracle['mirror_pw']}@{base}",
            "nopriv": f"oracle://nopriv:{oracle['mirror_pw']}@{base}"}
    m = Mirrors(engine, streams, urls.get)
    yield {"m": m, "streams": streams, "engine": engine, "urls": urls, **oracle}


def rows(world, target, key="id"):
    from pyiceberg.exceptions import NoSuchTableError

    try:
        t = world["streams"]._catalog().load_table(tuple(target.split(".")))
    except NoSuchTableError:
        return []
    return sorted(t.scan().to_arrow().to_pylist(), key=lambda r: r[key])


def until(world, name, target, check, seconds=60):
    end = time.time() + seconds
    while True:
        world["m"].run(name)
        got = rows(world, target)
        if check(got) or time.time() > end:
            return got
        time.sleep(1)


def create(world, name, source, target, connection="shop"):
    return world["m"].create("urn:li:corpuser:ops", {"name": name, "connection": connection, "table": source,
                                                     "target": target})


LONG = ("it's a \"quoted\" line\n" * 200)[:3900]       # LogMiner splits a statement this long over rows
CLOB = "lorem ipsum " * 900                             # 10,800 bytes: never in the redo values


def test_an_oracle_table_is_copied_then_kept_in_step(world):
    m = world["m"]
    table(world, "CREATE TABLE customers (id NUMBER(10) PRIMARY KEY, name VARCHAR2(4000), score NUMBER(10,2), "
                 "qty NUMBER, born DATE, seen TIMESTAMP(6), at TIMESTAMP WITH TIME ZONE, tag RAW(16), "
                 "ratio BINARY_DOUBLE, notes CLOB)", "customers")
    sql(world, ("INSERT INTO customers VALUES (1, 'Ana', 9.5, 3, DATE '1990-04-01', "
                "TIMESTAMP '2026-11-01 10:00:00.250000', TIMESTAMP '2026-11-01 10:00:00 +02:00', HEXTORAW('00FF'), "
                "1.5, :c)", {"c": CLOB}),
        "INSERT INTO customers (id, name) VALUES (2, 'Bob')")
    out = create(world, "ora_customers", "shop.customers", "shop_ora.customers")
    assert out["status"] == "running" and out["key"] == ["id"] and ":" in out["last_lsn"], out
    got = rows(world, "shop_ora.customers")
    assert [(r["id"], r["name"], r["score"], r["qty"]) for r in got] == [(1, "Ana", 9.5, 3.0), (2, "Bob", None, None)]
    a = got[0]
    assert a["born"] == dt.datetime(1990, 4, 1) and a["seen"] == dt.datetime(2026, 11, 1, 10, 0, 0, 250000)
    assert a["at"] == dt.datetime(2026, 11, 1, 8, 0, tzinfo=UTC) and a["tag"] == "00ff" and a["ratio"] == 1.5
    assert a["notes"] == CLOB and got[1]["notes"] is None

    # One transaction of three rows (one long, with quotes and newlines), committed as one.
    sql(world, ("INSERT INTO customers (id, name, score, born, at) VALUES (3, :n, -4.85, "
                "TO_DATE('2026-01-02 03:04:05', 'YYYY-MM-DD HH24:MI:SS'), TIMESTAMP '2026-01-02 03:04:05 -05:30')",
                {"n": LONG}),
        "INSERT INTO customers (id, name, qty) VALUES (4, 'Dee', 123.456)",
        "INSERT INTO customers (id, name) VALUES (5, 'Eve')")
    got = until(world, "ora_customers", "shop_ora.customers", lambda g: len(g) == 5)
    assert [r["id"] for r in got] == [1, 2, 3, 4, 5]
    c = got[2]
    assert c["name"] == LONG and c["score"] == -4.85 and c["born"] == dt.datetime(2026, 1, 2, 3, 4, 5)
    assert c["at"] == dt.datetime(2026, 1, 2, 8, 34, 5, tzinfo=UTC) and got[3]["qty"] == 123.456

    # Updates (one leaves the CLOB as it was), a key change, a delete, NULLs; and a transaction rolled back.
    sql(world, "UPDATE customers SET tag = HEXTORAW('ABCD'), score = 7.25 WHERE id = 1",
        "UPDATE customers SET id = 40 WHERE id = 4",
        "DELETE FROM customers WHERE id = 2",
        "UPDATE customers SET name = NULL, at = NULL WHERE id = 3",
        ("UPDATE customers SET notes = :c WHERE id = 5", {"c": "x" * 5000}))
    sql(world, "INSERT INTO customers (id, name) VALUES (99, 'Ghost')", "UPDATE customers SET score = 0 WHERE id = 1",
        commit=False)
    got = until(world, "ora_customers", "shop_ora.customers", lambda g: [r["id"] for r in g] == [1, 3, 5, 40])
    assert [r["id"] for r in got] == [1, 3, 5, 40], got
    one, three, five, forty = got
    assert one["tag"] == "abcd" and one["score"] == 7.25 and one["notes"] == CLOB and one["name"] == "Ana"
    assert one["born"] == dt.datetime(1990, 4, 1) and one["ratio"] == 1.5
    assert three["name"] is None and three["at"] is None and three["score"] == -4.85
    assert five["notes"] == "x" * 5000 and forty["name"] == "Dee" and forty["qty"] == 123.456
    assert m.run("ora_customers") == 0                                   # nothing new
    assert m.get("ora_customers")["status"] == "running"


def test_a_replay_after_a_crash_writes_nothing_twice(world):
    from sqlalchemy import update

    from cairn.pipelines.mirror import mirrors_table

    m = world["m"]
    table(world, "CREATE TABLE events (id NUMBER(19) PRIMARY KEY, v NUMBER(10))", "events", logging="PRIMARY KEY")
    create(world, "ora_events", "SHOP.EVENTS", "shop_ora.events")
    before = m.get("ora_events")["last_lsn"]
    sql(world, *[f"INSERT INTO events VALUES ({i}, {i})" for i in range(1, 41)])
    assert len(until(world, "ora_events", "shop_ora.events", lambda g: len(g) == 40)) == 40
    snapshots = len(world["streams"]._catalog().load_table(("shop_ora", "events")).snapshots())
    # The process died after the lakehouse commit and before it recorded its position: it reads the 40 rows again.
    with world["engine"].begin() as c:
        c.execute(update(mirrors_table).where(mirrors_table.c.name == "ora_events").values(last_lsn=before))
    assert m.run("ora_events") == 0                                      # all of it skipped by its offsets
    assert len(world["streams"]._catalog().load_table(("shop_ora", "events")).snapshots()) == snapshots
    # Only PRIMARY KEY supplemental logging: an update carries the key and what changed; the rest is read back.
    sql(world, "UPDATE events SET v = -1 WHERE id = 7")
    got = until(world, "ora_events", "shop_ora.events", lambda g: g[6]["v"] == -1)
    assert len(got) == 40 and got[6]["v"] == -1 and sum(r["v"] for r in got) == sum(range(1, 41)) - 7 - 1
    # A transaction open across runs (it began before the SCNs they read) arrives whole when it commits.
    long_tx = oracledb.connect(user="shop", password=world["password"], dsn=world["dsn"])
    with long_tx.cursor() as cur:
        cur.execute("INSERT INTO events VALUES (100, 100)")
        sql(world, "INSERT INTO events VALUES (41, 41)")
        until(world, "ora_events", "shop_ora.events", lambda g: len(g) == 41)
        cur.execute("INSERT INTO events VALUES (101, 101)")
        m.run("ora_events")
    long_tx.commit()
    long_tx.close()
    got = until(world, "ora_events", "shop_ora.events", lambda g: len(g) == 43)
    assert [r["id"] for r in got][-3:] == [41, 100, 101], got[-4:]


def test_pause_resume_and_a_column_added_while_paused(world, monkeypatch):
    from cairn.pipelines import mirror_oracle

    m = world["m"]
    parsed = []
    real = mirror_oracle.parse_dml
    monkeypatch.setattr(mirror_oracle, "parse_dml", lambda text: parsed.append(text) or real(text))
    table(world, "CREATE TABLE orders (id NUMBER(10) PRIMARY KEY, amount NUMBER(12,2), placed DATE, "
                 "at TIMESTAMP(6) WITH TIME ZONE, seen TIMESTAMP(6), memo VARCHAR2(200), tag RAW(8), big NUMBER(19))",
          "orders")
    sql(world, "INSERT INTO orders (id, amount) VALUES (1, 10)")
    create(world, "ora_orders", "shop.orders", "shop_ora.orders")
    assert m.set_enabled("urn:li:corpuser:ops", "ora_orders", False)["status"] == "paused"
    # Logged before the column is added: LogMiner can no longer map these with today's dictionary, so the mirror
    # reads them from their statements (Oracle's internal formats).
    sql(world, "INSERT INTO orders VALUES (2, -123.45, DATE '2026-03-04', TIMESTAMP '2026-03-04 05:06:07 +01:00', "
               "TIMESTAMP '2026-03-04 05:06:07.5', 'it''s \"two\"' || CHR(10) || 'lines', HEXTORAW('0A0B'), "
               "1231222344)",
        "UPDATE orders SET amount = 12.5 WHERE id = 1")
    sql(world, "DELETE FROM orders WHERE id = 1")
    sql(world, "ALTER TABLE orders ADD (note VARCHAR2(20))")
    sql(world, "INSERT INTO orders (id, amount, note) VALUES (3, 0.5, 'new')")
    assert m.run("ora_orders") == 0 and rows(world, "shop_ora.orders")[0]["amount"] == 10.0     # paused
    m.set_enabled("urn:li:corpuser:ops", "ora_orders", True)
    got = until(world, "ora_orders", "shop_ora.orders", lambda g: [r["id"] for r in g] == [2, 3])
    assert [r["id"] for r in got] == [2, 3], (got, m.get("ora_orders"))
    two, three = got
    assert two["amount"] == -123.45 and two["placed"] == dt.datetime(2026, 3, 4)
    assert two["at"] == dt.datetime(2026, 3, 4, 4, 6, 7, tzinfo=UTC) and two["seen"] == dt.datetime(2026, 3, 4, 5, 6, 7, 500000)
    assert two["memo"] == "it's \"two\"\nlines" and two["tag"] == "0a0b" and two["big"] == 1231222344
    assert two["note"] is None and three["note"] == "new" and three["amount"] == 0.5
    # The three changes logged before the column was added came from their statements ("COL n", HEXTORAW).
    assert len(parsed) == 3 and all('"COL 1"' in p and "HEXTORAW" in p for p in parsed), parsed
    # Other DDL fails the mirror, which says why; a TRUNCATE (on another mirror's table) empties it.
    sql(world, "ALTER TABLE orders DROP COLUMN memo")
    until(world, "ora_orders", "shop_ora.orders", lambda g: m.get("ora_orders")["status"] == "failed")
    got = m.get("ora_orders")
    assert got["status"] == "failed" and "DROP COLUMN" in got["error"].upper() and "create it again" in got["error"]
    sql(world, "TRUNCATE TABLE events")
    assert until(world, "ora_events", "shop_ora.events", lambda g: g == []) == []


def test_refusals_and_no_password_in_errors(world):
    from cairn.pipelines.mirror import MirrorError

    m, urls = world["m"], world["urls"]
    table(world, "CREATE TABLE nokey (id NUMBER(10))", "nokey")
    table(world, "CREATE TABLE unlogged (id NUMBER(10) PRIMARY KEY)", "unlogged", logging=None)
    table(world, "CREATE TABLE flags (id NUMBER(10) PRIMARY KEY, ok BOOLEAN)", "flags")
    with pytest.raises(MirrorError) as e:
        create(world, "ora_x", "shop.nokey", "shop_ora.x")
    assert e.value.status == 409 and "no primary key" in str(e.value)
    with pytest.raises(MirrorError) as e:
        create(world, "ora_x", "shop.unlogged", "shop_ora.x")
    assert e.value.status == 409 and 'ALTER TABLE "SHOP"."UNLOGGED" ADD SUPPLEMENTAL LOG DATA (ALL) COLUMNS' in str(e.value)
    with pytest.raises(MirrorError) as e:
        create(world, "ora_x", "shop.flags", "shop_ora.x")
    assert e.value.status == 409 and "OK (BOOLEAN)" in str(e.value)
    with pytest.raises(MirrorError) as e:
        create(world, "ora_x", "shop.customers", "shop_ora.x", connection="nopriv")
    assert e.value.status == 409
    for grant in ("GRANT LOGMINING TO NOPRIV", "GRANT SELECT ANY TRANSACTION TO NOPRIV",
                  "GRANT EXECUTE ON SYS.DBMS_LOGMNR TO NOPRIV", "GRANT SELECT ON SYS.V_$LOGMNR_CONTENTS TO NOPRIV",
                  'GRANT FLASHBACK ON "SHOP"."CUSTOMERS" TO NOPRIV'):
        assert grant in str(e.value), (grant, str(e.value))
    # A wrong password: refused as unreachable, without the password; a running mirror's error neither.
    good = urls["shop"]
    urls["wrong"] = good.replace(world["mirror_pw"], "Wrong-" + world["mirror_pw"][::-1])
    with pytest.raises(MirrorError) as e:
        create(world, "ora_x", "shop.customers", "shop_ora.x", connection="wrong")
    assert e.value.status == 502 and world["mirror_pw"][::-1] not in str(e.value) and "ORA-01017" in str(e.value)
    urls["shop"] = urls["wrong"]
    try:
        assert m.run("ora_customers") == 0
        got = m.get("ora_customers")
        assert got["status"] == "failed" and world["mirror_pw"][::-1] not in got["error"] and "ORA-01017" in got["error"]
    finally:
        urls["shop"] = good
    m.run("ora_customers")
    assert m.get("ora_customers")["status"] == "running"


def test_outside_a_pluggable_database_the_mirror_adds_the_log_files(world):
    """In the root (as in a non-CDB) LogMiner mines the log files it is given: the mirror adds the archived and
    online ones that cover its range."""
    password = world["password"]
    root = f"127.0.0.1:{world['port']}/FREE"
    sys_ = oracledb.connect(user="sys", password=password, dsn=root, mode=oracledb.AUTH_MODE_SYSDBA)
    with sys_.cursor() as cur:
        cur.execute(f'CREATE USER c##cairn IDENTIFIED BY "{world['mirror_pw']}" CONTAINER = ALL')
        for g in GRANTS:
            cur.execute(g.replace(" TO cairn", " TO c##cairn"))
        cur.execute("CREATE TABLE system.cairn_items (id NUMBER(10) PRIMARY KEY, label VARCHAR2(40))")
        cur.execute("ALTER TABLE system.cairn_items ADD SUPPLEMENTAL LOG DATA (ALL) COLUMNS")
        cur.execute("GRANT SELECT ON system.cairn_items TO c##cairn")
        cur.execute("GRANT FLASHBACK ON system.cairn_items TO c##cairn")
        cur.execute("INSERT INTO system.cairn_items VALUES (1, 'one')")
        sys_.commit()
    world["urls"]["root"] = f"oracle://c%23%23cairn:{world['mirror_pw']}@{root}"
    create(world, "ora_root_items", "SYSTEM.CAIRN_ITEMS", "shop_ora.root_items", connection="root")
    with sys_.cursor() as cur:
        cur.execute("INSERT INTO system.cairn_items VALUES (2, 'two')")
        cur.execute("UPDATE system.cairn_items SET label = 'uno' WHERE id = 1")
        sys_.commit()
    sysdba(world["name"], "ALTER SYSTEM ARCHIVE LOG CURRENT;")          # one change in an archived log ...
    with sys_.cursor() as cur:
        cur.execute("DELETE FROM system.cairn_items WHERE id = 2")      # ... one in the online log
        cur.execute("INSERT INTO system.cairn_items VALUES (3, 'three')")
        sys_.commit()
    sys_.close()
    got = until(world, "ora_root_items", "shop_ora.root_items", lambda g: [r["id"] for r in g] == [1, 3])
    assert [(r["id"], r["label"]) for r in got] == [(1, "uno"), (3, "three")], world["m"].get("ora_root_items")


def test_archived_logs_gone_while_paused(world):
    m = world["m"]
    table(world, "CREATE TABLE ledger (id NUMBER(10) PRIMARY KEY, v NUMBER(10))", "ledger")
    create(world, "ora_ledger", "shop.ledger", "shop_ora.ledger")
    m.set_enabled("urn:li:corpuser:ops", "ora_ledger", False)
    sql(world, "INSERT INTO ledger VALUES (1, 1)")
    # A DBA deletes the archived logs, and the online ones are reused: what the paused mirror needs is gone.
    sysdba(world["name"], "ALTER SYSTEM ARCHIVE LOG CURRENT;\n" * 3)
    subprocess.run(["docker", "exec", world["name"], "bash", "-c", "rm -f /opt/oracle/oradata/arch/*"], check=True)
    sysdba(world["name"], "ALTER SYSTEM ARCHIVE LOG CURRENT;\n" * 3)
    m.set_enabled("urn:li:corpuser:ops", "ora_ledger", True)
    assert m.run("ora_ledger") == 0
    got = m.get("ora_ledger")
    assert got["status"] == "failed" and "archived logs this mirror had read to are gone" in got["error"], got


def test_database_level_refusals(world):
    """Last: they change the database itself."""
    from cairn.pipelines.mirror import MirrorError

    sysdba(world["name"], "ALTER DATABASE DROP SUPPLEMENTAL LOG DATA;")
    with pytest.raises(MirrorError) as e:
        create(world, "ora_y", "shop.ledger", "shop_ora.y")
    assert e.value.status == 409 and "ALTER DATABASE ADD SUPPLEMENTAL LOG DATA" in str(e.value)
    sysdba(world["name"], "SHUTDOWN IMMEDIATE\nSTARTUP MOUNT\nALTER DATABASE NOARCHIVELOG;\nALTER DATABASE OPEN;\n"
                          "ALTER PLUGGABLE DATABASE ALL OPEN;\nALTER DATABASE ADD SUPPLEMENTAL LOG DATA;")
    with pytest.raises(MirrorError) as e:
        create(world, "ora_y", "shop.ledger", "shop_ora.y")
    assert e.value.status == 409 and "NOARCHIVELOG" in str(e.value) and "ALTER DATABASE ARCHIVELOG" in str(e.value)
