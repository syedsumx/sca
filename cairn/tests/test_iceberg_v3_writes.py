"""Iceberg v3 writes from Cairn's Python writers (pyiceberg with cairn.pipelines.iceberg_v3), on the real store and
catalog: appends, deletes, upserts and overwrites on v3 tables, with row lineage ids assigned as the v3 spec has them
(each data manifest's first_row_id, the snapshot's first-row-id and added-rows, the table's next-row-id), kept for
manifests carried into later snapshots; and a stream (the mirrors' writer) upserting into a v3 table. v2 tables are
written as before. Spark and Trino reading these ids is checked in catalog/conformance/test_iceberg_v3.py."""

import types

import pytest

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs the store and catalog binaries")


@pytest.fixture(scope="module")
def cat(lake):  # noqa: F811
    from pyiceberg.catalog.rest import RestCatalog

    from cairn.pipelines.iceberg_v3 import enable

    assert enable() is True and enable() is True                    # once, and idempotent
    props = {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
             "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1], "s3.region": "us-east-1",
             "s3.path-style-access": "true"}
    c = RestCatalog("v3w", **props)
    c.create_namespace("v3w")
    return c, props


def manifests(t):
    return sorted((m.first_row_id, m.added_rows_count, m.existing_rows_count)
                  for m in t.current_snapshot().manifests(t.io) if m.content.value == 0)


def test_row_ids_follow_the_v3_spec(cat):
    import pyarrow as pa
    from pyiceberg.expressions import EqualTo

    c, _ = cat
    data = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "v": pa.array(["a", "b", "c"])})
    t = c.create_table(("v3w", "t"), schema=data.schema, properties={"format-version": "3"})
    t.append(data)
    t = c.load_table(("v3w", "t"))
    s = t.current_snapshot()
    assert t.metadata.format_version == 3 and t.metadata.next_row_id == 3
    assert (s.first_row_id, s.added_rows) == (0, 3) and manifests(t) == [(0, 3, 0)]
    t.append(pa.table({"id": pa.array([4], pa.int64()), "v": pa.array(["d"])}))
    t = c.load_table(("v3w", "t"))
    assert t.metadata.next_row_id == 4                               # only the new row took an id
    assert manifests(t) == [(0, 3, 0), (3, 1, 0)]                    # the first manifest kept its ids
    # A delete rewrites the file it touches: the two rows kept are written anew and take new ids.
    t.delete(EqualTo("id", 2))
    t = c.load_table(("v3w", "t"))
    assert sorted(r["id"] for r in t.scan().to_arrow().to_pylist()) == [1, 3, 4]
    assert t.metadata.next_row_id == 6 and (3, 1, 0) in manifests(t)
    # Upsert and overwrite.
    t.upsert(pa.table({"id": pa.array([1, 9], pa.int64()), "v": pa.array(["A", "z"])}), join_cols=["id"])
    t = c.load_table(("v3w", "t"))
    assert sorted((r["id"], r["v"]) for r in t.scan().to_arrow().to_pylist()) == [(1, "A"), (3, "c"), (4, "d"), (9, "z")]
    before = t.metadata.next_row_id
    t.overwrite(pa.table({"id": pa.array([7], pa.int64()), "v": pa.array(["q"])}))
    t = c.load_table(("v3w", "t"))
    assert t.scan().to_arrow().to_pylist() == [{"id": 7, "v": "q"}] and t.metadata.next_row_id == before + 1
    # Ids never repeat: every snapshot starts where the last one ended.
    snaps = sorted(t.metadata.snapshots, key=lambda x: x.sequence_number)
    assert all(a.first_row_id + a.added_rows == b.first_row_id for a, b in zip(snaps, snaps[1:]))


def test_v2_tables_are_written_as_before(cat):
    import pyarrow as pa

    c, _ = cat
    data = pa.table({"id": pa.array([1], pa.int64())})
    t = c.create_table(("v3w", "old"), schema=data.schema)
    t.append(data)
    t.append(data)
    t = c.load_table(("v3w", "old"))
    assert t.metadata.format_version == 2 and t.current_snapshot().first_row_id is None
    assert [m.first_row_id for m in t.current_snapshot().manifests(t.io)] == [None, None]


def test_a_stream_upserts_into_a_v3_table(cat, tmp_path):
    import pyarrow as pa
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.pipelines.streams import Streams

    c, props = cat
    c.create_table(("v3w", "orders"), schema=pa.schema([pa.field("id", pa.int64()), pa.field("v", pa.string())]),
                   properties={"format-version": "3"})
    engine = create_engine(f"sqlite:///{tmp_path}/s.db")
    init_db(engine)
    st = Streams(engine, {}, props)
    stream = {"id": "s_v3", "name": "v3", "topic": "t", "format": "pgmirror", "mode": "upsert", "key": ["id"],
              "target": "v3w.orders", "create_table": False, "evolve": True, "encoding": "json", "bad_rows": "fail",
              "tokenize": {}}

    def msgs(*changes):
        return [types.SimpleNamespace(partition=0, offset=o, key=None, value=(op, row)) for o, op, row in changes]
    assert st.write(stream, msgs((0, "upsert", {"id": 1, "v": "a"}), (1, "upsert", {"id": 2, "v": "b"}))) == 2
    assert st.write(stream, msgs((2, "upsert", {"id": 1, "v": "A"}), (3, "delete", {"id": 2}))) >= 1
    t = c.load_table(("v3w", "orders"))
    assert t.metadata.format_version == 3 and t.scan().to_arrow().to_pylist() == [{"id": 1, "v": "A"}]
    assert t.metadata.next_row_id >= 3 and t.current_snapshot().first_row_id is not None
