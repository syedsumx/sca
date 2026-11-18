"""Streams: Kafka topics written into lakehouse tables continuously, change events (CDC) included.

A stream names a Kafka ``topic``, a ``format`` and the Iceberg ``target`` (``namespace.table``):

* ``json``: each message is a JSON object, one row. In ``append`` mode rows are added; in ``upsert`` mode
  the row replaces the one with the same ``key`` columns, and a tombstone (no value) whose Kafka key is a
  JSON object of the key columns deletes it.
* Messages are JSON, or with ``encoding: avro`` Avro in the Confluent wire format (a magic byte, the
  schema's id in a Schema Registry, the Avro body), as Debezium's Avro converter writes them. Schemas
  come from the registry (``CAIRN_STREAMS_SCHEMA_REGISTRY``: ``{"url", "username", "password", "ca_file"}``)
  and are kept once read; a message that is not in that format, or names a schema the registry does not
  know, is set aside like any other.
* ``debezium``: Debezium change events (with or without the schema envelope). Creates, updates and
  snapshot reads write the ``after`` image, deletes remove the ``before`` image's key, so the table
  follows the source database. Always ``upsert``.

Exactly once: each batch is one Iceberg commit, and the commit's snapshot carries the next Kafka offset
of every partition it covers (``cairn.stream.<id>.<topic>.<partition>``). A worker that takes over a
partition starts from there, so a crash between the table commit and the Kafka offset commit re-reads
nothing that is already in the table. (Kafka's committed offsets are a fallback, for when those
snapshots have been expired.)

A message that cannot be a row (not JSON, unknown operation, columns the table lacks without
``evolve``, no key) is set aside in ``cairn_stream_rejects`` with its offset and the reason; the stream
goes on. A batch the table refuses (say, a type that does not convert) stops the stream with the error
on its status and is retried with back-off: nothing is skipped silently. With ``bad_rows: quarantine``
the rows the table refuses are set aside there too, and the rest of the batch commits.

Transform (``transform``): one SQL SELECT over ``batch`` (the incoming rows) applied to each batch before it
lands, in-process (``cairn.pipelines.stream_transform``): projection, renames, casts, filters, row-level
expressions. It is checked when the stream is defined; a row it fails on is set aside with the rejects, a row its
WHERE drops is skipped, and the batch's offsets are still recorded (an empty commit when nothing is left).

Over HTTP (``format: http``): no Kafka. Producers post rows to ``POST /v1/ingest/<name>/rows`` with a
``channel`` (one per producer) and the channel ``offset`` of the first row; each request is one commit
whose snapshot records the channel's next offset, so a retried request writes nothing twice
(``GET /v1/ingest/<name>/channels/<channel>`` says where to resume). Posting needs ``INGEST_STREAM`` on
``urn:li:stream:<name>``. One writer per channel at a time.

Workers: ``cairn streams`` runs every enabled stream. Several replicas share the partitions (a Kafka
consumer group per stream). Settings: ``CAIRN_STREAMS_KAFKA`` (kafka-python consumer settings:
bootstrap_servers, security_protocol, sasl_*, ssl_cafile ...) and ``CAIRN_STREAMS_CATALOG`` (pyiceberg
REST catalog properties; default ``CAIRN_PIPES_CATALOG``), each JSON or a path to a JSON file.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
import warnings
from functools import reduce
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, delete, select, update

from cairn.common.db import metadata
from cairn.pipelines.pipes import (BAD_ROWS, TARGET, PipeError, check_not_held, check_resident, check_tokenize, fit,
                                   fit_rows, open_table)

logger = logging.getLogger(__name__)

streams = Table(
    "cairn_streams", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(256), nullable=False, unique=True),
    Column("topic", String(249), nullable=False),
    Column("format", String(16), nullable=False),           # json | debezium
    Column("mode", String(16), nullable=False),             # append | upsert
    Column("key", String(1024), nullable=False, default=""),  # comma-separated key columns (upsert)
    Column("target", String(512), nullable=False),
    Column("create_table", Boolean, nullable=False, default=False),
    Column("evolve", Boolean, nullable=False, default=False),   # add new columns instead of rejecting
    Column("encoding", String(16), nullable=False, default="json"),  # json | avro (Confluent wire format)
    Column("start", String(16), nullable=False, default="earliest"),
    Column("source", String(1024), nullable=True),         # the dataset the topic carries (lineage)
    Column("batch_rows", Integer, nullable=False, default=5000),
    Column("batch_seconds", Integer, nullable=False, default=10),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("created_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("bad_rows", String(16), nullable=False, server_default="fail"),   # fail | quarantine
    Column("tokenize", Text, nullable=True),                # JSON {column: token domain} (cairn.governance.tokens)
    Column("transform", Text, nullable=True),               # a SELECT over each batch before it lands (0125)
)

stream_status = Table(
    "cairn_stream_status", metadata,
    Column("stream_id", String(32), primary_key=True),
    Column("status", String(16), nullable=False),           # running | error
    Column("rows", BigInteger, nullable=False, default=0),
    Column("lag", BigInteger, nullable=True),
    Column("last_commit_ms", BigInteger, nullable=True),
    Column("error", String(2000), nullable=True),
    Column("updated_ms", BigInteger, nullable=False),
)

stream_rejects = Table(
    "cairn_stream_rejects", metadata,
    Column("stream_id", String(32), primary_key=True),
    Column("partition", Integer, primary_key=True),
    Column("offset", BigInteger, primary_key=True),
    Column("reason", String(1000), nullable=False),
    Column("value", Text, nullable=True),                   # the first 4 KiB, to see what it was
    Column("rejected_ms", BigInteger, nullable=False, index=True),
)

FORMATS = ("json", "debezium", "http")
CHANNEL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MAX_HTTP_ROWS = 10_000
ENCODINGS = ("json", "avro")
MODES = ("append", "upsert")
TOPIC = re.compile(r"^[A-Za-z0-9._-]{1,249}$")
COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
PROP = "cairn.stream."


class StreamError(PipeError):
    pass


def _now() -> int:
    return int(time.time() * 1000)


def _stream(r) -> dict:
    return {"id": r.id, "name": r.name, "topic": r.topic, "format": r.format, "mode": r.mode,
            "key": [k for k in r.key.split(",") if k], "target": r.target, "create_table": bool(r.create_table),
            "evolve": bool(r.evolve),
            "encoding": getattr(r, "encoding", None) or "json", "start": r.start, "source": r.source,
            "batch_rows": r.batch_rows, "batch_seconds": r.batch_seconds,
            "enabled": bool(r.enabled), "created_by": r.created_by, "created_ms": r.created_ms,
            "bad_rows": getattr(r, "bad_rows", None) or "fail",
            "tokenize": json.loads(r.tokenize) if getattr(r, "tokenize", None) else {},
            "transform": getattr(r, "transform", None) or None}


class Reject(Exception):
    """One message that cannot be a row."""


PRIMITIVE_ZERO = {"null": True, "boolean": False, "int": False, "long": False, "float": False, "double": False,
                  "bytes": False, "string": False}


def _empty_items(schema: Any) -> bool:
    """Does the Avro ``schema`` have an array whose items may be encoded in no bytes (null, a fixed of size 0, a
    record of only such fields)? A block count is then all a message needs to make the decoder build any number
    of items. (A map's keys are strings, at least a byte each; a union's branch index is a byte.)"""
    named: dict[str, Any] = {}
    memo: dict[str, bool] = {}

    def collect(t: Any, ns: str = "") -> None:
        if isinstance(t, list):
            for x in t:
                collect(x, ns)
        elif isinstance(t, dict):
            if t.get("type") in ("record", "error", "enum", "fixed") and t.get("name"):
                name = str(t["name"])
                ns2 = str(t.get("namespace", ns) or "")
                full = name if "." in name or not ns2 else f"{ns2}.{name}"
                named.setdefault(name.rsplit(".", 1)[-1], t)
                named[full] = t
                ns = full.rsplit(".", 1)[0] if "." in full else ""
            for f in t.get("fields") or []:
                if isinstance(f, dict):
                    collect(f.get("type"), ns)
            for k in ("items", "values"):
                if k in t:
                    collect(t[k], ns)
            if isinstance(t.get("type"), (dict, list)):
                collect(t["type"], ns)

    def zero(t: Any, seen: frozenset = frozenset()) -> bool:
        if isinstance(t, str):
            if t in PRIMITIVE_ZERO:
                return PRIMITIVE_ZERO[t]
            if t in seen:
                return False                 # recursion only ends through a union, which takes a byte
            if t not in memo:
                memo[t] = zero(named.get(t) or named.get(t.rsplit(".", 1)[-1]) or "string", seen | {t})
            return memo[t]
        if isinstance(t, list):
            return len(t) == 0
        if not isinstance(t, dict):
            return False
        kind = t.get("type")
        if kind == "fixed":
            return t.get("size") == 0
        if kind in ("record", "error"):
            name = str(t.get("name") or "")
            return all(zero(f.get("type"), seen | {name}) for f in t.get("fields") or [] if isinstance(f, dict))
        if kind in ("array", "map", "enum"):
            return False
        return zero(kind, seen)

    def arrays(t: Any, seen: set) -> bool:
        if isinstance(t, list):
            return any(arrays(x, seen) for x in t)
        if isinstance(t, str):
            n = named.get(t)
            if n is None or t in seen:
                return False
            seen.add(t)
            return arrays(n, seen)
        if not isinstance(t, dict):
            return False
        kind = t.get("type")
        if kind == "array":
            return zero(t.get("items")) or arrays(t.get("items"), seen)
        if kind == "map":
            return arrays(t.get("values"), seen)
        if kind in ("record", "error"):
            return any(arrays(f.get("type"), seen) for f in t.get("fields") or [] if isinstance(f, dict))
        return isinstance(kind, (dict, list)) and arrays(kind, seen)

    collect(schema)
    return arrays(schema, set())


class Registry:
    """Avro schemas by id from a Confluent-compatible Schema Registry, kept once read."""

    def __init__(self, cfg: dict):
        self.url = str(cfg.get("url") or "").rstrip("/")
        self.auth = (cfg["username"], cfg.get("password", "")) if cfg.get("username") else None
        self.verify = cfg.get("ca_file") or True
        self._schemas: dict[int, Any] = {}
        self._lock = threading.Lock()

    def schema(self, sid: int):
        got = self._schema(sid)
        if isinstance(got, Reject):          # kept too: a bad schema is not fetched again for every message
            raise Reject(str(got))
        return got

    def _schema(self, sid: int):
        with self._lock:
            if sid in self._schemas:
                return self._schemas[sid]
        import fastavro
        import httpx

        try:
            r = httpx.get(f"{self.url}/schemas/ids/{sid}", auth=self.auth, verify=self.verify, timeout=15)
        except httpx.HTTPError as e:
            raise StreamError(f"the Schema Registry is not reachable: {e}", 503)   # the batch is retried
        if r.status_code == 404:
            raise Reject(f"schema {sid} is not in the Schema Registry")
        if r.status_code != 200:
            raise StreamError(f"the Schema Registry answered {r.status_code} for schema {sid}", 503)
        try:
            doc = r.json()
            if doc.get("schemaType", "AVRO") != "AVRO":
                raise Reject(f"schema {sid} is {doc.get('schemaType')}, not Avro")
            raw = json.loads(doc["schema"])
            parsed = fastavro.parse_schema(raw)
            if _empty_items(raw):
                # A five-byte message could claim billions of items of no bytes each, all built in memory.
                raise Reject(f"schema {sid} has an array of values encoded in no bytes (null, an empty record or "
                             "fixed); such messages are not read")
        except Reject as e:
            parsed = e
        except Exception as e:  # noqa: BLE001 - whatever the registry holds, it is one schema, not the worker
            parsed = Reject(f"schema {sid} is not an Avro schema Cairn can read: {e}")
        with self._lock:
            self._schemas[sid] = parsed
        return parsed

    def decode(self, raw: bytes, what: str) -> Any:
        import io

        import fastavro

        if len(raw) < 5 or raw[0] != 0:
            raise Reject(f"the {what} is not in the Confluent Avro format (magic byte, schema id, body)")
        schema = self.schema(int.from_bytes(raw[1:5], "big"))
        try:
            return fastavro.schemaless_reader(io.BytesIO(raw[5:]), schema)
        except Exception as e:  # noqa: BLE001 - a body that does not match its schema
            raise Reject(f"the {what} does not decode with schema {int.from_bytes(raw[1:5], 'big')}: {e}")


def parse(stream: dict, key: Optional[bytes], value: Optional[bytes],
          registry: Optional[Registry] = None) -> Optional[tuple[str, dict]]:
    """A message as ``("upsert" | "delete", row)``; None for a tombstone with nothing to do."""
    if stream["format"] == "pgmirror":                    # a PostgreSQL mirror's change, decoded already
        return value  # type: ignore[return-value]
    def obj(raw: bytes, what: str) -> dict:
        if stream.get("encoding") == "avro":
            if registry is None:
                raise StreamError("avro streams need a Schema Registry (CAIRN_STREAMS_SCHEMA_REGISTRY)", 503)
            v = registry.decode(raw, what)
        else:
            try:
                v = json.loads(raw)
            except ValueError:
                raise Reject(f"the {what} is not JSON")
        if not isinstance(v, dict):
            raise Reject(f"the {what} is not {'a record' if stream.get('encoding') == 'avro' else 'a JSON object'}")
        return v

    if value is None:                                     # tombstone
        if stream["mode"] == "upsert" and stream["format"] == "json" and key:
            k = obj(key, "key")
            k = k.get("payload", k) if isinstance(k.get("payload"), dict) else k
            return "delete", k
        return None
    v = obj(value, "value")
    if stream["format"] in ("json", "http"):
        return "upsert", v
    if isinstance(v.get("payload"), dict) and "op" in v["payload"]:
        v = v["payload"]                                  # the schema envelope
    op = v.get("op")
    if op == "d":
        before = v.get("before")
        if not isinstance(before, dict):
            raise Reject("a delete without a before image")
        return "delete", before
    if op in ("c", "u", "r"):
        after = v.get("after")
        if not isinstance(after, dict):
            raise Reject("a change without an after image")
        return "upsert", after
    if op == "t":
        raise Reject("truncate events are not applied")
    raise Reject(f"unknown operation {op!r}")


WITHHELD = "[tokenised: withheld]"


def _withhold(v: Any, cols: set) -> Any:
    if isinstance(v, dict):
        return {k: WITHHELD if str(k).lower() in cols else _withhold(x, cols) for k, x in v.items()}
    if isinstance(v, list):
        return [_withhold(x, cols) for x in v]
    return v


def _withholding(reject: Callable, cols: set) -> Callable:
    """``reject`` for a stream that tokenises ``cols``: the value it files has those columns withheld (at any depth:
    a change event's before and after images), and a value that is not JSON is not kept at all."""
    def filed(sid, partition, offset, reason, value):
        kept = None
        if value is not None:
            try:
                kept = json.dumps(_withhold(json.loads(value), cols), default=str).encode()
            except (ValueError, UnicodeDecodeError):
                kept = None
        return reject(sid, partition, offset, reason, kept)
    return filed


def key_filter(cols: list[str], keys: list[tuple]):
    from pyiceberg.expressions import And, EqualTo, In, Or

    if len(cols) == 1:
        return In(cols[0], {k[0] for k in keys})
    return reduce(Or, (reduce(And, (EqualTo(c, v) for c, v in zip(cols, k))) for k in keys))


def infer_schema(rows: list[dict]):
    """An arrow schema for rows that may not all have every column; all-null columns become strings."""
    import pyarrow as pa

    cols: dict[str, None] = {}
    for r in rows:
        cols.update(dict.fromkeys(r))
    data = pa.Table.from_pylist([{c: r.get(c) for c in cols} for r in rows])
    return pa.schema([pa.field(f.name, pa.string() if pa.types.is_null(f.type) else f.type) for f in data.schema])


class Streams:
    def __init__(self, engine, kafka: dict, catalog: dict, audit=None, changes=None,
                 urn: Optional[Callable[[str, str], str]] = None, graph=None, registry: Optional[dict] = None):
        """``graph``: Cairn Core (``get_aspect``, ``emit``), for the lineage from a stream's ``source``.
        ``registry``: the Schema Registry for Avro streams ({"url", "username", "password", "ca_file"})."""
        self.engine, self.kafka, self.catalog_props, self.audit, self.changes = engine, kafka, catalog, audit, changes
        self.graph = graph
        self.registry = registry or {}
        self.tokens = None                     # cairn.governance.tokens.Tokens, when tokenisation is configured
        self.medallion = None                  # cairn.pipelines.medallion.Medallion: layers on the targets
        self.residency = None                  # cairn.governance.residency.Residency, when namespaces are pinned
        self.held = None                       # held("ns.table"): a legal hold is on it (cairn.governance.records)
        self._registry = Registry(self.registry) if self.registry.get("url") else None
        self.urn = urn or (lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)")

    # ---- definitions --------------------------------------------------------------------
    def create(self, body: dict, actor: str) -> dict:
        from cairn.pipelines.medallion import layer_after, layer_before

        row = self._values(body, actor)
        target, source = row["target"], row["source"]
        layer = layer_before(self, body, target)
        with self.engine.begin() as conn:
            if conn.execute(select(streams.c.id).where(streams.c.name == row["name"])).first():
                raise StreamError("a stream with that name exists", 409)
            conn.execute(streams.insert().values(**row))
            if self.audit is not None:
                self.audit.record("lakehouse", "stream.created", actor, self.urn(*target.split(".")), row, conn=conn)
            if source:
                self._lineage(self.urn(*target.split(".")), source, actor)   # fails: nothing is created
        layer_after(self, layer, target, actor, f"stream:{row['name']}")
        return _stream(type("R", (), row))

    def update(self, sid: str, body: dict, actor: str) -> dict:
        """Changes a stream in place, keeping its id: its offsets (in the table's snapshots) carry on. A new
        target table starts from ``start``. Running workers pick the change up within their interval."""
        from cairn.pipelines.medallion import layer_after, layer_before

        cur = self.get(sid)
        row = {**self._values(body, actor), "id": sid, "created_by": cur["created_by"], "created_ms": cur["created_ms"]}
        layer = layer_before(self, body, row["target"])
        with self.engine.begin() as conn:
            if conn.execute(select(streams.c.id).where(streams.c.name == row["name"], streams.c.id != sid)).first():
                raise StreamError("a stream with that name exists", 409)
            conn.execute(update(streams).where(streams.c.id == sid).values(**row))
            if self.audit is not None:
                self.audit.record("lakehouse", "stream.updated", actor, self.urn(*row["target"].split(".")), row, conn=conn)
            if row["source"] and (row["source"] != cur["source"] or row["target"] != cur["target"]):
                self._lineage(self.urn(*row["target"].split(".")), row["source"], actor)
        layer_after(self, layer, row["target"], actor, f"stream:{row['name']}")
        return self.get(sid)

    def _values(self, body: dict, actor: str) -> dict:
        name, topic = str(body.get("name") or "").strip(), str(body.get("topic") or "")
        fmt, target = str(body.get("format") or "json"), str(body.get("target") or "")
        if fmt == "http":
            topic = "http"                                # rows arrive at POST /v1/ingest/<name>/rows
        mode = "upsert" if fmt == "debezium" else str(body.get("mode") or "append")
        key = body.get("key") or []
        key = [k.strip() for k in key.split(",")] if isinstance(key, str) else [str(k) for k in key]
        if not name or not TOPIC.match(topic):
            raise StreamError("a stream needs a name and a Kafka topic")
        if fmt not in FORMATS:
            raise StreamError(f"format is one of {', '.join(FORMATS)}")
        if mode not in MODES:
            raise StreamError(f"mode is one of {', '.join(MODES)}")
        if not TARGET.match(target):
            raise StreamError("target is namespace.table")
        check_resident(self.residency, target, StreamError, "target")
        if mode == "upsert" and not key:
            raise StreamError("upsert (and debezium) streams need the key columns")
        if not all(COLUMN.match(k) for k in key):
            raise StreamError("key columns are column names")
        encoding = str(body.get("encoding") or "json")
        if encoding not in ENCODINGS:
            raise StreamError(f"encoding is one of {', '.join(ENCODINGS)}")
        if encoding == "avro" and fmt == "http":
            raise StreamError("http streams take JSON rows")
        if encoding == "avro" and not self.registry.get("url"):
            raise StreamError("avro streams need a Schema Registry (CAIRN_STREAMS_SCHEMA_REGISTRY)", 503)
        start = str(body.get("start") or "earliest")
        if start not in ("earliest", "latest"):
            raise StreamError("start is earliest or latest")
        source = body.get("source") or None
        if source is not None and not (isinstance(source, str) and source.startswith("urn:li:dataset:(") and len(source) <= 1024):
            raise StreamError("source is the dataset URN the topic carries")
        if source is not None and self.graph is None:
            raise StreamError("lineage needs Cairn Core: this deployment has none", 503)
        bad_rows = str(body.get("bad_rows") or "fail")
        if bad_rows not in BAD_ROWS:
            raise StreamError("bad_rows is fail (the stream stops on a row the table refuses) or quarantine")
        tokenize = check_tokenize(self.tokens, body.get("tokenize"), actor, False, StreamError)
        transform = body.get("transform") or None
        if transform is not None:
            from cairn.pipelines.stream_transform import TransformError, check

            if fmt not in ("json", "http"):
                raise StreamError("transform applies to json and http streams (debezium changes land as they are)")
            try:
                transform = check(transform, key if mode == "upsert" else None, list(tokenize or {}))
            except TransformError as e:
                raise StreamError(str(e))
        rows, secs = body.get("batch_rows", 5000), body.get("batch_seconds", 10)
        if not isinstance(rows, int) or not 1 <= rows <= 1_000_000 or not isinstance(secs, int) or not 1 <= secs <= 3600:
            raise StreamError("batch_rows is 1-1000000 and batch_seconds 1-3600")
        row = {"id": uuid.uuid4().hex, "name": name[:256], "topic": topic, "format": fmt, "mode": mode,
               "key": ",".join(key), "target": target, "create_table": bool(body.get("create_table")),
               "evolve": bool(body.get("evolve")),
               "encoding": encoding, "start": start, "source": source, "batch_rows": rows, "batch_seconds": secs,
               "enabled": bool(body.get("enabled", True)), "created_by": actor, "created_ms": _now(),
               "bad_rows": bad_rows, "tokenize": json.dumps(tokenize) if tokenize else None, "transform": transform}
        return row

    def _lineage(self, urn: str, source: str, actor: str) -> None:
        from cairn.common.proposals import make_mcp

        cur = self.graph.get_aspect(urn, "upstreamLineage") or {}
        ups = list(cur.get("upstreams") or [])
        if any(u.get("dataset") == source for u in ups):
            return
        ups.append({"dataset": source, "type": "TRANSFORMED", "properties": {"cairn.stream": "true"},
                    "auditStamp": {"time": _now(), "actor": actor}})
        self.graph.emit([make_mcp(urn, "upstreamLineage", {**cur, "upstreams": ups})])

    def list(self) -> list[dict]:
        with self.engine.connect() as conn:
            defs = [_stream(r) for r in conn.execute(select(streams).order_by(streams.c.name))]
            st = {r.stream_id: dict(r._mapping) for r in conn.execute(select(stream_status))}
        for d in defs:
            s = st.get(d["id"], {})
            d["status"] = {k: s.get(k) for k in ("status", "rows", "lag", "last_commit_ms", "error", "updated_ms")}
        from cairn.pipelines.medallion import with_layers

        return with_layers(self, defs)

    def get(self, sid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(streams).where(streams.c.id == sid)).first()
        if r is None:
            raise StreamError("no such stream", 404)
        return _stream(r)

    def set_enabled(self, sid: str, enabled: bool, actor: str) -> dict:
        with self.engine.begin() as conn:
            if not conn.execute(update(streams).where(streams.c.id == sid).values(enabled=enabled)).rowcount:
                raise StreamError("no such stream", 404)
            if self.audit is not None:
                self.audit.record("lakehouse", "stream.enabled" if enabled else "stream.disabled", actor, None,
                                  {"stream": sid}, conn=conn)
        return self.get(sid)

    def delete(self, sid: str, actor: str) -> None:
        with self.engine.begin() as conn:
            if not conn.execute(delete(streams).where(streams.c.id == sid)).rowcount:
                raise StreamError("no such stream", 404)
            conn.execute(delete(stream_status).where(stream_status.c.stream_id == sid))
            conn.execute(delete(stream_rejects).where(stream_rejects.c.stream_id == sid))
            if self.audit is not None:
                self.audit.record("lakehouse", "stream.deleted", actor, None, {"stream": sid}, conn=conn)

    def rejects(self, sid: str, limit: int = 200) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(
                select(stream_rejects).where(stream_rejects.c.stream_id == sid)
                .order_by(stream_rejects.c.rejected_ms.desc()).limit(limit))]

    # ---- state --------------------------------------------------------------------------
    def _status(self, sid: str, **values) -> None:
        values["updated_ms"] = _now()
        with self.engine.begin() as conn:
            cur = conn.execute(select(stream_status).where(stream_status.c.stream_id == sid)).first()
            if cur is None:
                conn.execute(stream_status.insert().values(stream_id=sid, rows=values.pop("add_rows", 0),
                                                           **{"status": "running", **values}))
            else:
                add = values.pop("add_rows", 0)
                conn.execute(update(stream_status).where(stream_status.c.stream_id == sid)
                             .values(rows=stream_status.c.rows + add, **values))

    def _reject(self, sid: str, partition: int, offset: int, reason: str, value: Optional[bytes]) -> None:
        text = None if value is None else value[:4096].decode("utf-8", "replace")
        with self.engine.begin() as conn:
            if conn.execute(select(stream_rejects.c.offset).where(
                    stream_rejects.c.stream_id == sid, stream_rejects.c.partition == partition,
                    stream_rejects.c.offset == offset)).first() is None:
                conn.execute(stream_rejects.insert().values(stream_id=sid, partition=partition, offset=offset,
                                                            reason=reason[:1000], value=text, rejected_ms=_now()))

    def _catalog(self):
        from pyiceberg.catalog.rest import RestCatalog

        from cairn.pipelines.iceberg_v3 import enable

        enable()                                         # pyiceberg writes v3 tables too
        return RestCatalog("cairn-streams", **self.catalog_props)

    @staticmethod
    def table_offsets(table, sid: str, topic: str) -> dict[int, int]:
        """The next offset of each partition, from the table's snapshots (the latest that names it)."""
        prefix = f"{PROP}{sid}.{topic}."
        out: dict[int, int] = {}
        for snap in sorted(table.metadata.snapshots, key=lambda s: s.timestamp_ms):
            for k, v in (snap.summary.additional_properties if snap.summary else {}).items():
                # Only "<prefix><partition>": a topic whose name extends this one (orders.eu after orders) is not it.
                if k.startswith(prefix) and k[len(prefix):].isdigit():
                    out[int(k[len(prefix):])] = int(v)
        return out

    def offsets(self, stream: dict) -> dict[int, int]:
        from pyiceberg.exceptions import NoSuchTableError

        try:
            table = self._catalog().load_table(tuple(stream["target"].split(".")))
        except NoSuchTableError:
            return {}
        return self.table_offsets(table, stream["id"], stream["topic"])

    # ---- writing one batch --------------------------------------------------------------
    def write(self, stream: dict, messages: list, reject: Optional[Callable] = None) -> int:
        """Writes a batch of Kafka messages (``.partition .offset .key .value``) as one Iceberg commit whose
        snapshot records the next offset of each partition. Returns the rows written. ``reject`` files a
        message that cannot be a row (``_reject`` unless given)."""
        import pyarrow as pa
        from pyiceberg.exceptions import CommitFailedException, NoSuchTableError

        sid, key = stream["id"], stream["key"]
        # Before any message is read: a batch for a pinned namespace is processed only where its pin allows (it
        # stops the stream with the error and is retried, so nothing is skipped).
        check_resident(self.residency, stream["target"], StreamError, f"stream {stream.get('name') or sid}")
        reject = reject or self._reject
        spec = stream.get("tokenize") or {}
        filed = reject                                    # files a value as given (a tokenised row's tokens stay)
        if spec:                                          # a set-aside message keeps no tokenised column in clear
            reject = _withholding(reject, {c.lower() for c in spec})

        def early(p, o, reason: str, row: Optional[dict], raw: Optional[bytes]) -> None:
            """A reject before the batch is tokenised: a tokenising stream keeps the row with its tokenised
            columns as tokens; when it cannot (no tokenisation here), the message with those columns withheld;
            and never a message it could not read as a row (its values would be there in clear)."""
            if spec:
                kept = self._tokenised_value(spec, row)
                if kept is not None:
                    return filed(sid, p, o, reason, kept)
                if row is None:
                    return filed(sid, p, o, reason, None)
            reject(sid, p, o, reason, raw)

        nxt: dict[int, int] = {}
        first: dict[int, int] = {}
        ops: list[tuple[str, dict, int, int]] = []
        for m in messages:
            nxt[m.partition] = max(nxt.get(m.partition, 0), m.offset + 1)
            first[m.partition] = min(first.get(m.partition, m.offset), m.offset)
            row = None
            try:
                parsed = parse(stream, m.key, m.value, self._registry)
                if parsed is None:
                    continue
                op, row = parsed
                if stream["mode"] == "upsert" and any(row.get(k) is None for k in key):
                    raise Reject(f"no value for the key {', '.join(key)}")
                if stream["mode"] == "upsert" and any(isinstance(row.get(k), (dict, list)) for k in key):
                    # Not a value to match rows on (and unhashable: it would fail the batch on every retry).
                    raise Reject(f"the key {', '.join(key)} is an object or a list, not a value")
                ops.append((op, row, m.partition, m.offset))
            except Reject as e:
                early(m.partition, m.offset, str(e), row, m.value)
        props = {f"{PROP}{sid}.{stream['topic']}.{p}": str(o) for p, o in nxt.items()}
        shaped = None                                      # the transform's output types, for a new table
        if stream.get("transform") and ops:
            from cairn.pipelines.stream_transform import transform_ops

            def set_aside(p, o, reason, r):
                if spec:
                    # The row is the transform's input, before tokenisation: its columns may feed a tokenised one
                    # under another name, and the engine's message may quote a value. Neither is kept in clear.
                    if not reason.startswith("transform: no value for the key"):
                        reason = "transform: failed on this row (the message is withheld: the stream tokenises columns)"
                    return filed(sid, p, o, reason, None)
                return reject(sid, p, o, reason, json.dumps(r, default=str).encode())
            ops, shaped = transform_ops(stream["transform"], ops, key if stream["mode"] == "upsert" else [], set_aside)
            if not ops:
                self._mark(stream, props)                  # all filtered or set aside: the offsets still move on
                return 0
        if not ops:
            return 0                                       # only tombstones or rejects: offsets go to Kafka
        if stream.get("tokenize"):
            # Tokenised as they land, keys included (so a delete finds the row its upsert wrote).
            if self.tokens is None:
                raise StreamError("this stream tokenises columns, and tokenisation is not configured", 503)
            done = self.tokens.tokenize_rows([r for _, r, _, _ in ops], stream["tokenize"])
            ops = [(op, r, p, o) for (op, _, p, o), r in zip(ops, done)]
        cat = self._catalog()
        ident = tuple(stream["target"].split("."))
        for attempt in range(5):
            upserts = [r for op, r, _, _ in ops if op == "upsert"]
            try:
                table = cat.load_table(ident)
            except NoSuchTableError:
                if not upserts:
                    return 0                               # deletes of rows that were never written
                schema = infer_schema(upserts)
                if shaped is not None and set(shaped.names) >= set(schema.names):
                    schema = pa.schema([pa.field(f.name, pa.string() if pa.types.is_null(f.type) else f.type)
                                        for f in shaped])
                table = open_table(cat, stream["target"], schema, stream["create_table"])
            if stream["topic"].startswith("http."):
                # An http channel's one-writer lock is per process: another replica may have committed these
                # rows since the channel's offset was read. The table decides (re-read on every retry).
                done = self.table_offsets(table, sid, stream["topic"])
                if any(done.get(p, 0) > o for p, o in first.items()):
                    raise StreamError(f"another request on channel {stream['topic'][5:]} committed first; resume "
                                      f"from offset {done.get(0, 0)}", 409)
            names = set(table.schema().as_arrow().names)
            extra = {c for _, r, _, _ in ops for c in r} - names
            if stream["evolve"] and extra:
                with table.update_schema() as u:
                    u.union_by_name(infer_schema([{c: r.get(c) for c in extra} for r in upserts]))
                table = cat.load_table(ident)
                names = set(table.schema().as_arrow().names)
            kept = []
            for op, r, p, o in ops:
                unknown = sorted(c for c in r if c not in names)
                if unknown and op == "upsert":
                    reject(sid, p, o, f"columns {stream['target']} does not have: {', '.join(unknown)}",
                                 json.dumps(r, default=str).encode())
                else:
                    kept.append((op, r, p, o))
            if stream["mode"] == "upsert":
                last: dict[tuple, tuple[str, dict, int, int]] = {}
                for op, r, p, o in kept:                   # the last change of each key wins
                    last[tuple(r[k] for k in key)] = (op, r, p, o)
                changes = list(last.values())
            else:
                changes = kept
            want = table.schema().as_arrow()
            if stream.get("bad_rows") == "quarantine" and any(op == "upsert" for op, _, _, _ in changes):
                # A row the table refuses (a value that does not convert, no value for a required column) is
                # set aside with its offset; the rest commit. Its key is left alone: the old row stays.
                at = [i for i, c in enumerate(changes) if c[0] == "upsert"]
                data, bad = fit_rows(table, [changes[i][1] for i in at], want, stream["target"])
                refused = {at[j] for j, _ in bad}
                for j, reason in bad:
                    _, r, p, o = changes[at[j]]
                    reject(sid, p, o, reason, json.dumps(r, default=str).encode())
                changes = [c for i, c in enumerate(changes) if i not in refused]
            else:
                data = None
            rows = [r for op, r, _, _ in changes if op == "upsert"]
            if data is None and rows:
                data = fit(table, pa.Table.from_pylist(rows, schema=want), stream["target"])
            if not rows:
                data = None
            if stream["mode"] == "upsert":
                keys = [tuple(r[k] for k in key) for _, r, _, _ in changes]
                deleted = len(keys) - len(rows)
            else:
                keys, deleted = [], 0
            if not keys and not rows:
                return 0
            if keys:      # an upsert replaces, and a delete removes, the rows with these keys: not while held
                check_not_held(self.held, stream["target"], StreamError, f"stream {stream.get('name') or sid}",
                               table, key_filter(key, keys))
            try:
                with table.transaction() as tx:
                    if keys:
                        with warnings.catch_warnings():   # new keys match nothing: that is normal here
                            warnings.filterwarnings("ignore", "Delete operation did not match any records")
                            tx.delete(key_filter(key, keys), snapshot_properties=props)
                    if data is not None:
                        tx.append(data, snapshot_properties=props)
                break
            except CommitFailedException:                  # another writer committed first: re-read, retry
                time.sleep(0.2 * (attempt + 1))
        else:
            raise StreamError(f"{stream['target']}: the commit kept conflicting")
        if self.changes is not None:
            self.changes.record([self.urn(*ident)], "stream", f"stream:{stream['name']}")
        return len(rows) + deleted


    def _tokenised_value(self, spec: dict, row: Optional[dict]) -> Optional[bytes]:
        """What a tokenising stream keeps of a rejected row: the row with its tokenised columns as tokens, or
        nothing when there is no row (a message that could not be read) or it cannot be tokenised."""
        if not isinstance(row, dict) or self.tokens is None:
            return None
        try:
            done = self.tokens.tokenize_rows([dict(row)], spec)[0]
        except Exception:  # noqa: BLE001 - fail closed: keep nothing rather than the value in clear
            logger.warning("streams: a rejected row could not be tokenised; its value is not kept", exc_info=True)
            return None
        return json.dumps(done, default=str).encode()

    def _mark(self, stream: dict, props: dict) -> None:
        """An empty commit recording a batch's offsets, when its transform left nothing to write (an http channel
        resumes from the table's offsets, so a fully filtered request must still move them on)."""
        import pyarrow as pa
        from pyiceberg.exceptions import CommitFailedException, NoSuchTableError

        for attempt in range(5):
            try:
                table = self._catalog().load_table(tuple(stream["target"].split(".")))
            except NoSuchTableError:
                return                                     # created by the first row that lands
            try:
                table.append(pa.Table.from_pylist([], schema=table.schema().as_arrow()), snapshot_properties=props)
                return
            except CommitFailedException:
                time.sleep(0.2 * (attempt + 1))
        raise StreamError(f"{stream['target']}: the commit kept conflicting")

    # ---- rows over HTTP ---------------------------------------------------------------------
    _channel_locks: dict = {}
    _channel_locks_guard = threading.Lock()

    def by_name(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(streams).where(streams.c.name == name)).first()
        if r is None:
            raise StreamError("no such stream", 404)
        return _stream(r)

    def channel_offset(self, stream: dict, channel: str) -> int:
        """The next offset a channel expects: one past the last row committed through it."""
        return self.offsets({**stream, "topic": f"http.{channel}"}).get(0, 0)

    def ingest(self, name: str, channel: str, rows: list, offset: Optional[int] = None) -> dict:
        """Rows posted to an http stream, written as one commit. With ``offset`` (the channel offset of
        the first row), rows already committed through the channel are skipped: a retried request
        writes nothing twice. One writer per channel at a time."""
        import types
        import zlib

        stream = self.by_name(name)
        if stream["format"] != "http":
            raise StreamError(f"{name} is not an http stream", 409)
        if not stream["enabled"]:
            raise StreamError(f"{name} is paused", 409)
        if not CHANNEL.match(channel or ""):
            raise StreamError("channel: letters, digits, '.', '_' and '-' (up to 64)")
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_HTTP_ROWS or not all(isinstance(r, dict) for r in rows):
            raise StreamError(f"rows: 1 to {MAX_HTTP_ROWS} JSON objects")
        if offset is not None and (not isinstance(offset, int) or offset < 0):
            raise StreamError("offset: the channel offset of the first row (0 or more)")
        key = (stream["id"], channel)
        with self._channel_locks_guard:
            lock = self._channel_locks.setdefault(key, threading.Lock())
        with lock:
            committed = self.channel_offset(stream, channel)
            start = committed if offset is None else offset
            if start > committed:
                raise StreamError(f"offset {start} skips ahead: the channel expects {committed}", 409)
            skip = committed - start
            todo = rows[skip:]
            part = zlib.crc32(channel.encode()) & 0x7FFFFFFF        # where its rejects are filed
            msgs = [types.SimpleNamespace(partition=0, offset=committed + i, key=None,
                                          value=json.dumps(r, default=str).encode()) for i, r in enumerate(todo)]
            written, rejected = 0, set()

            def reject(sid, p, o, reason, value):
                rejected.add(o)
                self._reject(sid, part, o, f"[{channel}] {reason}", value)
            if msgs:
                written = self.write({**stream, "topic": f"http.{channel}"}, msgs, reject=reject)
            nxt = self.channel_offset(stream, channel) if msgs else committed
        self._status(stream["id"], status="running", add_rows=written, last_commit_ms=_now(), error=None)
        out = {"written": written, "duplicates": skip, "rejected": len(todo) - written if stream["mode"] == "append" else None,
               "next_offset": nxt}
        if stream.get("transform"):                       # rows its WHERE dropped are neither written nor rejected
            out["rejected"] = len(rejected)
            if stream["mode"] == "append":
                out["filtered"] = len(todo) - written - len(rejected)
        return out


class StreamWorker(threading.Thread):
    """Consumes one stream until stopped: batches by rows or seconds, one Iceberg commit per batch."""

    def __init__(self, streams_: Streams, stream: dict):
        super().__init__(daemon=True, name=f"cairn-stream-{stream['name']}")
        self.s, self.stream, self.stop = streams_, stream, threading.Event()
        # What the rebalance listener raised: kafka-python only logs it, inside poll(), and carries on reading from
        # Kafka's committed offsets. The run loop takes it up after poll() and starts over.
        self.rebalance_error: Optional[BaseException] = None

    def _consumer(self):
        from kafka import ConsumerRebalanceListener, KafkaConsumer

        st = self.stream
        consumer = KafkaConsumer(group_id=f"cairn-stream-{st['id']}", enable_auto_commit=False,
                                 auto_offset_reset=st["start"], **self.s.kafka)
        worker = self

        class Seek(ConsumerRebalanceListener):
            def on_partitions_revoked(self, revoked):
                try:
                    worker.flush(consumer)                # what this worker read is written before it goes
                except Exception as e:
                    worker.rebalance_error = e
                    raise

            def on_partitions_assigned(self, assigned):
                try:
                    at = worker.s.offsets(st)             # the table's own record wins over Kafka's
                    for tp in assigned:
                        if tp.partition in at:
                            consumer.seek(tp, at[tp.partition])
                except Exception as e:
                    worker.rebalance_error = e
                    raise

        consumer.subscribe([st["topic"]], listener=Seek())
        return consumer

    def flush(self, consumer) -> None:
        if not self.pending:
            return
        from kafka import OffsetAndMetadata, TopicPartition

        batch, self.pending = self.pending, []
        n = self.s.write(self.stream, batch)
        nxt: dict[int, int] = {}
        for m in batch:
            nxt[m.partition] = max(nxt.get(m.partition, 0), m.offset + 1)
        commit = {TopicPartition(self.stream["topic"], p): OffsetAndMetadata(o, "", -1) for p, o in nxt.items()}
        try:
            consumer.commit(commit)
        except Exception as e:  # noqa: BLE001 - the table holds the offsets; Kafka's are a fallback
            logger.warning("stream %s: Kafka offset commit failed: %s", self.stream["name"], e)
        lag = None
        try:
            ends = consumer.end_offsets(list(commit))
            lag = sum(max(0, ends[tp] - consumer.position(tp)) for tp in commit)
        except Exception:  # noqa: BLE001 - lag is informational
            pass
        self.s._status(self.stream["id"], status="running", error=None, last_commit_ms=_now(), lag=lag, add_rows=n)

    def run(self) -> None:
        backoff = 1.0
        st = self.stream
        while not self.stop.is_set():
            consumer, self.pending, self.rebalance_error = None, [], None
            try:
                consumer = self._consumer()
                first = None
                while not self.stop.is_set():
                    got = consumer.poll(timeout_ms=500, max_records=st["batch_rows"])
                    if self.rebalance_error is not None:
                        # The partitions were not set to the table's offsets (or a batch was not written): what
                        # was read may already be in the table. Drop it; the next consumer starts over from there.
                        err, self.rebalance_error, self.pending = self.rebalance_error, None, []
                        raise StreamError(f"rebalance: {err}", 503)
                    for msgs in got.values():
                        self.pending.extend(msgs)
                    if self.pending and first is None:
                        first = time.time()
                    if self.pending and (len(self.pending) >= st["batch_rows"] or time.time() - first >= st["batch_seconds"]):
                        self.flush(consumer)
                        first = None
                        backoff = 1.0
                self.flush(consumer)
            except Exception as e:  # noqa: BLE001 - shown on the stream; it starts again from the table's offsets
                msg = (str(e).splitlines() or [type(e).__name__])[0][:2000]
                logger.warning("stream %s failed: %s", st["name"], msg)
                try:
                    self.s._status(st["id"], status="error", error=msg)
                except Exception:  # noqa: BLE001
                    logger.exception("stream %s: status not recorded", st["name"])
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60.0)
            finally:
                if consumer is not None:
                    try:
                        consumer.close(autocommit=False)
                    except Exception:  # noqa: BLE001
                        pass


class StreamRunner:
    """Keeps one worker per enabled stream (``cairn streams``); definitions are re-read every ``interval``."""

    def __init__(self, streams_: Streams, interval: float = 15.0):
        self.s, self.interval, self.workers = streams_, interval, {}
        self.stop = threading.Event()

    def reconcile(self) -> None:
        want = {d["id"]: d for d in self.s.list() if d["enabled"] and d["format"] != "http"}
        for sid, w in list(self.workers.items()):
            d = want.get(sid)
            if d is None or {k: d[k] for k in w.stream if k != "status"} != {k: w.stream[k] for k in w.stream if k != "status"} \
                    or not w.is_alive():
                w.stop.set()
                w.join(30)
                del self.workers[sid]
        for sid, d in want.items():
            if sid not in self.workers:
                d = {k: v for k, v in d.items() if k not in ("status", "layer")}   # not what a worker runs
                self.workers[sid] = StreamWorker(self.s, d)
                self.workers[sid].start()

    def run_forever(self) -> None:
        while not self.stop.is_set():
            try:
                self.reconcile()
            except Exception:  # noqa: BLE001 - the database may be restarting
                logger.exception("streams: reconcile failed")
            self.stop.wait(self.interval)
        for w in self.workers.values():
            w.stop.set()
        for w in self.workers.values():
            w.join(30)


def build_streams_router(s: Streams, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/v1/streams", tags=["lakehouse"])

    def admin(pr):
        authorize(pr, "MANAGE_SYSTEM_OPERATIONS", None)
        return pr.urn

    def wrap(fn):
        try:
            return fn()
        except PipeError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    def list_streams(pr=Depends(current_principal)):
        admin(pr)
        return s.list()

    @router.post("", status_code=201)
    def create(body: dict = Body(...), pr=Depends(current_principal)):
        return wrap(lambda: s.create(body, admin(pr)))

    @router.put("/{sid}")
    def update_stream(sid: str, body: dict = Body(...), pr=Depends(current_principal)):
        return wrap(lambda: s.update(sid, body, admin(pr)))

    @router.post("/{sid}/enabled")
    def enabled(sid: str, body: dict = Body(...), pr=Depends(current_principal)):
        return wrap(lambda: s.set_enabled(sid, bool(body.get("enabled")), admin(pr)))

    @router.delete("/{sid}", status_code=204)
    def remove(sid: str, pr=Depends(current_principal)):
        wrap(lambda: s.delete(sid, admin(pr)))

    @router.get("/{sid}/rejects")
    def rejects(sid: str, pr=Depends(current_principal)):
        admin(pr)
        return s.rejects(sid)

    return router


def build_ingest_router(s: Streams, current_principal, authorize) -> Any:
    """Rows into http streams: ``INGEST_STREAM`` on ``urn:li:stream:<name>`` (or ``MANAGE_INGESTION``)."""
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/v1/ingest", tags=["lakehouse"])

    def may(pr, name: str) -> None:
        if authorize(pr, "MANAGE_INGESTION", None, raise_on_deny=False):
            return
        authorize(pr, "INGEST_STREAM", f"urn:li:stream:{name}")

    def wrap(fn):
        try:
            return fn()
        except PipeError as e:
            raise HTTPException(e.status, str(e))

    @router.post("/{name}/rows")
    def ingest(name: str, channel: str = Body(...), rows: list = Body(...), offset: Optional[int] = Body(None),
               pr=Depends(current_principal)):
        """Writes rows into the stream's table now (one commit); ``offset`` makes retries exactly once."""
        may(pr, name)
        return wrap(lambda: s.ingest(name, channel, rows, offset))

    @router.get("/{name}/channels/{channel}")
    def channel(name: str, channel: str, pr=Depends(current_principal)):
        """Where a channel is: the offset its next row should have (resume from here after a crash)."""
        may(pr, name)
        return wrap(lambda: {"channel": channel, "next_offset": s.channel_offset(s.by_name(name), channel)})

    return router
