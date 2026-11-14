"""MongoDB as a mirror source: a collection kept in a lakehouse table through change streams.

Used by :class:`cairn.pipelines.mirror.Mirrors` for connections whose URL is ``mongodb://`` (or ``mongodb+srv://``);
the mirror's table is ``database.collection``. Change streams need a replica set or a sharded cluster (a single
node started with ``--replSet`` will do). The connection's user needs ``find`` and ``changeStream`` on the
collection (the ``read`` role on its database). Cairn changes nothing on the server.

Each document becomes a row keyed by ``_id`` (an ObjectId as its hex string). Top-level fields are columns; embedded
documents and arrays are kept as JSON text; a field that appears later is added as a column. The copy starts from
the cluster's operation time read before it, so the changes after it converge on the copy (a change replayed over
it writes the same row again). Inserts, replacements and updates write the document as it is now (an update reads
the current document), deletes delete. A dropped or renamed collection ends the stream: the mirror fails and says
to recreate it.

Where a mirror has read to is its resume token, recorded after each lakehouse commit; offsets are cluster times,
so a crash between the commit and that record replays changes that leave the table the same. The server keeps
changes as long as its oplog does: a mirror paused for longer finds its token gone, fails and says so.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Optional

MAX_ROWS = 20_000
URL = re.compile(r"^mongodb(\+srv)?://")


def connect(url: str):
    from pymongo import MongoClient

    return MongoClient(url, serverSelectionTimeoutMS=10_000, connectTimeoutMS=10_000, tz_aware=True)


def value(v: Any) -> Any:
    """A BSON value as a lakehouse value."""
    from bson import Decimal128, ObjectId
    from bson.timestamp import Timestamp

    if isinstance(v, ObjectId):
        return str(v)
    if isinstance(v, Decimal128):
        return float(v.to_decimal())
    if isinstance(v, Timestamp):
        return v.as_datetime()
    if isinstance(v, dt.datetime):
        return v.astimezone(dt.timezone.utc) if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=lambda x: value(x) if not isinstance(x, (dict, list)) else str(x), sort_keys=True)
    if isinstance(v, bytes):
        return v.hex()
    return v


def row(doc: dict) -> dict:
    return {str(k): value(v) for k, v in doc.items()}


def check(client, database: str, collection: str) -> list[str]:
    """``["_id"]``, after checking change streams can be opened on the collection."""
    hello = client.admin.command("hello")
    if not hello.get("setName") and hello.get("msg") != "isdbgrid":
        raise ValueError("change streams need a replica set or a sharded cluster (start mongod with --replSet)")
    if collection not in client[database].list_collection_names():
        raise ValueError(f"no collection {database}.{collection}")
    return ["_id"]


def position(client) -> str:
    """Where changes after a copy taken now start: the cluster's current operation time."""
    t = client.admin.command("hello").get("operationTime") or client.admin.command("ping").get("operationTime")
    if t is None:
        raise RuntimeError("the server reports no operation time (is it a replica set?)")
    return f"time:{t.time}:{t.inc}"


def copy(client, database: str, collection: str, batch: int = 5000):
    """The collection's documents, in batches of rows."""
    out = []
    for doc in client[database][collection].find({}, batch_size=batch):
        out.append(row(doc))
        if len(out) >= batch:
            yield out
            out = []
    if out:
        yield out


def offset(cluster_time) -> int:
    return (int(cluster_time.time) << 32) | int(cluster_time.inc)


def changes(client, database: str, collection: str, mark: str) -> tuple[list, Optional[str]]:
    """Changes after ``mark`` (a resume token, or ``time:<t>:<i>`` after a copy): ``([(offset, op, row)], the mark
    after the last whole transaction read, or None)``. ``op`` is upsert or delete."""
    from bson.timestamp import Timestamp
    from pymongo.errors import OperationFailure

    kw: dict[str, Any] = {"full_document": "updateLookup", "max_await_time_ms": 300}
    if mark.startswith("time:"):
        _, t, i = mark.split(":")
        kw["start_at_operation_time"] = Timestamp(int(t), int(i))
    else:
        kw["start_after"] = json.loads(mark)
    out: list = []
    end: Optional[str] = None
    try:
        with client[database][collection].watch(**kw) as stream:
            txn = None
            while True:
                ev = stream.try_next()
                if ev is None:
                    break
                op = ev["operationType"]
                if op in ("drop", "rename", "dropDatabase", "invalidate"):
                    raise RuntimeError(f"the collection's change stream ended ({op}: dropped or renamed): "
                                       "delete the mirror and create it again")
                at = offset(ev["clusterTime"])
                this = (str(ev.get("lsid")), ev.get("txnNumber")) if ev.get("txnNumber") is not None else None
                if len(out) >= MAX_ROWS and (this is None or this != txn):
                    break                                  # stop between transactions
                txn = this
                if op in ("insert", "replace", "update"):
                    doc = ev.get("fullDocument")
                    if doc is not None:                    # None: deleted since; its delete follows
                        out.append((at, "upsert", row(doc)))
                elif op == "delete":
                    out.append((at, "delete", {"_id": value(ev["documentKey"]["_id"])}))
                end = json.dumps(ev["_id"])
    except OperationFailure as e:
        if e.code in (136, 280, 286) or "resume" in str(e).lower() or "oplog" in str(e).lower():
            raise RuntimeError("the changes this mirror had read to are gone from the server's oplog: delete the "
                               f"mirror and create it again to copy the collection afresh ({e})")
        raise
    return out, end
