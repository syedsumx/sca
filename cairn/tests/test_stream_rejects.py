"""A stream that tokenises columns sets bad messages aside without those columns in clear."""

import json
import types

from sqlalchemy import create_engine

from cairn.common.db import init_db
from cairn.pipelines.streams import WITHHELD, Streams


def msg(offset, value):
    return types.SimpleNamespace(partition=0, offset=offset, key=None, value=value)


def test_rejects_of_a_tokenising_stream_withhold_its_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    s = Streams(engine, {}, {})
    stream = {"id": "s1", "name": "cdc", "topic": "pg.customers", "format": "debezium", "mode": "upsert",
              "key": ["id"], "encoding": "json", "tokenize": {"email": "customer"}, "transform": None}
    change = {"payload": {"op": "c", "before": None, "after": {"id": None, "email": "jane@acme.com", "tier": "gold"}}}
    assert s.write(stream, [msg(0, json.dumps(change).encode()), msg(1, b"jane@acme.com, not json")]) == 0
    got = {r["offset"]: r for r in s.rejects("s1")}
    assert set(got) == {0, 1}
    assert "jane@acme.com" not in json.dumps(got)
    assert json.loads(got[0]["value"])["payload"]["after"] == {"id": None, "email": WITHHELD, "tier": "gold"}
    assert got[1]["value"] is None and got[1]["reason"]


def test_rejects_of_other_streams_keep_the_message(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    s = Streams(engine, {}, {})
    stream = {"id": "s2", "name": "clicks", "topic": "clicks", "format": "json", "mode": "upsert", "key": ["id"],
              "encoding": "json", "tokenize": {}, "transform": None}
    s.write(stream, [msg(0, b'{"email": "jane@acme.com"}')])
    assert json.loads(s.rejects("s2")[0]["value"]) == {"email": "jane@acme.com"}


def test_transform_rejects_of_a_tokenising_stream_keep_nothing_in_clear(tmp_path):
    # The transform's input is filed before tokenisation, under the input's names: email feeds the tokenised contact.
    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    s = Streams(engine, {}, {})
    s._mark = lambda stream, props: None                  # no table here: nothing is left to write
    stream = {"id": "s3", "name": "signups", "topic": "http.web", "format": "json", "mode": "append", "key": [],
              "encoding": "json", "tokenize": {"contact": "customer"},
              "transform": "SELECT CAST(n AS INTEGER) AS n, email AS contact FROM batch"}
    row = {"n": "jane@acme.com", "email": "jane@acme.com"}
    assert s.write(stream, [msg(0, json.dumps(row).encode())]) == 0
    got = s.rejects("s3")
    assert len(got) == 1 and got[0]["value"] is None and got[0]["reason"].startswith("transform:")
    assert "jane@acme.com" not in json.dumps(got)
