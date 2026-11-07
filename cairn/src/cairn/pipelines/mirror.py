"""Mirrors: a PostgreSQL table kept in a lakehouse table, change by change, with no Kafka in between.

``POST /v1/mirrors`` ``{"name", "connection", "table": "schema.table", "target": "namespace.table"}``
(``MANAGE_INGESTION``) mirrors a table. ``connection`` is one of Cairn's PostgreSQL connections (its URL from
``CAIRN_CONNECTIONS``), whose role may replicate (``REPLICATION``, or superuser on a managed service's terms)
and owns the table or may create a publication for it. The server runs with ``wal_level = logical``.

* **Start.** Cairn creates a publication for the table and a logical replication slot (PostgreSQL's own
  ``pgoutput`` decoder, nothing to install). It then copies the table's rows, and changes since the slot was made
  follow. Every change is an upsert or delete by the table's primary key, so the copy and the changes converge.
* **Keeping up.** Each scheduler tick reads the slot's whole transactions (``pg_logical_slot_peek_binary_changes``)
  and writes them as one Iceberg commit through the same writer as streams. The commit records the last log
  position (LSN) it holds, and only then is the slot advanced. A crash between the two replays changes the table
  already has, and they are skipped by their LSN: nothing is written twice or lost.
* **What arrives.** Inserts and updates are upserts, deletes delete, and a ``TRUNCATE`` empties the lakehouse
  table. Values are typed from PostgreSQL's types: integers, floats, numeric as float, booleans, dates,
  timestamps, and the rest as text. A column added at the source is added to the table. An unchanged TOASTed
  value (a large value an update did not touch) is read back from the source by key.
* **MySQL.** A connection whose URL is ``mysql://`` is mirrored through MySQL's row-based binary log instead
  (``cairn.pipelines.mirror_mysql``): a copy from a recorded log position, then whole transactions, with log
  positions as offsets. MySQL keeps no slot, so the server's log retention must outlast a paused mirror.
* **SQL Server.** A connection whose URL is ``mssql://`` is mirrored through change data capture
  (``cairn.pipelines.mirror_mssql``), which a database administrator turns on for the table: a copy from the highest
  captured LSN, then whole transactions in commit order. CDC's cleanup must outlast a paused mirror.
* **Stopping.** ``DELETE /v1/mirrors/<name>`` drops the slot and the publication: a slot nobody reads keeps the
  server's WAL forever, so the status shows how much WAL the slot holds back (``retained_bytes``).
  ``POST .../pause`` keeps the slot; ``POST .../resume`` carries on from it.

The lakehouse table is governed as any other: its readers need Cairn's read access; the mirror writes as Cairn.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import struct
import time
import types
import uuid
from decimal import Decimal
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, String, Table, Text, insert, select, update

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

mirrors_table = Table(
    "cairn_mirrors", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(64), nullable=False, unique=True),
    Column("connection", String(128), nullable=False),
    Column("source", String(256), nullable=False),          # schema.table
    Column("target", String(256), nullable=False),          # namespace.table
    Column("key", String(1024), nullable=False),            # the source's primary key columns
    Column("enabled", Boolean, nullable=False),
    Column("snapshot_done", Boolean, nullable=False),
    Column("status", String(16), nullable=False),           # starting | running | paused | failed
    Column("error", Text, nullable=True),
    Column("rows", BigInteger, nullable=False),
    Column("last_lsn", String(32), nullable=True),
    Column("last_commit_ms", BigInteger, nullable=True),
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)

NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
MAX_CHANGES = 20_000


class MirrorError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def lsn_int(text: str) -> int:
    hi, lo = text.split("/")
    return (int(hi, 16) << 32) | int(lo, 16)


def lsn_text(value: int) -> str:
    return f"{value >> 32:X}/{value & 0xFFFFFFFF:X}"


# ---- pgoutput (protocol version 1) -----------------------------------------------------------------
INT_TYPES = {20, 21, 23, 26}            # int8, int2, int4, oid
FLOAT_TYPES = {700, 701, 1700}          # float4, float8, numeric
BOOL, DATE, TS, TSTZ = 16, 1082, 1114, 1184


def typed(oid: int, text: Optional[str]) -> Any:
    if text is None:
        return None
    try:
        if oid in INT_TYPES:
            return int(text)
        if oid in FLOAT_TYPES:
            return float(Decimal(text))
        if oid == BOOL:
            return text == "t"
        if oid == DATE:
            return dt.date.fromisoformat(text)
        if oid == TS:
            return dt.datetime.fromisoformat(text)
        if oid == TSTZ:
            return dt.datetime.fromisoformat(text if not re.search(r"[+-]\d\d$", text) else text + ":00") \
                .astimezone(dt.timezone.utc)
    except ValueError:
        return text                                      # infinity and the like: kept as written
    return text


class Decoder:
    """Turns pgoutput messages into changes. Relation messages say what a relation id's columns are; they come
    before the first change of each relation in a decoding session."""

    def __init__(self):
        self.relations: dict[int, dict] = {}

    @staticmethod
    def _string(buf: bytes, i: int) -> tuple[str, int]:
        end = buf.index(b"\0", i)
        return buf[i:end].decode(), end + 1

    def _tuple(self, buf: bytes, i: int, rel: dict) -> tuple[dict, list[str], int]:
        (n,) = struct.unpack_from(">h", buf, i)
        i += 2
        row, unchanged = {}, []
        for c in range(n):
            kind = buf[i:i + 1]
            i += 1
            name, oid = rel["columns"][c]
            if kind == b"n":
                row[name] = None
            elif kind == b"u":
                unchanged.append(name)
            elif kind in (b"t", b"b"):
                (ln,) = struct.unpack_from(">i", buf, i)
                i += 4
                raw = buf[i:i + ln]
                i += ln
                row[name] = typed(oid, raw.decode()) if kind == b"t" else raw.hex()
            else:
                raise MirrorError(f"pgoutput: unknown tuple data kind {kind!r}", 502)
        return row, unchanged, i

    @staticmethod
    def _expect(buf: bytes, i: int, *tags: bytes) -> None:
        if buf[i:i + 1] not in tags:
            raise MirrorError(f"pgoutput: expected {tags} at {i}, got {buf[i:i + 1]!r}", 502)

    def decode(self, buf: bytes) -> Optional[tuple]:
        """One message as ``(kind, relation, row, unchanged)``; kind in begin, commit, upsert, delete, truncate;
        None for messages with nothing to do."""
        tag = buf[:1]
        if tag == b"R":
            (relid,) = struct.unpack_from(">I", buf, 1)
            ns, i = self._string(buf, 5)
            name, i = self._string(buf, i)
            i += 1                                       # replica identity
            (n,) = struct.unpack_from(">h", buf, i)
            i += 2
            cols = []
            for _ in range(n):
                i += 1                                   # flags (part of the key)
                col, i = self._string(buf, i)
                (oid,) = struct.unpack_from(">I", buf, i)
                i += 8                                   # type oid, type modifier
                cols.append((col, oid))
            self.relations[relid] = {"schema": ns, "table": name, "columns": cols}
            return None
        if tag == b"B":
            return ("begin", None, None, None)
        if tag == b"C":
            return ("commit", None, None, None)
        if tag in (b"I", b"U", b"D"):
            (relid,) = struct.unpack_from(">I", buf, 1)
            rel = self.relations.get(relid)
            if rel is None:
                raise MirrorError(f"pgoutput: a change to relation {relid} before its description", 502)
            i = 5
            if tag == b"I":
                self._expect(buf, i, b"N")
                row, unchanged, _ = self._tuple(buf, i + 1, rel)
                return ("upsert", rel, row, unchanged)
            if tag == b"U":
                old = None
                if buf[i:i + 1] in (b"K", b"O"):
                    old, _, i = self._tuple(buf, i + 1, rel)
                self._expect(buf, i, b"N")
                row, unchanged, _ = self._tuple(buf, i + 1, rel)
                return ("upsert", rel, row, unchanged) if old is None else ("update", rel, row, unchanged, old)
            self._expect(buf, i, b"K", b"O")
            row, _, _ = self._tuple(buf, i + 1, rel)
            return ("delete", rel, row, [])
        if tag == b"T":
            return ("truncate", None, None, None)
        return None                                      # type, origin, message: nothing to do


class Mirrors:
    """Mirrors of PostgreSQL tables into the lakehouse, written through ``streams.write``."""

    def __init__(self, engine, streams, source_url: Callable[[str], str], audit=None,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.engine, self.streams, self.source_url, self.audit, self.clock = engine, streams, source_url, audit, clock

    # ---- definitions -------------------------------------------------------------------------
    @staticmethod
    def _names(mid: str) -> tuple[str, str]:
        return f"cairn_mirror_{mid}", f"cairn_mirror_{mid}"     # slot, publication

    def _kind(self, connection: str) -> str:
        url = self.source_url(connection) or ""
        if re.match(r"^mysql(\+\w+)?://", url):
            return "mysql"
        return "mssql" if re.match(r"^(mssql(\+\w+)?|sqlserver)://", url) else "postgres"

    def _connect(self, connection: str):
        import psycopg

        url = self.source_url(connection)
        if not url:
            raise MirrorError(f"no connection {connection!r}", 404)
        url = re.sub(r"^postgresql\+\w+://", "postgresql://", url)
        if not url.startswith(("postgresql://", "postgres://")):
            raise MirrorError(f"{connection} is not a PostgreSQL connection")
        return psycopg.connect(url, autocommit=True, connect_timeout=10)

    def _row(self, name: str):
        with self.engine.connect() as c:
            row = c.execute(select(mirrors_table).where(mirrors_table.c.name == name)).first()
        if row is None:
            raise MirrorError("no such mirror", 404)
        return row

    @staticmethod
    def _public(r, retained: Optional[int] = None) -> dict:
        out = {"name": r.name, "connection": r.connection, "table": r.source, "target": r.target,
               "key": r.key.split(","), "enabled": r.enabled, "status": r.status, "error": r.error, "rows": r.rows,
               "last_lsn": r.last_lsn, "last_commit_ms": r.last_commit_ms, "created_by": r.created_by,
               "created_ms": r.created_ms}
        if retained is not None:
            out["retained_bytes"] = retained
        return out

    def create(self, actor: str, body: dict) -> dict:
        from psycopg import sql

        name, connection = str(body.get("name") or ""), str(body.get("connection") or "")
        source, target = str(body.get("table") or ""), str(body.get("target") or "")
        if not NAME.match(name):
            raise MirrorError("name: lowercase letters, digits and '_'")
        parts = source.split(".")
        if len(parts) != 2 or not all(IDENT.match(p) for p in parts):
            raise MirrorError("table is schema.table")
        tparts = target.split(".")
        if len(tparts) != 2 or not all(re.match(r"^[a-z_][a-z0-9_]{0,62}$", p) for p in tparts):
            raise MirrorError("target is namespace.table (lowercase)")
        with self.engine.connect() as c:
            if c.execute(select(mirrors_table.c.id).where(mirrors_table.c.name == name)).first():
                raise MirrorError(f"{name} exists", 409)
        mid = uuid.uuid4().hex[:16]
        if not self.source_url(connection):
            raise MirrorError(f"no connection {connection!r}", 404)
        if self._kind(connection) == "mysql":
            key = self._create_mysql(connection, parts)
            return self._created(actor, mid, name, connection, source, target, key)
        if self._kind(connection) == "mssql":
            key = self._create_mssql(connection, parts)
            return self._created(actor, mid, name, connection, source, target, key)
        slot, pub = self._names(mid)
        ident = sql.Identifier(*parts)
        with self._connect(connection) as conn:
            if conn.execute("SHOW wal_level").fetchone()[0] != "logical":
                raise MirrorError("the source runs without wal_level = logical", 409)
            key = [r[0] for r in conn.execute(
                "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
                "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary "
                "ORDER BY array_position(i.indkey, a.attnum)", (f'"{parts[0]}"."{parts[1]}"',)).fetchall()]
            if not key:
                raise MirrorError(f"{source} has no primary key: a mirror applies changes by key", 409)
            conn.execute(sql.SQL("CREATE PUBLICATION {} FOR TABLE {}").format(sql.Identifier(pub), ident))
            try:
                conn.execute("SELECT pg_create_logical_replication_slot(%s, 'pgoutput')", (slot,))
            except Exception:
                conn.execute(sql.SQL("DROP PUBLICATION IF EXISTS {}").format(sql.Identifier(pub)))
                raise
        return self._created(actor, mid, name, connection, source, target, key)

    def _create_mysql(self, connection: str, parts: list[str]) -> list[str]:
        from cairn.pipelines import mirror_mysql as my

        try:
            conn = my.connect(self.source_url(connection))
        except Exception as e:  # noqa: BLE001 - say why
            raise MirrorError(f"the source could not be reached: {e}", 502)
        try:
            key = my.check(conn, parts[0], parts[1])
        except ValueError as e:
            raise MirrorError(str(e), 409)
        finally:
            conn.close()
        if not key:
            raise MirrorError(f"{'.'.join(parts)} has no primary key: a mirror applies changes by key", 409)
        return key

    def _create_mssql(self, connection: str, parts: list[str]) -> list[str]:
        from cairn.pipelines import mirror_mssql as ms

        try:
            conn = ms.connect(self.source_url(connection))
        except Exception as e:  # noqa: BLE001 - say why
            raise MirrorError(f"the source could not be reached: {e}", 502)
        try:
            key, _ = ms.check(conn, parts[0], parts[1])
        except ValueError as e:
            raise MirrorError(str(e), 409)
        finally:
            conn.close()
        if not key:
            raise MirrorError(f"{'.'.join(parts)} has no primary key: a mirror applies changes by key", 409)
        return key

    def _created(self, actor: str, mid: str, name: str, connection: str, source: str, target: str,
                 key: list[str]) -> dict:
        with self.engine.begin() as c:
            c.execute(insert(mirrors_table).values(
                id=mid, name=name, connection=connection, source=source, target=target, key=",".join(key),
                enabled=True, snapshot_done=False, status="starting", error=None, rows=0, last_lsn=None,
                last_commit_ms=None, created_by=actor, created_ms=self.clock()))
        if self.audit is not None:
            self.audit.record("data", "mirror.created", actor, f"urn:li:mirror:{name}",
                              {"connection": connection, "table": source, "target": target})
        self.run(name)
        return self.get(name)

    def get(self, name: str) -> dict:
        r = self._row(name)
        retained = None
        if self._kind(r.connection) in ("mysql", "mssql"):
            return self._public(r)                   # no slot: the server's log retention holds its place
        try:
            with self._connect(r.connection) as conn:
                got = conn.execute("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)::bigint "
                                   "FROM pg_replication_slots WHERE slot_name = %s", (self._names(r.id)[0],)).fetchone()
                retained = int(got[0]) if got and got[0] is not None else None
        except Exception:  # noqa: BLE001 - the source is away: shown without it
            pass
        return self._public(r, retained)

    def list(self) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(mirrors_table).order_by(mirrors_table.c.name)).all()
        return [self._public(r) for r in rows]

    def set_enabled(self, actor: str, name: str, enabled: bool) -> dict:
        r = self._row(name)
        with self.engine.begin() as c:
            c.execute(update(mirrors_table).where(mirrors_table.c.id == r.id).values(
                enabled=enabled, status="running" if enabled else "paused"))
        if self.audit is not None:
            self.audit.record("data", "mirror.resumed" if enabled else "mirror.paused", actor,
                              f"urn:li:mirror:{name}", {})
        return self.get(name)

    def delete(self, actor: str, name: str) -> None:
        from psycopg import sql

        r = self._row(name)
        slot, pub = self._names(r.id)
        if self._kind(r.connection) in ("mysql", "mssql"):   # nothing kept on the server for a mirror
            with self.engine.begin() as c:
                c.execute(mirrors_table.delete().where(mirrors_table.c.id == r.id))
            if self.audit is not None:
                self.audit.record("data", "mirror.deleted", actor, f"urn:li:mirror:{name}", {})
            return
        with self._connect(r.connection) as conn:
            conn.execute("SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name = %s",
                         (slot,))
            conn.execute(sql.SQL("DROP PUBLICATION IF EXISTS {}").format(sql.Identifier(pub)))
        with self.engine.begin() as c:
            c.execute(mirrors_table.delete().where(mirrors_table.c.id == r.id))
        if self.audit is not None:
            self.audit.record("data", "mirror.deleted", actor, f"urn:li:mirror:{name}", {})

    # ---- running ----------------------------------------------------------------------------
    def _stream(self, r) -> dict:
        return {"id": f"mirror_{r.id}", "name": f"mirror:{r.name}", "topic": f"pg.{r.id}", "format": "pgmirror",
                "mode": "upsert", "key": r.key.split(","), "target": r.target, "create_table": True, "evolve": True,
                "encoding": "json", "bad_rows": "fail", "tokenize": {}}

    def _write(self, r, changes: list[tuple[int, str, dict]], topic: Optional[str] = None) -> int:
        """Writes (offset, op, row) as one commit; returns rows written."""
        stream = self._stream(r)
        if topic:
            stream["topic"] = topic
        msgs = [types.SimpleNamespace(partition=0, offset=o, key=None, value=(op, row)) for o, op, row in changes]

        def reject(sid, partition, offset, reason, value):
            raise MirrorError(f"a change at {lsn_text(offset) if topic is None else offset} cannot be written: {reason}",
                              502)
        return self.streams.write(stream, msgs, reject=reject)

    def _snapshot(self, r, conn) -> int:
        """Copies the source's rows (changes since the slot was made follow, and converge on them)."""
        from psycopg import sql

        cur = conn.cursor(name=f"cairn_mirror_copy_{r.id}")       # server-side: rows in batches
        conn.autocommit = False
        written = 0
        try:
            cur.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(*r.source.split("."))))
            names = None
            offset = 0
            while True:
                batch = cur.fetchmany(5000)
                if names is None:
                    names = [d.name for d in cur.description]
                if not batch:
                    break
                rows = [("upsert", {n: _plain(v) for n, v in zip(names, b)}) for b in batch]
                written += self._write(r, [(offset + i, op, row) for i, (op, row) in enumerate(rows)],
                                       topic=f"pg.{r.id}.copy")
                offset += len(rows)
        finally:
            cur.close()
            conn.rollback()
            conn.autocommit = True
        return written

    def _fill_unchanged(self, conn, r, row: dict, unchanged: list[str]) -> dict:
        from psycopg import sql

        key = r.key.split(",")
        got = conn.execute(sql.SQL("SELECT {} FROM {} WHERE {}").format(
            sql.SQL(", ").join(sql.Identifier(c) for c in unchanged), sql.Identifier(*r.source.split(".")),
            sql.SQL(" AND ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in key)),
            [row[k] for k in key]).fetchone()
        if got is None:
            return row                                   # deleted since: its delete follows
        return {**row, **{c: _plain(v) for c, v in zip(unchanged, got)}}

    def run(self, name: str) -> int:
        """Brings one mirror up to date: whole transactions from its slot, one commit, then the slot advanced."""
        r = self._row(name)
        if not r.enabled:
            return 0
        if self._kind(r.connection) == "mysql":
            return self._run_mysql(r)
        if self._kind(r.connection) == "mssql":
            return self._run_mssql(r)
        slot, pub = self._names(r.id)
        try:
            with self._connect(r.connection) as conn:
                if not r.snapshot_done:
                    copied = self._snapshot(r, conn)
                    with self.engine.begin() as c:
                        c.execute(update(mirrors_table).where(mirrors_table.c.id == r.id).values(
                            snapshot_done=True, rows=mirrors_table.c.rows + copied))
                committed = self.streams.offsets(self._stream(r)).get(0, 0)
                got = conn.execute(
                    "SELECT lsn::text, data FROM pg_logical_slot_peek_binary_changes(%s, NULL, %s, "
                    "'proto_version', '1', 'publication_names', %s)", (slot, MAX_CHANGES, pub)).fetchall()
                last_commit = None
                for i in range(len(got) - 1, -1, -1):        # whole transactions only
                    if bytes(got[i][1])[:1] == b"C":
                        last_commit = i
                        break
                if last_commit is None:
                    self._status(r, "running", None)
                    return 0
                dec, changes, truncated = Decoder(), [], False
                for lsn, data in got[:last_commit + 1]:
                    msg = dec.decode(bytes(data))
                    if msg is None or msg[0] in ("begin", "commit"):
                        continue
                    pos = lsn_int(lsn)
                    if pos < committed:
                        continue                         # in the lakehouse already (a replay after a crash)
                    if msg[0] == "truncate":
                        truncated = True
                        changes = []                     # what came before is emptied anyway
                        continue
                    kind, _, row, unchanged = msg[:4]
                    if unchanged:
                        row = self._fill_unchanged(conn, r, row, unchanged)
                    if kind == "update":
                        old = msg[4]
                        key = r.key.split(",")
                        if any(old.get(k) != row.get(k) for k in key):   # the key changed: the old row goes
                            changes.append((pos, "delete", {k: old.get(k) for k in key}))
                        kind = "upsert"
                    changes.append((pos, kind, row))
                end = lsn_int(got[last_commit][0])
                written = 0
                if truncated:
                    self._truncate(r)
                if changes:
                    # Offsets are LSNs, unique per change; the commit records the next one.
                    written = self._write(r, changes)
                conn.execute("SELECT pg_replication_slot_advance(%s, %s::pg_lsn)", (slot, lsn_text(end)))
            with self.engine.begin() as c:
                c.execute(update(mirrors_table).where(mirrors_table.c.id == r.id).values(
                    status="running", error=None, rows=mirrors_table.c.rows + written, last_lsn=lsn_text(end),
                    last_commit_ms=self.clock()))
            return written
        except Exception as e:  # noqa: BLE001 - shown on the mirror, tried again next tick
            logger.warning("mirror %s failed: %s", name, e)
            self._status(r, "failed", f"{type(e).__name__}: {e}"[:2000])
            return 0

    def _run_mysql(self, r) -> int:
        """Brings a MySQL mirror up to date from the binary log, whole transactions per commit."""
        from cairn.pipelines import mirror_mysql as my

        url = self.source_url(r.connection)
        schema, table = r.source.split(".")
        try:
            if not r.snapshot_done:
                conn = my.connect(url)
                try:
                    log_file, pos = my.position(conn)    # changes from here on follow the copy and converge on it
                    copied, offset = 0, 0
                    for rows in my.copy(conn, schema, table):
                        batch = [(offset + i, "upsert", {k: _plain(v) for k, v in row.items()})
                                 for i, row in enumerate(rows)]
                        copied += self._write(r, batch, topic=f"pg.{r.id}.copy")
                        offset += len(rows)
                finally:
                    conn.close()
                with self.engine.begin() as c:
                    c.execute(update(mirrors_table).where(mirrors_table.c.id == r.id).values(
                        snapshot_done=True, rows=mirrors_table.c.rows + copied, last_lsn=f"{log_file}:{pos}"))
                r = self._row(r.name)
            log_file, pos = r.last_lsn.rsplit(":", 1)
            committed = self.streams.offsets(self._stream(r)).get(0, 0)
            got, end = my.changes(url, schema, table, log_file, int(pos), r.key.split(","))
            got = [(o, op, {k: _plain(v) for k, v in row.items()}) for o, op, row in got if o >= committed]
            written = 0
            batch: list = []
            for o, op, row in got + [(None, "end", None)]:
                if op in ("truncate", "end"):
                    if batch:
                        written += self._write(r, batch)
                        batch = []
                    if op == "truncate":
                        self._truncate(r)
                    continue
                batch.append((o, op, row))
            values = {"status": "running", "error": None, "last_commit_ms": self.clock(),
                      "rows": mirrors_table.c.rows + written}
            if end is not None:
                values["last_lsn"] = f"{end[0]}:{end[1]}"
            with self.engine.begin() as c:
                c.execute(update(mirrors_table).where(mirrors_table.c.id == r.id).values(**values))
            return written
        except Exception as e:  # noqa: BLE001 - shown on the mirror, tried again next tick
            msg = f"{type(e).__name__}: {e}"
            if "1236" in msg or "purged" in msg.lower():
                msg = ("the binary log this mirror had read to is gone from the server (purged): delete the mirror "
                       "and create it again to copy the table afresh. " + msg)
            logger.warning("mirror %s failed: %s", r.name, msg)
            self._status(r, "failed", msg[:2000])
            return 0

    def _run_mssql(self, r) -> int:
        """Brings a SQL Server mirror up to date from change data capture, whole transactions per commit."""
        from cairn.pipelines import mirror_mssql as ms

        schema, table = r.source.split(".")
        try:
            conn = ms.connect(self.source_url(r.connection))
            try:
                key, instance = ms.check(conn, schema, table)
                if not r.snapshot_done:
                    start = ms.position(conn, instance)  # changes after it follow the copy and converge on it
                    copied, offset = 0, 0
                    for rows in ms.copy(conn, schema, table):
                        batch = [(offset + i, "upsert", {k: _plain(v) for k, v in row.items()})
                                 for i, row in enumerate(rows)]
                        copied += self._write(r, batch, topic=f"pg.{r.id}.copy")
                        offset += len(rows)
                    with self.engine.begin() as c:
                        c.execute(update(mirrors_table).where(mirrors_table.c.id == r.id).values(
                            snapshot_done=True, rows=mirrors_table.c.rows + copied, last_lsn=ms.lsn_hex(start)))
                    r = self._row(r.name)
                committed = self.streams.offsets(self._stream(r)).get(0, 0)
                got, end = ms.changes(conn, instance, ms.lsn_bytes(r.last_lsn), key)
            finally:
                conn.close()
            got = [(o, op, {k: _plain(v) for k, v in row.items()}) for o, op, row in got if o >= committed]
            written = self._write(r, got) if got else 0
            values = {"status": "running", "error": None, "last_commit_ms": self.clock(),
                      "rows": mirrors_table.c.rows + written}
            if end is not None:
                values["last_lsn"] = ms.lsn_hex(end)
            with self.engine.begin() as c:
                c.execute(update(mirrors_table).where(mirrors_table.c.id == r.id).values(**values))
            return written
        except Exception as e:  # noqa: BLE001 - shown on the mirror, tried again next tick
            msg = f"{type(e).__name__}: {e}"
            logger.warning("mirror %s failed: %s", r.name, msg)
            self._status(r, "failed", msg[:2000])
            return 0

    def _truncate(self, r) -> None:
        from pyiceberg.exceptions import NoSuchTableError
        from pyiceberg.expressions import AlwaysTrue

        try:
            table = self.streams._catalog().load_table(tuple(r.target.split(".")))
        except NoSuchTableError:
            return
        table.delete(AlwaysTrue())

    def _status(self, r, status: str, error: Optional[str]) -> None:
        with self.engine.begin() as c:
            c.execute(update(mirrors_table).where(mirrors_table.c.id == r.id).values(status=status, error=error))

    def run_due(self) -> int:
        """Every enabled mirror, once (the scheduler's tick)."""
        with self.engine.connect() as c:
            names = [x.name for x in c.execute(select(mirrors_table.c.name).where(mirrors_table.c.enabled.is_(True)))]
        return sum(self.run(n) for n in names)


def _plain(v: Any) -> Any:
    """A value read with psycopg (the copy) as the decoder would give it."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dt.datetime) and v.tzinfo is not None:
        return v.astimezone(dt.timezone.utc)
    if isinstance(v, (dict, list)):
        import json

        return json.dumps(v)
    if isinstance(v, (bytes, memoryview)):
        return bytes(v).hex()
    if isinstance(v, uuid.UUID):
        return str(v)
    return v


def build_mirrors_router(m: Mirrors, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException, Response
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/mirrors", tags=["pipelines"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except MirrorError as e:
            raise HTTPException(e.status, str(e))

    def manage(p):
        authorize(p, "MANAGE_INGESTION", None)

    @router.get("")
    async def list_(p=Depends(current_principal)):
        manage(p)
        return {"mirrors": await call(m.list)}

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        manage(p)
        return await call(lambda: m.create(p.urn, body))

    @router.get("/{name}")
    async def get(name: str, p=Depends(current_principal)):
        manage(p)
        return await call(lambda: m.get(name))

    @router.post("/{name}/pause")
    async def pause(name: str, p=Depends(current_principal)):
        manage(p)
        return await call(lambda: m.set_enabled(p.urn, name, False))

    @router.post("/{name}/resume")
    async def resume(name: str, p=Depends(current_principal)):
        manage(p)
        return await call(lambda: m.set_enabled(p.urn, name, True))

    @router.post("/{name}/sync")
    async def sync(name: str, p=Depends(current_principal)):
        manage(p)
        return {"written": await call(lambda: m.run(name))}

    @router.delete("/{name}", status_code=204)
    async def delete(name: str, p=Depends(current_principal)):
        manage(p)
        await call(lambda: m.delete(p.urn, name))
        return Response(status_code=204)

    return router
