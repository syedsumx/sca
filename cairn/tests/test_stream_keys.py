"""Upsert streams (cairn.pipelines.streams.Streams.write): a message whose key column holds an object or a list is
set aside like any message that cannot be a row. It cannot be a key (the batch is grouped by key, and a key is a
value to match in the table), and before it was set aside it failed the whole batch on every retry: one such
message stopped the stream for good.

Written into a real Iceberg table (pyiceberg's SQL catalog on a local warehouse); the end-to-end tests with Kafka,
Cairn Catalog and Cairn Store are in tests/test_streams_e2e.py."""

import json
import types

import pytest

pytest.importorskip("pyiceberg")
pytest.importorskip("pyarrow")

from cairn.pipelines.streams import Streams  # noqa: E402


@pytest.fixture
def streams(tmp_path):
    from pyiceberg.catalog.sql import SqlCatalog
    from sqlalchemy import create_engine

    from cairn.common.db import init_db

    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    s = Streams(engine, {}, {})
    cat = SqlCatalog("t", uri=f"sqlite:///{tmp_path}/iceberg.db", warehouse=f"file://{tmp_path}/wh")
    s._catalog = lambda: cat
    import pyarrow as pa

    cat.create_namespace("raw")
    cat.create_table(("raw", "orders"), schema=pa.schema([("id", pa.int64()), ("v", pa.string())]))
    return s, cat


def msg(offset, value):
    return types.SimpleNamespace(partition=0, offset=offset, key=None, value=json.dumps(value).encode())


@pytest.mark.parametrize("bad", [{"a": 1}, [1, 2]])
def test_a_key_that_is_not_a_value_is_set_aside(streams, bad):
    s, cat = streams
    stream = {"id": "s1", "name": "orders", "topic": "orders", "format": "json", "mode": "upsert", "key": ["id"],
              "target": "raw.orders", "create_table": True, "evolve": False, "encoding": "json"}
    rejected = []
    n = s.write(stream, [msg(0, {"id": 1, "v": "a"}), msg(1, {"id": bad, "v": "b"}), msg(2, {"id": 2, "v": "c"})],
                reject=lambda sid, p, o, reason, raw: rejected.append((o, reason)))
    assert n == 2
    assert [o for o, _ in rejected] == [1] and "key" in rejected[0][1]
    rows = sorted(cat.load_table(("raw", "orders")).scan().to_arrow().to_pylist(), key=lambda r: r["id"])
    assert rows == [{"id": 1, "v": "a"}, {"id": 2, "v": "c"}]
