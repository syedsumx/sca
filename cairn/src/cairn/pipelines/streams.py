"""Streams: Kafka topics written into lakehouse tables continuously, change events (CDC) included.

A stream names a Kafka ``topic``, a ``format`` and the Iceberg ``target`` (``namespace.table``):

* ``json``: each message is a JSON object, one row. In ``append`` mode rows are added; in ``upsert`` mode
  the row replaces the one with the same ``key`` columns, and a tombstone (no value) whose Kafka key is a
  JSON object of the key columns deletes it.
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
on its status and is retried with back-off: nothing is skipped silently.

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
from cairn.pipelines.pipes import TARGET, PipeError, fit, open_table

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
    Column("start", String(16), nullable=False, default="earliest"),
    Column("source", String(1024), nullable=True),         # the dataset the topic carries (lineage)
    Column("batch_rows", Integer, nullable=False, default=5000),
    Column("batch_seconds", Integer, nullable=False, default=10),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("created_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
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

FORMATS = ("json", "debezium")
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
            "evolve": bool(r.evolve), "start": r.start, "source": r.source, "batch_rows": r.batch_rows, "batch_seconds": r.batch_seconds,
            "enabled": bool(r.enabled), "created_by": r.created_by, "created_ms": r.created_ms}


class Reject(Exception):
    """One message that cannot be a row."""


def parse(stream: dict, key: Optional[bytes], value: Optional[bytes]) -> Optional[tuple[str, dict]]:
    """A message as ``("upsert" | "delete", row)``; None for a tombstone with nothing to do."""
    def obj(raw: bytes, what: str) -> dict:
        try:
            v = json.loads(raw)
        except ValueError:
            raise Reject(f"the {what} is not JSON")
        if not isinstance(v, dict):
            raise Reject(f"the {what} is not a JSON object")
        return v

    if value is None:                                     # tombstone
        if stream["mode"] == "upsert" and stream["format"] == "json" and key:
            k = obj(key, "key")
            k = k.get("payload", k) if isinstance(k.get("payload"), dict) else k
            return "delete", k
        return None
    v = obj(value, "value")
    if stream["format"] == "json":
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
                 urn: Optional[Callable[[str, str], str]] = None, graph=None):
        """``graph``: Cairn Core (``get_aspect``, ``emit``), for the lineage from a stream's ``source``."""
        self.engine, self.kafka, self.catalog_props, self.audit, self.changes = engine, kafka, catalog, audit, changes
        self.graph = graph
        self.urn = urn or (lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)")

    # ---- definitions --------------------------------------------------------------------
    def create(self, body: dict, actor: str) -> dict:
        name, topic = str(body.get("name") or "").strip(), str(body.get("topic") or "")
        fmt, target = str(body.get("format") or "json"), str(body.get("target") or "")
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
        if mode == "upsert" and not key:
            raise StreamError("upsert (and debezium) streams need the key columns")
        if not all(COLUMN.match(k) for k in key):
            raise StreamError("key columns are column names")
        start = str(body.get("start") or "earliest")
        if start not in ("earliest", "latest"):
            raise StreamError("start is earliest or latest")
        source = body.get("source") or None
        if source is not None and not (isinstance(source, str) and source.startswith("urn:li:dataset:(") and len(source) <= 1024):
            raise StreamError("source is the dataset URN the topic carries")
        if source is not None and self.graph is None:
            raise StreamError("lineage needs Cairn Core: this deployment has none", 503)
        rows, secs = body.get("batch_rows", 5000), body.get("batch_seconds", 10)
        if not isinstance(rows, int) or not 1 <= rows <= 1_000_000 or not isinstance(secs, int) or not 1 <= secs <= 3600:
            raise StreamError("batch_rows is 1-1000000 and batch_seconds 1-3600")
        row = {"id": uuid.uuid4().hex, "name": name[:256], "topic": topic, "format": fmt, "mode": mode,
               "key": ",".join(key), "target": target, "create_table": bool(body.get("create_table")),
               "evolve": bool(body.get("evolve")), "start": start, "source": source, "batch_rows": rows, "batch_seconds": secs,
               "enabled": bool(body.get("enabled", True)), "created_by": actor, "created_ms": _now()}
        with self.engine.begin() as conn:
            if conn.execute(select(streams.c.id).where(streams.c.name == row["name"])).first():
                raise StreamError("a stream with that name exists", 409)
            conn.execute(streams.insert().values(**row))
            if self.audit is not None:
                self.audit.record("lakehouse", "stream.created", actor, self.urn(*target.split(".")), row, conn=conn)
            if source:
                self._lineage(self.urn(*target.split(".")), source, actor)   # fails: nothing is created
        return _stream(type("R", (), row))

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
        return defs

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

        return RestCatalog("cairn-streams", **self.catalog_props)

    @staticmethod
    def table_offsets(table, sid: str, topic: str) -> dict[int, int]:
        """The next offset of each partition, from the table's snapshots (the latest that names it)."""
        prefix = f"{PROP}{sid}.{topic}."
        out: dict[int, int] = {}
        for snap in sorted(table.metadata.snapshots, key=lambda s: s.timestamp_ms):
            for k, v in (snap.summary.additional_properties if snap.summary else {}).items():
                if k.startswith(prefix):
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
    def write(self, stream: dict, messages: list) -> int:
        """Writes a batch of Kafka messages (``.partition .offset .key .value``) as one Iceberg commit whose
        snapshot records the next offset of each partition. Returns the rows written."""
        import pyarrow as pa
        from pyiceberg.exceptions import CommitFailedException, NoSuchTableError

        sid, key = stream["id"], stream["key"]
        nxt: dict[int, int] = {}
        ops: list[tuple[str, dict, int, int]] = []
        for m in messages:
            nxt[m.partition] = max(nxt.get(m.partition, 0), m.offset + 1)
            try:
                parsed = parse(stream, m.key, m.value)
                if parsed is None:
                    continue
                op, row = parsed
                if stream["mode"] == "upsert" and any(row.get(k) is None for k in key):
                    raise Reject(f"no value for the key {', '.join(key)}")
                ops.append((op, row, m.partition, m.offset))
            except Reject as e:
                self._reject(sid, m.partition, m.offset, str(e), m.value)
        props = {f"{PROP}{sid}.{stream['topic']}.{p}": str(o) for p, o in nxt.items()}
        if not ops:
            return 0                                       # only tombstones or rejects: offsets go to Kafka
        cat = self._catalog()
        ident = tuple(stream["target"].split("."))
        for attempt in range(5):
            upserts = [r for op, r, _, _ in ops if op == "upsert"]
            try:
                table = cat.load_table(ident)
            except NoSuchTableError:
                if not upserts:
                    return 0                               # deletes of rows that were never written
                table = open_table(cat, stream["target"], infer_schema(upserts), stream["create_table"])
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
                    self._reject(sid, p, o, f"columns {stream['target']} does not have: {', '.join(unknown)}",
                                 json.dumps(r, default=str).encode())
                else:
                    kept.append((op, r))
            if stream["mode"] == "upsert":
                last: dict[tuple, tuple[str, dict]] = {}
                for op, r in kept:                         # the last change of each key wins
                    last[tuple(r[k] for k in key)] = (op, r)
                keys = list(last)
                rows = [r for op, r in last.values() if op == "upsert"]
                deleted = len(keys) - len(rows)
            else:
                keys, rows, deleted = [], [r for _, r in kept], 0
            if not keys and not rows:
                return 0
            want = table.schema().as_arrow()
            data = fit(table, pa.Table.from_pylist(rows, schema=want), stream["target"]) if rows else None
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


class StreamWorker(threading.Thread):
    """Consumes one stream until stopped: batches by rows or seconds, one Iceberg commit per batch."""

    def __init__(self, streams_: Streams, stream: dict):
        super().__init__(daemon=True, name=f"cairn-stream-{stream['name']}")
        self.s, self.stream, self.stop = streams_, stream, threading.Event()

    def _consumer(self):
        from kafka import ConsumerRebalanceListener, KafkaConsumer

        st = self.stream
        consumer = KafkaConsumer(group_id=f"cairn-stream-{st['id']}", enable_auto_commit=False,
                                 auto_offset_reset=st["start"], **self.s.kafka)
        worker = self

        class Seek(ConsumerRebalanceListener):
            def on_partitions_revoked(self, revoked):
                worker.flush(consumer)                    # what this worker read is written before it goes

            def on_partitions_assigned(self, assigned):
                at = worker.s.offsets(st)                 # the table's own record wins over Kafka's
                for tp in assigned:
                    if tp.partition in at:
                        consumer.seek(tp, at[tp.partition])

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
            consumer, self.pending = None, []
            try:
                consumer = self._consumer()
                first = None
                while not self.stop.is_set():
                    for msgs in consumer.poll(timeout_ms=500, max_records=st["batch_rows"]).values():
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
        want = {d["id"]: d for d in self.s.list() if d["enabled"]}
        for sid, w in list(self.workers.items()):
            d = want.get(sid)
            if d is None or {k: d[k] for k in w.stream if k != "status"} != {k: w.stream[k] for k in w.stream if k != "status"} \
                    or not w.is_alive():
                w.stop.set()
                w.join(30)
                del self.workers[sid]
        for sid, d in want.items():
            if sid not in self.workers:
                d = {k: v for k, v in d.items() if k != "status"}
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
