"""Amazon DynamoDB as a mirror source: a DynamoDB table kept in a lakehouse table through DynamoDB Streams.

Used by :class:`cairn.pipelines.mirror.Mirrors` for connections whose URL is ``dynamodb://``:

* ``dynamodb://eu-west-1``: AWS in that region, with the credentials of Cairn's API (its pod's role, the
  environment, or a profile: boto3's usual chain);
* ``dynamodb://ACCESS_KEY:SECRET_KEY@eu-west-1``: AWS with those keys;
* ``dynamodb://ACCESS_KEY:SECRET_KEY@host:8000?region=us-east-1&tls=false``: an endpoint of its own (DynamoDB
  Local, a VPC endpoint's name), when a port is given.

The mirror's table is the DynamoDB table's name. Its stream is turned on with new images (``NEW_IMAGE`` or
``NEW_AND_OLD_IMAGES``); Cairn changes nothing on AWS. The credentials need ``dynamodb:DescribeTable``, ``Scan``,
``DescribeStream``, ``GetShardIterator`` and ``GetRecords`` on the table and its stream.

* **The copy.** A strongly consistent ``Scan``. Changes are then read from the start of the stream's retention
  (``TRIM_HORIZON``, at most 24 hours back): each record carries the item as it became, so replaying records over
  the copy, in each item's order, ends with the table as the stream leaves it.
* **Changes.** Each run reads every shard from its last sequence number, a parent shard to its end before its
  children (DynamoDB keeps each item's changes in order along that lineage), writes them as one commit, and then
  records each shard's position (``cairn_mirror_shards``). A crash in between replays records that leave the table
  the same.
* **Items as rows.** The table's key attributes are the key; every top-level attribute is a column (a new one is
  added when it first appears). Strings, booleans and nulls as they are; numbers as doubles, except key attributes,
  kept as their exact text; binary as hex; maps, lists and sets as JSON text.
* **When the stream has moved on.** Records live 24 hours. A mirror that has not run for 23 hours, a position that
  has been trimmed, or a stream turned off and on again (a new stream) makes the mirror copy the table again; the
  audit log says why.
"""

from __future__ import annotations

import base64
import json
import re
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

MAX_ROWS = 20_000
URL = re.compile(r"^dynamodb://")
TABLE = re.compile(r"^[A-Za-z0-9_.\-]{3,255}$")
REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
RETENTION_MS = 23 * 3600 * 1000           # the stream keeps 24 hours: an hour's margin
STREAM = ("turn on the table's stream with new images: aws dynamodb update-table --table-name {table} "
          "--stream-specification StreamEnabled=true,StreamViewType=NEW_AND_OLD_IMAGES")


class Restart(Exception):
    """The stream no longer holds every change after the mirror's position: the table is copied again."""


def settings(url: str) -> dict:
    if not URL.match(url or ""):
        raise ValueError("not a dynamodb:// URL")
    u = urlsplit(url)
    params = {k: v[0] for k, v in parse_qs(u.query).items()}
    host = u.hostname or ""
    out = {"key": unquote(u.username or "") or None, "secret": unquote(u.password or "") or None, "endpoint": None}
    if u.port:
        tls = params.get("tls", "true").lower() != "false"
        out["endpoint"] = f"{'https' if tls else 'http'}://{host}:{u.port}"
        out["region"] = params.get("region", "us-east-1")
    else:
        out["region"] = host
    if not REGION.match(out["region"] or ""):
        raise ValueError("a dynamodb:// URL names an AWS region (dynamodb://eu-west-1), or a host and port with "
                         "?region=")
    return out


def clients(url: str):
    """``(dynamodb, dynamodbstreams)`` clients."""
    import boto3
    from botocore.config import Config

    s = settings(url)
    kw: dict[str, Any] = {"region_name": s["region"],
                          "config": Config(connect_timeout=10, read_timeout=60, retries={"max_attempts": 4})}
    if s["key"]:
        kw.update(aws_access_key_id=s["key"], aws_secret_access_key=s["secret"])
    if s["endpoint"]:
        kw["endpoint_url"] = s["endpoint"]
    session = boto3.session.Session()
    return session.client("dynamodb", **kw), session.client("dynamodbstreams", **kw)


def check(ddb, table: str) -> tuple[list[str], dict, str]:
    """``(key attributes, their types (S, N or B), the stream's ARN)``, after checking the stream carries new images."""
    from botocore.exceptions import ClientError

    if not TABLE.match(table):
        raise ValueError("a DynamoDB table's name: 3 to 255 letters, digits, '_', '-' and '.'")
    try:
        t = ddb.describe_table(TableName=table)["Table"]
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            raise ValueError(f"no DynamoDB table {table} in this region") from None
        if code in ("AccessDeniedException", "UnrecognizedClientException"):
            raise ValueError(f"the credentials may not describe table {table} (dynamodb:DescribeTable): {code}") from None
        raise
    key = [k["AttributeName"] for k in sorted(t["KeySchema"], key=lambda k: k["KeyType"] != "HASH")]
    types = {a["AttributeName"]: a["AttributeType"] for a in t.get("AttributeDefinitions", [])}
    spec = t.get("StreamSpecification") or {}
    if not spec.get("StreamEnabled") or not t.get("LatestStreamArn"):
        raise ValueError(f"table {table} has no stream: " + STREAM.format(table=table))
    if spec.get("StreamViewType") not in ("NEW_IMAGE", "NEW_AND_OLD_IMAGES"):
        raise ValueError(f"table {table}'s stream carries {spec.get('StreamViewType')}, not the items' new images: "
                         "turn it off, then " + STREAM.format(table=table))
    return key, {k: types.get(k, "S") for k in key}, t["LatestStreamArn"]


# ---- values ------------------------------------------------------------------------------------------
def _plain(v: Any) -> Any:
    """A deserialized value inside a map, list or set, as JSON can hold it."""
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() and abs(v) < 2 ** 63 else float(v)
    if isinstance(v, (bytes, bytearray)):
        return base64.b64encode(bytes(v)).decode()
    if hasattr(v, "value") and isinstance(v.value, (bytes, bytearray)):     # boto3's Binary
        return base64.b64encode(bytes(v.value)).decode()
    if isinstance(v, (set, frozenset)):
        return sorted((_plain(x) for x in v), key=lambda x: (str(type(x)), x))
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


def value(attr: dict, key: bool = False) -> Any:
    """One attribute value (DynamoDB's typed form, ``{"N": "1.5"}``) as a lakehouse value."""
    (t, v), = attr.items()
    if t == "S":
        return v
    if t == "N":
        return v if key else float(Decimal(v))
    if t == "B":
        return (base64.b64decode(v) if isinstance(v, str) else bytes(v)).hex()
    if t == "BOOL":
        return bool(v)
    if t == "NULL":
        return None
    from boto3.dynamodb.types import TypeDeserializer

    return json.dumps(_plain(TypeDeserializer().deserialize(attr)), sort_keys=True)


def row(item: dict, key: list[str]) -> dict:
    return {name: value(attr, name in key) for name, attr in item.items()}


def copy(ddb, table: str, key: list[str], batch: int = 5000):
    """The table's items (a strongly consistent scan), in batches of rows."""
    out, start = [], None
    while True:
        kw: dict[str, Any] = {"TableName": table, "ConsistentRead": True}
        if start:
            kw["ExclusiveStartKey"] = start
        page = ddb.scan(**kw)
        for item in page.get("Items", []):
            out.append(row(item, key))
            if len(out) >= batch:
                yield out
                out = []
        start = page.get("LastEvaluatedKey")
        if not start:
            break
    if out:
        yield out


# ---- the stream ------------------------------------------------------------------------------------
def shards(streams, arn: str) -> list[dict]:
    """The stream's shards: ``{"id", "parent", "closed"}``."""
    out, start = [], None
    while True:
        kw: dict[str, Any] = {"StreamArn": arn, "Limit": 100}
        if start:
            kw["ExclusiveStartShardId"] = start
        d = streams.describe_stream(**kw)["StreamDescription"]
        for s in d.get("Shards", []):
            out.append({"id": s["ShardId"], "parent": s.get("ParentShardId"),
                        "closed": bool(s.get("SequenceNumberRange", {}).get("EndingSequenceNumber"))})
        start = d.get("LastEvaluatedShardId")
        if not start:
            return out


def changes(streams, arn: str, key: list[str], state: dict, max_rows: int = MAX_ROWS) -> tuple[list, dict]:
    """Records after each shard's position, a parent's before its children's: ``([(op, row)], new state)``. ``state``
    maps a shard to ``{"seq": last sequence number read or None, "done": read to its end}``; the new state holds
    every shard the stream still lists, and those the old one had not finished."""
    from botocore.exceptions import ClientError

    listed = shards(streams, arn)
    ids = {s["id"] for s in listed}
    state = {k: dict(v) for k, v in state.items()}
    lost = [k for k, v in state.items() if k not in ids and not v["done"]]
    if lost:
        raise Restart(f"the stream no longer lists shard {lost[0]}, which this mirror had not read to its end")
    for s in listed:
        state.setdefault(s["id"], {"seq": None, "done": False})
    by_id = {s["id"]: s for s in listed}
    out: list = []

    def ready(sid: str) -> bool:
        parent = by_id[sid]["parent"]
        if parent is None or (parent not in ids and parent not in state):
            return True                                  # no parent, or one trimmed away before this mirror saw it
        return bool(state.get(parent, {}).get("done"))

    progress = True
    while progress and len(out) < max_rows:
        progress = False
        for s in sorted(listed, key=lambda s: s["id"]):
            st = state[s["id"]]
            if st["done"] or not ready(s["id"]):
                continue
            kw: dict[str, Any] = {"StreamArn": arn, "ShardId": s["id"]}
            if st["seq"]:
                kw.update(ShardIteratorType="AFTER_SEQUENCE_NUMBER", SequenceNumber=st["seq"])
            else:
                kw["ShardIteratorType"] = "TRIM_HORIZON"
            try:
                it = streams.get_shard_iterator(**kw).get("ShardIterator")
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "TrimmedDataAccessException":
                    raise Restart(f"the stream no longer holds shard {s['id']} after {st['seq']}") from None
                raise
            empty = 0
            while it and len(out) < max_rows:
                got = streams.get_records(ShardIterator=it, Limit=1000)
                records = got.get("Records", [])
                for rec in records:
                    d = rec["dynamodb"]
                    if rec["eventName"] == "REMOVE":
                        out.append(("delete", row(d["Keys"], key)))
                    else:
                        out.append(("upsert", row(d["NewImage"], key)))
                    st["seq"] = d["SequenceNumber"]
                    progress = True
                it = got.get("NextShardIterator")
                if it is None:
                    st["done"] = True                    # read to the end of a closed shard
                    progress = True
                    break
                if not records:
                    empty += 1
                    if not s["closed"] and empty >= 3:
                        break                            # caught up on an open shard
                else:
                    empty = 0
    # Shards the stream no longer lists are forgotten once read to their end.
    state = {k: v for k, v in state.items() if k in ids or not v["done"]}
    return out, state
