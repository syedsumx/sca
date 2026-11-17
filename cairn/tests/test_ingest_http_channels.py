"""HTTP stream channels across API replicas: the lock that keeps one writer per channel is per process, so two
replicas (or workers) can take the same request at once (a producer's retry after a timeout). The table's own
offsets decide what is written: a batch whose rows another writer has already committed writes nothing twice.
A local Iceberg catalog (SQLite, files on disk) stands in for Cairn Catalog; test_ingest_http_e2e.py runs on
the real one."""

from __future__ import annotations

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("pyiceberg")

from sqlalchemy import create_engine  # noqa: E402

from cairn.common.db import init_db  # noqa: E402
from cairn.pipelines.streams import StreamError, Streams  # noqa: E402

OPS = "urn:li:corpuser:ops"


@pytest.fixture()
def replicas(tmp_path):
    from pyiceberg.catalog.sql import SqlCatalog

    engine = create_engine(f"sqlite:///{tmp_path}/s.db")
    init_db(engine)
    cat = SqlCatalog("t", uri=f"sqlite:///{tmp_path}/cat.db", warehouse=f"file://{tmp_path}/wh")
    cat.create_namespace("web")

    def replica():
        s = Streams(engine, {}, {})
        s._catalog = lambda: cat
        s._channel_locks = {}               # each replica's process has its own
        return s

    a, b = replica(), replica()
    a.create({"name": "web-events", "format": "http", "target": "web.events", "create_table": True}, OPS)
    return a, b, cat


def _count(cat) -> int:
    return cat.load_table(("web", "events")).scan().to_arrow().num_rows


def test_a_request_taken_by_two_replicas_at_once_is_written_once(replicas):
    a, b, cat = replicas
    rows = [{"id": i, "page": f"/p{i}"} for i in range(3)]
    assert a.ingest("web-events", "producer-1", rows, 0)["written"] == 3

    # Replica A reads the channel's offset (3); before it commits, replica B commits the same retry.
    more = [{"id": i, "page": f"/p{i}"} for i in range(3, 6)]
    write = a.write

    def b_first(stream, msgs, reject=None):
        assert b.ingest("web-events", "producer-1", more, 3)["written"] == 3
        return write(stream, msgs, reject=reject)

    a.write = b_first
    try:
        out = a.ingest("web-events", "producer-1", more, 3)
        assert out["written"] == 0
    except StreamError as e:                 # or refused as a conflict, for the producer to retry
        assert e.status == 409
    assert _count(cat) == 6
    assert a.channel_offset(a.by_name("web-events"), "producer-1") == 6
