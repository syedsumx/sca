"""Legal holds cover Cairn's own writers, not only Trino (cairn.dataaccess.trino.HOLD_OPS): pipes, streams, mirrors,
database mirrors, SaaS and connector syncs, vector indexes, sample and synthetic data, table branches, serving
tables' write-back and clean-room uploads write to the lakehouse through pyiceberg, and each of them refuses (409)
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


# ---- syncs ------------------------------------------------------------------------------------------------------
def test_a_saas_sync_does_not_overwrite_or_upsert_over_a_held_table(engine, catalog):
    import pyarrow as pa

    from cairn.pipelines.saas import SaasError, SaasSyncs

    table(catalog, "accounts", [{"id": "1", "name": "Acme", "_modified": "t0", "_synced_ms": 0}])
    s = SaasSyncs(engine, {}, untouched, lambda owner, urn: True)
    s._catalog = lambda: catalog
    s.held = holds("lake.accounts")
    sync = {"id": "x", "name": "accounts", "kind": "salesforce", "target": "lake.accounts", "fields": ["Name"]}
    with pytest.raises(SaasError) as e:                                     # a full replace
        s._replace(sync, pa.table({"id": ["9"]}), {})
    assert e.value.status == 409 and "legal hold" in str(e.value)
    with pytest.raises(SaasError, match="legal hold"):
        s._commit(sync, [{"id": "1", "values": {"Name": "Acme 2"}, "modified": "t1"}], advance=False)
    with pytest.raises(SaasError, match="legal hold"):
        s._commit(sync, [], replace=True, advance=False)
    s._commit(sync, [{"id": "2", "values": {"Name": "Beta"}, "modified": "t1"}], advance=False)   # new: appended
    assert rows(catalog, "accounts") == ["1", "2"] and rows(catalog, "accounts", "name") == ["Acme", "Beta"]


def test_a_connector_sync_does_not_replace_or_upsert_over_a_held_table(engine, catalog, monkeypatch):
    from cairn.pipelines.connectors import ConnectorError, ConnectorSyncs

    monkeypatch.setenv("CAIRN_CONNECTOR_IMAGES", "")
    c = ConnectorSyncs(engine, {}, untouched, lambda owner, urn: True, None)
    c._catalog = lambda: catalog
    c.held = holds("lake.people", "lake.log")
    up = {"name": "people", "target": "lake.people", "mode": "upsert", "primary_key": ["id"]}
    assert c._commit(up, [{"id": "1", "n": "a"}]) == 1                     # made by this commit
    with pytest.raises(ConnectorError) as e:
        c._commit(up, [{"id": "1", "n": "b"}])
    assert e.value.status == 409 and "legal hold" in str(e.value)
    assert c._commit(up, [{"id": "2", "n": "c"}]) == 1                     # a new key
    assert rows(catalog, "people", "n") == ["a", "c"]
    rep = {"name": "log", "target": "lake.log", "mode": "replace"}
    c._commit(rep, [{"line": "x"}])
    with pytest.raises(ConnectorError, match="legal hold"):
        c._commit(rep, [{"line": "y"}])
    assert rows(catalog, "log", "line") == ["x"]


# ---- vector indexes, samples, synthetic data, branches ---------------------------------------------------------
class Embedder:
    name, dim = "test", 2

    def __init__(self):
        self.texts: list = []

    def embed(self, texts):
        self.texts += texts
        return [[1.0, float(len(t))] for t in texts]


def test_a_vector_index_does_not_purge_or_rewrite_a_held_vectors_table(engine, catalog):
    pytest.importorskip("numpy")
    import pyarrow as pa
    from pyiceberg.expressions import EqualTo

    from cairn.pipelines.pipes import PipeError
    from cairn.pipelines.vectors import VectorIndexes

    src = table(catalog, "tickets", [{"id": "1", "body": "printer"}, {"id": "2", "body": "screen"}])
    emb = Embedder()
    vx = VectorIndexes(engine, {}, emb)
    vx._catalog = lambda: catalog
    ix = vx.create({"name": "t", "source": "lake.tickets", "key": "id", "text": "body"}, ADMIN)
    vx.refresh("t")
    vectors = vx.target(ix)
    vx.held = holds(vectors)
    src.append(pa.Table.from_pylist([{"id": "3", "body": "mouse"}], schema=src.schema().as_arrow()))
    assert vx.refresh("t")["embedded"] == 1                  # only new rows: appended, nothing removed
    src.delete(EqualTo("id", "1"))
    emb.embed = untouched                                    # refused before anything is embedded
    with pytest.raises(PipeError) as e:
        vx.refresh("t")
    assert e.value.status == 409 and "legal hold" in str(e.value)
    with pytest.raises(PipeError, match="legal hold"):
        vx.delete("t", ADMIN)
    assert rows(catalog, vectors.split(".", 1)[1], "key") == ["1", "2", "3"]


def test_sample_data_is_neither_reloaded_over_nor_removed_from_a_held_table(engine):
    pytest.importorskip("pyarrow")
    pytest.importorskip("pyiceberg")
    from cairn.pipelines.samples import MARK, SampleError, Samples

    s = Samples(engine, {"uri": "http://catalog"})
    s.held = holds("samples.orders")
    held_table = types.SimpleNamespace(properties={MARK: "retail"}, overwrite=untouched,
                                       scan=lambda **k: types.SimpleNamespace(to_arrow=lambda: [{"id": 1}]))
    s._catalog = lambda: Untouched()
    s._existing = lambda cat, name: held_table if name == "orders" else None
    s._is_sample = lambda t, name, cols: True
    with pytest.raises(SampleError) as e:
        s.load(ADMIN, "retail")
    assert e.value.status == 409 and "legal hold" in str(e.value)
    with pytest.raises(SampleError, match="legal hold"):     # purge_table is untouched: no table goes
        s.remove(ADMIN, "retail")


def test_synthetic_data_does_not_replace_a_held_table(catalog):
    from cairn.pipelines.synthetic import Synthetic, SyntheticError

    table(catalog, "orders", [{"id": 1}])
    table(catalog, "orders_dev", [{"id": 7}])
    s = Synthetic({}, lambda actor, privilege, urn: True, lambda ns, t: f"{ns}.{t}", untouched, "lakehouse",
                  untouched, untouched)
    s._catalog = lambda: catalog
    s.held = holds("lake.orders_dev")
    with pytest.raises(SyntheticError) as e:                 # before the source is profiled or the target dropped
        s.generate(ADMIN, "lake.orders", "lake.orders_dev", 10, replace=True)
    assert e.value.status == 409 and "legal hold" in str(e.value)
    assert rows(catalog, "orders_dev") == [7]


def test_a_branch_that_deletes_rows_is_not_published_to_a_held_table(catalog):
    import pyarrow as pa
    from pyiceberg.expressions import EqualTo

    from cairn.pipelines.branches import BranchError, Branches

    table(catalog, "orders", [{"id": 1}, {"id": 2}])
    b = Branches({}, lambda a, p, u: True, lambda ns, t: f"{ns}.{t}", lambda a, s, n: (["id"], [], False), "lakehouse")
    b._catalog = lambda: catalog
    b.held = holds("lake.orders")
    b.create_ref(ADMIN, "lake.orders", "adds")
    catalog.load_table(("lake", "orders")).append(pa.table({"id": pa.array([3], pa.int64())}), branch="adds")
    assert b.publish(ADMIN, "lake.orders", "adds")["published"]          # appends only: published
    b.create_ref(ADMIN, "lake.orders", "cleanup")
    catalog.load_table(("lake", "orders")).delete(EqualTo("id", 1), branch="cleanup")
    with pytest.raises(BranchError) as e:
        b.publish(ADMIN, "lake.orders", "cleanup")
    assert e.value.status == 409 and "legal hold" in str(e.value)
    assert rows(catalog, "orders") == [1, 2, 3]
    with pytest.raises(BranchError, match="legal hold"):                # its snapshots would go at expiry
        b.drop_ref(ADMIN, "lake.orders", "cleanup")


# ---- serving tables' write-back ---------------------------------------------------------------------------------
def test_a_serving_table_does_not_write_back_over_rows_of_a_held_table(engine, catalog, tmp_path):
    import json

    from cairn.dataaccess.serving import ServingError, ServingTables, pending_writes

    table(catalog, "orders", [{"id": 1, "v": "a"}])
    st = ServingTables(engine, lambda: catalog, lambda: "", None, lambda p, priv, urn: True, lambda t: t)
    store = create_engine(f"sqlite:///{tmp_path}/serving.db").execution_options(     # the serving database's
        schema_translate_map={pending_writes.schema: None})                           # pending writes
    pending_writes.create(store)
    st._write = st._read = store
    st.held = holds("lake.orders")
    c = {"name": "orders", "source": "lake.orders", "primary_key": json.dumps(["id"]), "indexes": "[]", "writable": True}

    def pend(key: int, v: str) -> None:
        with store.begin() as conn:
            conn.execute(insert(pending_writes).values(serving_table="orders", key_hash=str(key),
                                                       row=json.dumps({"id": key, "v": v}), written_by=ADMIN,
                                                       written_ms=key))
    pend(1, "changed")
    with pytest.raises(ServingError) as e:                  # the row with key 1 would be replaced
        st._write_back(c)
    assert e.value.status == 409 and "legal hold" in str(e.value)
    assert rows(catalog, "orders", "v") == ["a"]
    with store.connect() as conn:                           # still pending: written back once the hold is off
        assert conn.execute(select(pending_writes.c.key_hash)).scalars().all() == ["1"]
    with store.begin() as conn:
        conn.execute(pending_writes.delete())
    pend(2, "new")
    assert st._write_back(c) == 1                           # a new key: only appended
    assert rows(catalog, "orders") == [1, 2]


# ---- clean-room uploads -------------------------------------------------------------------------------------------
def test_a_partner_upload_does_not_replace_a_held_upload(engine):
    pytest.importorskip("pyarrow")
    from cairn.dataaccess.cleanrooms import CleanRooms, RoomError

    partner = "partner:acme"
    written = []
    cr = CleanRooms(engine, untouched, lambda actor, party: actor == party, lambda actor, table: True,
                    write_table=lambda target, data: written.append(target))
    cr.create(ADMIN, {"name": "ads", "parties": [ADMIN, partner]})
    cr.upload(partner, "ads", "spend", b"a,b\n1,2\n", "csv")
    cr.held = holds(written[0])
    with pytest.raises(RoomError) as e:
        cr.upload(partner, "ads", "spend", b"a,b\n3,4\n", "csv")
    assert e.value.status == 409 and "legal hold" in str(e.value)
    assert len(written) == 1
    cr.upload(partner, "ads", "other", b"a,b\n3,4\n", "csv")                # a new table: written
    assert len(written) == 2


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
