"""MongoDB mirrors, the parts that need no server: where a run leaves the mirror's resume point. The end-to-end test
against a real replica set is ``test_mirror_mongo_e2e.py``."""

import json

import pytest

pytest.importorskip("pymongo")

from bson.timestamp import Timestamp  # noqa: E402

from cairn.pipelines import mirror_mongo as mg  # noqa: E402


class Stream:
    """A change stream with ``events``, then nothing; its post-batch resume token is ``token``."""

    def __init__(self, events, token):
        self.events, self.resume_token = list(events), token

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def try_next(self):
        return self.events.pop(0) if self.events else None


class Client(dict):
    def __init__(self, stream):
        super().__init__()
        self.stream = stream

    def __getitem__(self, name):
        stream = self.stream
        return {"orders": type("C", (), {"watch": lambda self, **kw: stream})()}


def test_a_quiet_collection_moves_its_resume_point_on():
    # No change to this collection: the stream's post-batch token is past everything read. Kept at the copy's
    # time instead, the mirror would fail as purged once the oplog of a busy cluster rolled past it.
    _, end = mg.changes(Client(Stream([], {"_data": "82AB"})), "shop", "orders", "time:100:1")
    assert end == json.dumps({"_data": "82AB"})


def test_changes_then_quiet_end_at_the_post_batch_token():
    ev = {"_id": {"_data": "8201"}, "operationType": "insert", "clusterTime": Timestamp(100, 2),
          "fullDocument": {"_id": 1, "v": "a"}}
    got, end = mg.changes(Client(Stream([ev], {"_data": "8202"})), "shop", "orders", "time:100:1")
    assert [op for _, op, _ in got] == ["upsert"] and end == json.dumps({"_data": "8202"})
