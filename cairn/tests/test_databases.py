"""Operational databases (Snowflake gap K, part 1) on a real PostgreSQL server (CAIRN_TEST_PG_URL, a role that may
create databases and roles). Cairn makes a database whose only way in is a login Cairn hands out:
- a write login can create and fill tables, and the tables belong to the database, not the login;
- a read login reads and cannot write;
- someone without the privilege gets nothing;
- a login cannot reach another database;
- an expired or revoked login is dropped and its sessions end;
- a branch is a copy with its own roles;
- dropping the database removes everything.
Passwords are never stored or audited."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, select

from cairn.common.audit import AuditLog, audit_log
from cairn.common.db import init_db

ANA, BOB, CAL = "urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:cal"


@pytest.fixture
def dbs(tmp_path):
    url = os.environ.get("CAIRN_TEST_PG_URL")
    if not url:
        pytest.skip("CAIRN_TEST_PG_URL (a PostgreSQL to use) not set")
    from psycopg.conninfo import conninfo_to_dict

    from cairn.dataaccess.databases import Databases

    admin = url.replace("postgresql+psycopg://", "postgresql://")
    info = conninfo_to_dict(admin)
    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    grants = {ANA: {"CREATE_DATABASE", "MANAGE_DATABASE", "USE_DATABASE", "VIEW_DATABASE"}, BOB: {"VIEW_DATABASE"}}
    clock = {"now": 1_800_000_000_000}
    d = Databases(engine, admin, info.get("host", "localhost"), int(info.get("port", 5432)),
                  lambda actor, priv, urn: priv in grants.get(actor, set()), AuditLog(engine), "disable",
                  clock=lambda: clock["now"])
    made: list[str] = []
    yield {"d": d, "grants": grants, "clock": clock, "made": made, "engine": engine}
    for name in made:
        try:
            d.drop(ANA, name)
        except Exception:  # noqa: BLE001 - already gone
            pass


def connect(creds, **kw):
    import psycopg

    return psycopg.connect(host=creds["host"], port=creds["port"], dbname=kw.get("dbname", creds["database"]),
                           user=creds["user"], password=creds["password"], sslmode="disable", connect_timeout=5,
                           autocommit=True)


def test_a_database_is_reached_only_through_logins_cairn_hands_out(dbs):
    import psycopg

    from cairn.dataaccess.databases import DatabaseError

    d = dbs["d"]
    name = "app_" + uuid.uuid4().hex[:8]
    with pytest.raises(DatabaseError) as e:
        d.create(BOB, name)
    assert e.value.status == 403
    out = d.create(ANA, name)
    dbs["made"].append(name)
    assert out["urn"] == f"urn:li:pgDatabase:{name}" and out["database"] == name
    with pytest.raises(DatabaseError) as e:
        d.create(ANA, name)
    assert e.value.status == 409
    # Ana writes: the table belongs to the database's owner role, so it outlives her login.
    w = d.credentials(ANA, name, "write", 30)
    assert w["user"].startswith("cdb_u_") and len(w["password"]) >= 24
    with connect(w) as c:
        c.execute("CREATE TABLE orders (id int primary key, amount numeric)")
        c.execute("INSERT INTO orders VALUES (1, 9.5), (2, 20)")
        owner = c.execute("SELECT tableowner FROM pg_tables WHERE tablename = 'orders'").fetchone()[0]
    assert owner == f"cdb_{name}_owner"
    # Bob reads, and cannot write; he may not have a write login.
    r = d.credentials(BOB, name, "read", 30)
    with connect(r) as c:
        assert c.execute("SELECT count(*) FROM orders").fetchone()[0] == 2
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.execute("INSERT INTO orders VALUES (3, 1)")
    with pytest.raises(DatabaseError) as e:
        d.credentials(BOB, name, "write", 30)
    assert e.value.status == 403
    with pytest.raises(DatabaseError) as e:
        d.credentials(CAL, name, "read", 30)
    assert e.value.status == 403
    with pytest.raises(DatabaseError):
        d.credentials(ANA, name, "write", 10_000)                      # at most a day
    # A login reaches its own database only.
    other = "app_" + uuid.uuid4().hex[:8]
    d.create(ANA, other)
    dbs["made"].append(other)
    with pytest.raises(psycopg.OperationalError):
        connect(w, dbname=other)
    # Bob sees his own login; Ana, who manages the database, sees both. Bob ends his.
    assert [x["user"] for x in d.logins(BOB, name)] == [r["user"]]
    assert {x["user"] for x in d.logins(ANA, name)} == {w["user"], r["user"]}
    d.revoke(BOB, name, r["user"])
    with pytest.raises(psycopg.OperationalError):
        connect(r)
    # Expiry: the scheduler drops the login once its time is up, ending its sessions.
    held = connect(w)
    dbs["clock"]["now"] += 31 * 60_000
    assert d.expire() == 1
    with pytest.raises(psycopg.OperationalError):
        held.execute("SELECT 1")
    with pytest.raises(psycopg.OperationalError):
        connect(w)
    # No password anywhere in the audit log.
    with dbs["engine"].connect() as c:
        details = " ".join(str(r.details) for r in c.execute(select(audit_log)))
    assert w["password"] not in details and r["password"] not in details and "credentials" in " ".join(
        str(r.action) for r in dbs["engine"].connect().execute(select(audit_log)))


def test_a_branch_is_a_copy_with_roles_of_its_own(dbs):
    import psycopg

    d = dbs["d"]
    name, copy = "app_" + uuid.uuid4().hex[:8], "app_" + uuid.uuid4().hex[:8]
    d.create(ANA, name)
    dbs["made"] += [copy, name]
    w = d.credentials(ANA, name, "write", 30)
    with connect(w) as c:
        c.execute("CREATE TABLE t (id serial PRIMARY KEY, v int)")
        c.execute("INSERT INTO t (v) VALUES (1), (2), (3)")
    out = d.branch(ANA, name, copy)
    assert out["branch_of"] == name
    bw = d.credentials(ANA, copy, "write", 30)
    with connect(bw) as c:
        c.execute("INSERT INTO t (v) VALUES (4)")                      # the copy's tables (and sequences) are its own
        assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 4
    with connect(w) as c:
        assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 3   # the source is untouched
    with pytest.raises(psycopg.OperationalError):
        connect(w, dbname=copy)                                         # the source's login is not the copy's
    # A busy source cannot be copied.
    from cairn.dataaccess.databases import DatabaseError

    held = connect(w)
    with pytest.raises(DatabaseError) as e:
        d.branch(ANA, name, "app_" + uuid.uuid4().hex[:8])
    assert e.value.status == 409
    held.close()
    # Dropping ends sessions and removes the database, its roles and its logins.
    held = connect(bw)
    d.drop(ANA, copy)
    dbs["made"].remove(copy)
    with pytest.raises(psycopg.OperationalError):
        connect(bw)
    assert copy not in [x["name"] for x in d.list(ANA)]


def test_names_are_checked(dbs):
    from cairn.dataaccess.databases import DatabaseError

    for bad in ["Bad", "1x", "a-b", "x" * 32, "postgres", "template1", 'x"; drop', ""]:
        with pytest.raises(DatabaseError):
            dbs["d"].create(ANA, bad)


def test_the_source_of_a_branch_can_be_dropped(dbs):
    """Review finding: a branch kept the source roles' grants and default privileges, so dropping the source
    failed at DROP ROLE after the database itself was gone, and Cairn kept listing it."""
    d = dbs["d"]
    name, copy = "app_" + uuid.uuid4().hex[:8], "app_" + uuid.uuid4().hex[:8]
    d.create(ANA, name)
    dbs["made"] += [copy, name]
    w = d.credentials(ANA, name, "write", 30)
    with connect(w) as c:
        c.execute("CREATE TABLE t (id serial PRIMARY KEY, v int)")
        c.execute("INSERT INTO t (v) VALUES (1)")
    d.branch(ANA, name, copy)
    d.drop(ANA, name)                                          # was: DependentObjectsStillExist, a 500
    dbs["made"].remove(name)
    assert name not in [x["name"] for x in d.list(ANA)]
    bw, br = d.credentials(ANA, copy, "write", 30), d.credentials(ANA, copy, "read", 30)
    with connect(bw) as c:                                      # the branch is whole without its source
        c.execute("INSERT INTO t (v) VALUES (2)")
    with connect(br) as c:
        assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 2
        c.execute("SELECT 1")
    d.drop(ANA, copy)
    dbs["made"].remove(copy)

