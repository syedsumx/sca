"""DynamoDB mirrors (L167) without AWS: URLs, attribute values, and the stream reader against a scripted DynamoDB
Streams client (a parent shard read to its end before its children, trimmed positions, shards gone). The mirror
runs against DynamoDB Local in ``test_mirror_dynamodb_e2e.py``."""

import pytest

from cairn.pipelines import mirror_dynamodb as dyn


def test_urls():
    assert dyn.settings("dynamodb://eu-west-1") == {"key": None, "secret": None, "endpoint": None,
                                                    "region": "eu-west-1"}
    s = dyn.settings("dynamodb://AK:S%2Fecret@localhost:8000?region=us-east-1&tls=false")
    assert s == {"key": "AK", "secret": "S/ecret", "endpoint": "http://localhost:8000", "region": "us-east-1"}
    assert dyn.settings("dynamodb://vpce.example:443?region=eu-west-2")["endpoint"] == "https://vpce.example:443"
    for bad in ("dynamodb://not a region", "dynamodb://localhost:8000?region=x", "postgresql://h/db"):
        with pytest.raises(ValueError):
            dyn.settings(bad)


def test_values():
    item = {"pk": {"S": "a"}, "sk": {"N": "10.50"}, "n": {"N": "1e3"}, "b": {"B": b"\x01\x02"}, "t": {"BOOL": False},
            "z": {"NULL": True}, "m": {"M": {"x": {"N": "1"}, "y": {"BS": [b"\x00"]}}}, "ns": {"NS": ["2", "1.5"]}}
    r = dyn.row(item, ["pk", "sk"])
    assert r["sk"] == "10.50" and r["n"] == 1000.0 and r["b"] == "0102" and r["t"] is False and r["z"] is None
    assert r["m"] == '{"x": 1, "y": ["AA=="]}' and r["ns"] == "[1.5, 2]"


class Trimmed(Exception):
    pass


class Stream:
    """A scripted DynamoDB Streams client: shards with parents, records and an end for closed ones."""

    def __init__(self, shards, trimmed=()):
        self.shards, self.trimmed, self.calls = shards, set(trimmed), []

    def describe_stream(self, StreamArn, Limit, ExclusiveStartShardId=None):
        return {"StreamDescription": {"Shards": [
            {"ShardId": sid, **({"ParentShardId": s["parent"]} if s.get("parent") else {}),
             "SequenceNumberRange": {"StartingSequenceNumber": "0",
                                     **({"EndingSequenceNumber": "9"} if s["closed"] else {})}}
            for sid, s in self.shards.items()]}}

    def get_shard_iterator(self, StreamArn, ShardId, ShardIteratorType, SequenceNumber=None):
        from botocore.exceptions import ClientError

        if (ShardId, SequenceNumber) in self.trimmed:
            raise ClientError({"Error": {"Code": "TrimmedDataAccessException", "Message": "trimmed"}}, "GetShardIterator")
        records = self.shards[ShardId]["records"]
        start = 0 if SequenceNumber is None else [r[0] for r in records].index(SequenceNumber) + 1
        return {"ShardIterator": f"{ShardId}|{start}"}

    def get_records(self, ShardIterator, Limit):
        sid, start = ShardIterator.split("|")
        self.calls.append(sid)
        s = self.shards[sid]
        part = s["records"][int(start):int(start) + 2]           # two at a time: several pages per shard
        nxt = int(start) + len(part)
        out = {"Records": [{"eventName": ev, "dynamodb": {"SequenceNumber": seq, "Keys": {"pk": {"S": k}},
                                                          **({"NewImage": {"pk": {"S": k}, "v": {"N": str(v)}}}
                                                             if ev != "REMOVE" else {})}}
                           for seq, ev, k, v in part]}
        if not (s["closed"] and nxt >= len(s["records"])):
            out["NextShardIterator"] = f"{sid}|{nxt}"
        return out


def test_a_parent_is_read_to_its_end_before_its_children():
    s = Stream({
        "shard-b": {"parent": "shard-a", "closed": False, "records": [("5", "MODIFY", "k", 3), ("6", "REMOVE", "j", 0)]},
        "shard-a": {"parent": None, "closed": True,
                    "records": [("1", "INSERT", "k", 1), ("2", "INSERT", "j", 1), ("3", "MODIFY", "k", 2)]},
    })
    got, state = dyn.changes(s, "arn", ["pk"], {})
    assert [(op, r["pk"], r.get("v")) for op, r in got] == [("upsert", "k", 1.0), ("upsert", "j", 1.0),
                                                           ("upsert", "k", 2.0), ("upsert", "k", 3.0),
                                                           ("delete", "j", None)]
    assert state == {"shard-a": {"seq": "3", "done": True}, "shard-b": {"seq": "6", "done": False}}
    # Nothing new: an open shard caught up, a closed one not read again.
    got, state2 = dyn.changes(s, "arn", ["pk"], state)
    assert got == [] and state2 == state and "shard-a" not in s.calls[-3:]


def test_a_child_waits_for_an_unfinished_parent_and_a_trimmed_parent_is_skipped():
    s = Stream({"child": {"parent": "gone-parent", "closed": False, "records": [("7", "INSERT", "x", 1)]}})
    got, _ = dyn.changes(s, "arn", ["pk"], {})                   # a parent the mirror never saw: trimmed before it
    assert [r["pk"] for _, r in got] == ["x"]
    s = Stream({"p": {"parent": None, "closed": True, "records": [("1", "INSERT", "a", 1), ("2", "INSERT", "b", 1),
                                                                  ("3", "INSERT", "c", 1)]},
                "c": {"parent": "p", "closed": False, "records": [("4", "INSERT", "d", 1)]}})
    got, state = dyn.changes(s, "arn", ["pk"], {}, max_rows=2)    # stops inside the parent
    assert [r["pk"] for _, r in got] == ["a", "b"] and state["c"] == {"seq": None, "done": False}
    got, state = dyn.changes(s, "arn", ["pk"], state)
    assert [r["pk"] for _, r in got] == ["c", "d"] and state["p"]["done"]


def test_positions_the_stream_no_longer_holds_restart_the_mirror():
    s = Stream({"a": {"parent": None, "closed": False, "records": [("1", "INSERT", "k", 1)]}}, trimmed={("a", "1")})
    with pytest.raises(dyn.Restart):
        dyn.changes(s, "arn", ["pk"], {"a": {"seq": "1", "done": False}})
    s = Stream({"b": {"parent": "a", "closed": False, "records": []}})
    with pytest.raises(dyn.Restart):                              # a shard half read, gone from the stream
        dyn.changes(s, "arn", ["pk"], {"a": {"seq": "1", "done": False}})
    got, state = dyn.changes(s, "arn", ["pk"], {"a": {"seq": "9", "done": True}})
    assert got == [] and "a" not in state                         # read to its end, gone: forgotten
