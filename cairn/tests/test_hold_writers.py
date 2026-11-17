"""Legal holds cover Cairn's own writers, not only Trino (cairn.dataaccess.trino.HOLD_OPS): pipes, streams, mirrors
and database mirrors write to the lakehouse through pyiceberg, and each of them refuses (409)
to delete or replace rows of a held table: an overwrite, a delete, a truncate, a purge, an upsert of keys that are
there. Appends still land. Holds that cannot be read refuse (503).

On pyiceberg's SQL catalog (the same tables and commits as the REST catalog) where rows are written, and on fakes
that fail when the catalog would be reached where the refusal must come first. The holds themselves are the
records service's (tests/test_records_unit.py; on real Trino in tests/test_trino_e2e.py)."""

from __future__ import annotations

import types

import pytest
from sqlalchemy import create_engine, insert, select

from cairn.common.db import init_db

ADMIN = "urn:li:corpuser:admin"


def untouched(*_a, **_k):
    raise AssertionError("reached past the legal hold check: rows of a held table were about to go")


class Untouched:
    """A catalog, table or source that must not be reached."""

    def __getattr__(self, name):
        return untouched

    def __call__(self, *a, **k):
        untouched()


def holds(*targets):
    """``held("ns.table")`` for the given tables."""
    return lambda target: target in targets


def unreadable(target):
    raise RuntimeError("the database is down")


@pytest.fixture()
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path}/h.db")
    init_db(e)
    return e


@pytest.fixture()
def catalog(tmp_path):
    pytest.importorskip("pyarrow")
    pytest.importorskip("pyiceberg")
    from pyiceberg.catalog.sql import SqlCatalog

    cat = SqlCatalog("t", uri=f"sqlite:///{tmp_path}/cat.db", warehouse=f"file://{tmp_path}/wh")
    cat.create_namespace("lake")
    return cat


def table(cat, name: str, rows: list[dict], schema=None):
    import pyarrow as pa

    data = pa.Table.from_pylist(rows, schema=schema)
    t = cat.create_table(("lake", name), schema=data.schema)
    if rows:
        t.append(data)
    return cat.load_table(("lake", name))


def rows(cat, name: str, col: str = "id") -> list:
    return sorted(cat.load_table(("lake", name)).scan().to_arrow().column(col).to_pylist())


# ---- the shared check -----------------------------------------------------------------------------------------
class Refused(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def test_the_check_refuses_on_a_held_table_and_fails_closed(catalog):
    from pyiceberg.expressions import EqualTo

    from cairn.pipelines.pipes import check_not_held

    t = table(catalog, "events", [{"id": 1}, {"id": 2}])
    check_not_held(None, "lake.events", Refused, "w", t)                      # holds not configured
    check_not_held(holds(), "lake.events", Refused, "w", t)                   # not held
    with pytest.raises(Refused) as e:
        check_not_held(holds("lake.events"), "lake.events", Refused, "w")
    assert e.value.status == 409 and "under a legal hold" in str(e.value)
    with pytest.raises(Refused) as e:                                         # every row: some are there
        check_not_held(holds("lake.events"), "lake.events", Refused, "w", t)
    assert e.value.status == 409
    with pytest.raises(Refused):                                              # this row is there
        check_not_held(holds("lake.events"), "lake.events", Refused, "w", t, EqualTo("id", 2))
    # A delete that matches no row removes nothing: it goes ahead on a held table.
    check_not_held(holds("lake.events"), "lake.events", Refused, "w", t, EqualTo("id", 3))
    check_not_held(holds("lake.empty"), "lake.empty", Refused, "w", table(catalog, "empty", [], schema=t.schema().as_arrow()))
    with pytest.raises(Refused) as e:                                         # holds that cannot be read refuse
        check_not_held(unreadable, "lake.events", Refused, "w", t, EqualTo("id", 3))
    assert e.value.status == 503
    with pytest.raises(Refused) as e:                                         # rows that cannot be read: may be there
        check_not_held(holds("lake.events"), "lake.events", Refused, "w", Untouched(), EqualTo("id", 3))
    assert e.value.status == 409


# ---- pipes and streams ----------------------------------------------------------------------------------------
def pipes(engine, catalog, held):
    from cairn.pipelines.pipes import Pipes

    p = Pipes(engine, {}, {})
    p._catalog = lambda: catalog
    p.held = held
    return p


def test_a_files_pipe_does_not_replace_or_remove_an_objects_row_in_a_held_table(engine, catalog):
    import pyarrow as pa

    from cairn.pipelines.pipes import PipeError

    table(catalog, "docs", [{"key": "a.md", "text": "first"}])
    p = pipes(engine, catalog, holds("lake.docs"))
    pipe = {"id": "p1", "name": "docs", "target": "lake.docs", "format": "files", "create_table": True}
    with pytest.raises(PipeError) as e:                       # a new version of a.md would replace its row
        p._append(pipe, pa.table({"key": ["a.md"], "text": ["second"]}))
    assert e.value.status == 409 and "legal hold" in str(e.value)
    assert p._append(pipe, pa.table({"key": ["b.md"], "text": ["new"]}))[0] == 1     # a new object: appended
    with pytest.raises(PipeError, match="legal hold"):        # the object was deleted from the store
        p.forget(pipe, "a.md")
    # A sweep or event reports the refusal on the object (tried again next time) and goes on with the others.
    out = p._forget_one(pipe, "a.md")
    assert out["status"] == "error" and "legal hold" in out["error"]
    assert rows(catalog, "docs", "key") == ["a.md", "b.md"]
    assert rows(catalog, "docs", "text") == ["first", "new"]

    p.held = holds()                                           # the hold released: the new version replaces it
    p._append(pipe, pa.table({"key": ["a.md"], "text": ["second"]}))
    assert rows(catalog, "docs", "text") == ["new", "second"]


def streams(engine, catalog, held):
    from cairn.pipelines.streams import Streams

    s = Streams(engine, {}, {})
    s._catalog = lambda: catalog
    s.held = held
    return s


def change(offset, op, row):
    return types.SimpleNamespace(partition=0, offset=offset, key=None, value=(op, row))


def test_a_stream_does_not_upsert_over_or_delete_rows_of_a_held_table(engine, catalog):
    from cairn.pipelines.streams import StreamError

    table(catalog, "orders", [{"id": 1, "v": "a"}])
    s = streams(engine, catalog, holds("lake.orders"))
    stream = {"id": "s1", "name": "orders", "topic": "t", "format": "pgmirror", "mode": "upsert", "key": ["id"],
              "target": "lake.orders", "create_table": True, "evolve": True, "encoding": "json", "bad_rows": "fail",
              "tokenize": {}}
    for op, row in (("upsert", {"id": 1, "v": "b"}), ("delete", {"id": 1})):
        with pytest.raises(StreamError) as e:
            s.write(stream, [change(0, op, row)])
        assert e.value.status == 409 and "legal hold" in str(e.value)
    assert s.write(stream, [change(0, "upsert", {"id": 2, "v": "c"})]) == 1          # a new key: an append
    assert rows(catalog, "orders") == [1, 2] and rows(catalog, "orders", "v") == ["a", "c"]


# ---- mirrors and database mirrors ------------------------------------------------------------------------------
def mysql_mirror(engine, catalog, held, monkeypatch, feed):
    from cairn.pipelines import mirror_mysql as my
    from cairn.pipelines.mirror import Mirrors, mirrors_table

    monkeypatch.setattr(my, "changes", lambda url, schema, tbl, log_file, pos, key: feed)
    s = streams(engine, catalog, held)
    m = Mirrors(engine, s, lambda connection: "mysql://u:p@127.0.0.1:3306/shop")
    m.held = held
    with engine.begin() as c:
        c.execute(insert(mirrors_table).values(
            id="m1", name="orders", connection="shop", source="shop.orders", target="lake.orders", key="id",
            enabled=True, snapshot_done=True, status="running", error=None, rows=0, last_lsn="binlog.000001:4",
            last_commit_ms=None, created_by=ADMIN, created_ms=0))
    return m


def test_a_source_truncate_fails_a_mirror_of_a_held_table_instead_of_emptying_it(engine, catalog, monkeypatch):
    from cairn.pipelines.mirror import mirrors_table

    table(catalog, "orders", [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}])
    feed = ([(10, "upsert", {"id": 3, "v": "c"}), (11, "truncate", {}), (12, "upsert", {"id": 4, "v": "d"})],
            ("binlog.000001", 900))
    m = mysql_mirror(engine, catalog, holds("lake.orders"), monkeypatch, feed)
    assert m.run("orders") == 0
    with engine.connect() as c:
        r = c.execute(select(mirrors_table)).first()
    assert r.status == "failed" and "legal hold" in r.error
    assert r.last_lsn == "binlog.000001:4"            # the position does not move: the changes wait for the hold
    assert 1 in rows(catalog, "orders") and 2 in rows(catalog, "orders")    # not emptied

    # A copy made again (snapshot_done off) would empty it first: refused the same way.
    with engine.begin() as c:
        c.execute(mirrors_table.update().values(snapshot_done=False))
    from cairn.pipelines import mirror_mysql as my

    monkeypatch.setattr(my, "connect", lambda url: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(my, "position", lambda conn: ("binlog.000002", 4))
    monkeypatch.setattr(my, "copy", untouched)
    assert m.run("orders") == 0
    with engine.connect() as c:
        r = c.execute(select(mirrors_table)).first()
    assert r.status == "failed" and "legal hold" in r.error and not r.snapshot_done
    assert 1 in rows(catalog, "orders") and 2 in rows(catalog, "orders")


def test_a_mirror_of_a_table_that_is_not_held_still_follows_a_truncate(engine, catalog, monkeypatch):
    from cairn.pipelines.mirror import mirrors_table

    table(catalog, "orders", [{"id": 1, "v": "a"}])
    feed = ([(10, "truncate", {}), (11, "upsert", {"id": 4, "v": "d"})], ("binlog.000001", 900))
    m = mysql_mirror(engine, catalog, holds("lake.other"), monkeypatch, feed)
    assert m.run("orders") == 1
    assert rows(catalog, "orders") == [4]
    with engine.connect() as c:
        assert c.execute(select(mirrors_table.c.last_lsn)).scalar() == "binlog.000001:900"


def test_a_database_mirror_does_not_empty_or_change_rows_of_a_held_table(engine, catalog):
    pytest.importorskip("psycopg")
    from cairn.pipelines.mirror import MirrorError
    from cairn.pipelines.mirror_db import OWNER, DatabaseMirrors

    import pyarrow as pa

    data = pa.table({"id": [1, 2]})
    t = catalog.create_table(("lake", "people"), schema=data.schema, properties={OWNER: "d1"})
    t.append(data)
    m = DatabaseMirrors(engine, types.SimpleNamespace(_catalog=lambda: catalog), lambda connection: "")
    m.held = holds("lake.people")
    target = types.SimpleNamespace(target="lake.people", mirror_id="d1", source_name="people", renames="{}",
                                   gone="{}")
    # A copy made again of a table whose rows cannot be kept: emptied first, refused while held.
    with pytest.raises(MirrorError) as e:
        m._conform(target, {"1": {"name": "id", "oid": 20}}, {"history": False, "soft_delete": False})
    assert e.value.status == 409 and "legal hold" in str(e.value)
    assert rows(catalog, "people") == [1, 2]


# ---- wiring ---------------------------------------------------------------------------------------------------------
def test_services_hand_the_holds_to_every_writer_and_fail_closed(engine):
    from cairn.governance.records import Records
    from cairn.services import HOLD_WRITERS, _wire_holds

    records = Records(engine, None, None)
    lake = types.SimpleNamespace(urn=lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:trino,lakehouse.{ns[0]}.{t},PROD)")
    s = types.SimpleNamespace(records=records, lakehouse=lake,
                              **{w: types.SimpleNamespace(held=None) for w in HOLD_WRITERS})
    _wire_holds(s)
    assert all(getattr(s, w).held is s.table_held for w in HOLD_WRITERS)
    assert not s.table_held("crm.people")
    records.place(ADMIN, {"dataset": "urn:li:dataset:(urn:li:dataPlatform:trino,lakehouse.crm.people,PROD)",
                          "case": "C-1", "reason": "litigation"})
    assert s.table_held("crm.people") and not s.table_held("crm.other")
    records.held = unreadable                                # the database is down: the writers refuse
    from cairn.pipelines.pipes import PipeError, check_not_held

    with pytest.raises(PipeError) as e:
        check_not_held(s.table_held, "crm.other", PipeError, "pipe x")
    assert e.value.status == 503
    # No records service (privacy not configured): no holds, nothing wired.
    bare = types.SimpleNamespace(pipes=types.SimpleNamespace(held=None))
    _wire_holds(bare)
    assert bare.pipes.held is None
