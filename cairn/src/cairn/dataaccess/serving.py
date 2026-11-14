"""Serving tables: a lakehouse table kept as a row store in PostgreSQL, for lookups in milliseconds, governed
exactly like a read of the table.

An owner of a lakehouse table (``MANAGE_DATASET`` on it) or an operator (``MANAGE_SYSTEM_OPERATIONS``) declares a
serving table over it (a table or a derived table in Cairn's Iceberg catalog):
``POST /v1/serving`` ``{"name", "table": "ns.table", "primary_key": [...], "indexes": [[...], ...], "writable"}``.
Cairn copies the table into a PostgreSQL database it manages (``CAIRN_SERVING_STORE_URL``, or a database
``cairn_serving`` it makes on the server it administers, ``CAIRN_DATABASES_ADMIN_URL``), one table per serving
table with the declared primary key and a B-tree index per secondary index, and keeps it in step with the Iceberg
table's snapshots on each scheduler tick:

* **append**: the new snapshots only added data files (no file removed, no delete file added): only the added
  files are read, and their rows are upserted by key (a key seen again is a changed row);
* **changed**: files were removed or rewritten (an update, a delete, a compaction, a rollback, or the last synced
  snapshot was expired): the table is read and compared row by row (a hash per row), and only the rows that
  differ are written, the keys that are gone are deleted;
* **full**: the table's columns changed (or ``{"full": true}``): a new row-store table is filled and swapped in.

``GET /v1/serving/<name>`` reports the freshness: the snapshot served, the source's current snapshot and the lag
(how long the oldest commit not yet served has waited; 0 when it is up to date).

**Reads.** ``GET /v1/serving/<name>/rows`` with ``key`` (repeatable, a one-column key), ``from``/``to`` (a range
of the first key column, ``from`` included, ``to`` not), ``filter=<column>:<op>:<value>`` (``eq ne lt le gt ge
in prefix null notnull``; ``in`` takes ``|``-separated values), ``columns``, ``limit`` (at most 1000) and
``order`` (``asc``/``desc`` by key); ``POST /v1/serving/<name>/lookup`` ``{"keys": [{...}], "columns"}`` looks up
to 1000 keys of any shape. Each answer is decided for the caller as the data preview decides a read of the source
table (the same checks, in :class:`cairn.dataaccess.service.DataAccessService`): the table must be in the catalog
and the caller need read access; a table under an aggregation or projection policy has no rows to serve; the
caller's **row filters** (policies and consent, transpiled to PostgreSQL) are applied in the query, so a filtered
key reads as ``missing``; **column masks** are applied to every value as Trino masks them
(:func:`cairn.ml.online.mask_value`); and a key, range or filter on a column masked for the caller is refused,
since comparing raw values would reveal them. A decision that cannot be made refuses (503). Reads run on a
read-only connection with a statement timeout. They are not audited one by one; declaring, dropping, syncing and
writing are.

**Writes (hybrid tables).** A serving table declared ``"writable": true`` takes
``POST /v1/serving/<name>/rows`` ``{"rows": [{...}]}`` from the source table's owners only (``MANAGE_DATASET``):
each row (all key columns, any others) is merged into the row store at once, readable at once, and kept as a
pending write; the next sync first upserts the pending rows into the Iceberg table by key (one commit), then
syncs as usual. A key with a pending write is never overwritten by a sync in between.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import secrets
import threading
import time
import uuid as _uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from fastapi import Request
from sqlalchemy import (BigInteger, Boolean, Column, Date, DateTime, Index, Integer, LargeBinary, MetaData, Numeric,
                        PrimaryKeyConstraint, String, Table, Text, Time, and_, delete, func, insert, literal_column,
                        select, text, tuple_, update)

from cairn.common.db import metadata
from cairn.ml.online import encode, mask_value

logger = logging.getLogger(__name__)

SCHEMA = "cairn_serving"
NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
TABLE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+$")
HIDDEN = "_cairn_hash"
MAX_LIMIT = 1000
MAX_KEYS = 1000
MAX_INDEXES = 8
BATCH = 500
OPS = {"eq", "ne", "lt", "le", "gt", "ge", "in", "prefix", "null", "notnull"}

serving_tables = Table(
    "cairn_serving_tables", metadata,
    Column("name", String(40), primary_key=True),
    Column("source", String(512), nullable=False),           # ns.table in Cairn's Iceberg catalog
    Column("source_urn", String(1024), nullable=False),
    Column("primary_key", Text, nullable=False),              # JSON [column, ...]
    Column("indexes", Text, nullable=False),                  # JSON [[column, ...], ...]
    Column("writable", Boolean, nullable=False, default=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("pg_table", String(63), nullable=True),           # the row-store table now served
    Column("types", Text, nullable=True),                     # JSON [[column, Iceberg type], ...] as last synced
    Column("synced_ms", BigInteger, nullable=True),
    Column("synced_snapshot", BigInteger, nullable=True),
    Column("synced_snapshot_ms", BigInteger, nullable=True),  # when that snapshot was committed
    Column("source_snapshot", BigInteger, nullable=True),     # the source's current snapshot when last checked
    Column("behind_since_ms", BigInteger, nullable=True),     # the oldest commit not served, when last checked
    Column("rows", BigInteger, nullable=True),
    Column("last_mode", String(16), nullable=True),
    Column("last_written", Integer, nullable=True),
    Column("last_removed", Integer, nullable=True),
    Column("last_duration_ms", Integer, nullable=True),
    Column("error", String(2000), nullable=True),
)

_store_meta = MetaData(schema=SCHEMA)
pending_writes = Table(                                       # in the serving database, beside the rows
    "_cairn_pending", _store_meta,
    Column("serving_table", String(40), primary_key=True),
    Column("key_hash", String(64), primary_key=True),
    Column("row", Text, nullable=False),                      # the whole row, JSON (values as encode() keeps them)
    Column("written_by", String(512), nullable=False),
    Column("written_ms", BigInteger, nullable=False),
)


class ServingError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- types and values
def _base(t: str) -> str:
    return (t or "").lower().strip().split("(", 1)[0].strip()


def _decimal_spec(t: str) -> tuple[int, int]:
    if "(" not in t:
        return 38, 0
    inner = t[t.find("(") + 1:t.rfind(")")].split(",")
    return int(inner[0]), int(inner[1]) if len(inner) > 1 else 0


PRIMITIVE = {"boolean", "int", "integer", "long", "bigint", "float", "double", "decimal", "date", "time", "timestamp",
             "timestamptz", "timestamp_ns", "timestamptz_ns", "string", "uuid"}
WRITABLE = PRIMITIVE - {"timestamp_ns", "timestamptz_ns", "uuid"}


def pg_type(t: str):
    from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, JSONB, REAL

    base = _base(t)
    if base == "boolean":
        return Boolean()
    if base in ("int", "integer"):
        return Integer()
    if base in ("long", "bigint"):
        return BigInteger()
    if base == "float":
        return REAL()
    if base == "double":
        return DOUBLE_PRECISION()
    if base == "decimal":
        p, s = _decimal_spec(t)
        return Numeric(p, s, asdecimal=True)
    if base == "date":
        return Date()
    if base == "time":
        return Time()
    if base in ("timestamp", "timestamp_ns"):
        return DateTime(timezone=False)
    if base in ("timestamptz", "timestamptz_ns"):
        return DateTime(timezone=True)
    if base in ("string", "uuid"):
        return Text()
    if base in ("binary", "fixed"):
        return LargeBinary()
    return JSONB()                                            # list<...>, map<...>, struct<...>


def _db_value(v: Any, t: str) -> Any:
    """A value read from the lakehouse (Arrow, as Python) as the row store keeps it."""
    if v is None:
        return None
    base = _base(t)
    if base == "uuid":
        return str(_uuid.UUID(bytes=bytes(v))) if isinstance(v, (bytes, bytearray)) else str(v)
    if base in ("timestamptz", "timestamptz_ns") and isinstance(v, dt.datetime):
        return v.astimezone(dt.timezone.utc)
    if base not in PRIMITIVE and base not in ("binary", "fixed"):
        return encode(v)
    return v


def to_python(value: Any, t: str, column: str) -> Any:
    """A caller's value (JSON or query text) as the column's Python value; refuses what is not one."""
    if value is None:
        return None
    base = _base(t)
    bad = ServingError(f"{column}: {value!r} is not a {t}")
    try:
        if base in ("int", "integer", "long", "bigint"):
            if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
                raise bad
            v = int(value)
            if base in ("int", "integer") and not -2 ** 31 <= v < 2 ** 31:
                raise bad
            return v
        if base in ("float", "double"):
            if isinstance(value, bool):
                raise bad
            return float(value)
        if base == "decimal":
            if isinstance(value, bool):
                raise bad
            p, s = _decimal_spec(t)
            d = Decimal(str(value)).quantize(Decimal(1).scaleb(-s))
            if len(d.as_tuple().digits) > p:
                raise bad
            return d
        if base == "boolean":
            if isinstance(value, bool):
                return value
            if str(value).lower() in ("true", "false"):
                return str(value).lower() == "true"
            raise bad
        if base == "date":
            return dt.date.fromisoformat(str(value))
        if base == "time":
            return dt.time.fromisoformat(str(value))
        if base in ("timestamp", "timestamp_ns"):
            v = dt.datetime.fromisoformat(str(value))
            return v.astimezone(dt.timezone.utc).replace(tzinfo=None) if v.tzinfo else v
        if base in ("timestamptz", "timestamptz_ns"):
            v = dt.datetime.fromisoformat(str(value))
            if v.tzinfo is None:
                raise ServingError(f"{column}: {value!r} needs a time zone offset")
            return v.astimezone(dt.timezone.utc)
        if base == "string":
            if isinstance(value, (dict, list)):
                raise bad
            return str(value)
        if base == "uuid":
            return str(_uuid.UUID(str(value)))
    except ServingError:
        raise
    except (ValueError, TypeError, ArithmeticError, InvalidOperation):
        raise bad from None
    raise ServingError(f"{column}: a {t} column cannot be compared or written here")


def _row_hash(cols: list[str], row: dict) -> str:
    return hashlib.sha256(json.dumps([encode(row.get(c)) for c in cols], default=str,
                                     separators=(",", ":")).encode()).hexdigest()


def _key_hash(key: tuple) -> str:
    return hashlib.sha256(json.dumps(list(key), default=str, separators=(",", ":")).encode()).hexdigest()


def read_trino_position_deletes() -> None:
    """Lets pyiceberg read the position-delete files Trino writes (an UPDATE, DELETE or MERGE on an Iceberg table).

    pyiceberg reads a delete file's ``file_path`` column dictionary-encoded, and Arrow cannot decode Trino's
    DELTA_LENGTH_BYTE_ARRAY encoding that way ("DecodeArrow of DictAccumulator for DeltaLengthByteArrayDecoder").
    Only on that error, the file is read again plainly; the result is the same (positions per data file)."""
    import pyiceberg.io.pyarrow as pio

    original = pio._read_deletes
    if getattr(original, "_cairn_plain_fallback", False):
        return

    def read_deletes(io, data_file):
        try:
            return original(io, data_file)
        except OSError as e:
            from pyiceberg.manifest import FileFormat

            if "DeltaLengthByteArray" not in str(e) or data_file.file_format != FileFormat.PARQUET:
                raise
            import pyarrow.compute as pc
            import pyarrow.parquet as pq

            with io.new_input(data_file.file_path).open() as fh:
                t = pq.read_table(fh, columns=["file_path", "pos"])
            return {path.as_py(): t.filter(pc.equal(t["file_path"], path)).column("pos")
                    for path in pc.unique(t["file_path"])}
    read_deletes._cairn_plain_fallback = True
    pio._read_deletes = read_deletes


def ensure_database(admin_dsn: str, dbname: str = "cairn_serving") -> str:
    """Makes ``dbname`` on the server Cairn administers (once), connectable by nobody else; its connection string."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    if not NAME.match(dbname):
        raise ServingError("CAIRN_SERVING_DATABASE: lowercase letters, digits and '_'", 503)
    with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=10) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone() is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
            conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(dbname)))
    info = conninfo_to_dict(admin_dsn)
    info["dbname"] = dbname
    return make_conninfo(**info)


# --------------------------------------------------------------------------- the service
class ServingTables:
    def __init__(self, engine, catalog: Callable[[], Any], store_dsn: Callable[[], str], access,
                 may: Callable[[Any, str, Optional[str]], bool], urn_of: Callable[[str], str], audit=None,
                 statement_timeout_ms: int = 5000, clock: Callable[[], int] = _now):
        """``catalog()``: Cairn's own Iceberg catalog client (reads the tables unmasked); ``store_dsn()``: the
        serving database's connection string; ``access``: the DataAccessService whose read decisions (catalog,
        read access, query policies, row filters, masks) every answer goes through; ``may(principal, privilege,
        urn)``: Cairn's authorization; ``urn_of("ns.table")``: the table's dataset URN."""
        self.engine, self.catalog, self.store_dsn, self.access, self.may, self.urn_of, self.audit = \
            engine, catalog, store_dsn, access, may, urn_of, audit
        self.timeout_ms, self.clock = int(statement_timeout_ms), clock
        self._write = self._read = None
        self._init = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._phys_cache: dict[tuple, Table] = {}
        try:
            read_trino_position_deletes()
        except ImportError:                                   # no pyiceberg here: syncs fail and say so
            pass

    # ---- the serving database ----------------------------------------------------------------
    def _engines(self):
        if self._write is not None:
            return self._write, self._read
        with self._init:
            if self._write is None:
                import psycopg
                from sqlalchemy import create_engine

                dsn = self.store_dsn()
                if not dsn:
                    raise ServingError("serving tables have no database", 503)
                ro = f"-c TimeZone=UTC -c default_transaction_read_only=on -c statement_timeout={self.timeout_ms}"
                write = create_engine("postgresql+psycopg://", pool_pre_ping=True,
                                      creator=lambda: psycopg.connect(dsn, options="-c TimeZone=UTC", connect_timeout=10))
                read = create_engine("postgresql+psycopg://", pool_pre_ping=True, pool_size=10,
                                     creator=lambda: psycopg.connect(dsn, options=ro, connect_timeout=10))
                with write.begin() as conn:
                    conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
                pending_writes.create(write, checkfirst=True)
                self._write, self._read = write, read
        return self._write, self._read

    def _phys(self, c: dict, pg_table: Optional[str] = None, types: Optional[list] = None) -> Table:
        pg_table = pg_table or c["pg_table"]
        types = types if types is not None else json.loads(c["types"] or "[]")
        pk, indexes = json.loads(c["primary_key"]), json.loads(c["indexes"])
        key = (pg_table, json.dumps(types), json.dumps(pk), json.dumps(indexes))
        t = self._phys_cache.get(key)
        if t is None:
            gen = pg_table.rsplit("_", 1)[-1]
            t = Table(pg_table, MetaData(schema=SCHEMA),
                      *[Column(n, pg_type(ty), nullable=n not in pk) for n, ty in types],
                      Column(HIDDEN, String(64), nullable=False),
                      PrimaryKeyConstraint(*pk, name=f"pk_{gen}"))
            for i, cols in enumerate(indexes):
                Index(f"ix_{gen}_{i}", *[t.c[x] for x in cols])
            self._phys_cache[key] = t
        return t

    # ---- configuration -------------------------------------------------------------------------
    def _config(self, name: str) -> Optional[dict]:
        with self.engine.connect() as conn:
            r = conn.execute(select(serving_tables).where(serving_tables.c.name == name)).first()
        return dict(r._mapping) if r else None

    def _get(self, name: str) -> dict:
        c = self._config(name)
        if c is None:
            raise ServingError("no such serving table", 404)
        return c

    def _allowed(self, p, privilege: str, urn: Optional[str]) -> bool:
        try:
            return bool(self.may(p, privilege, urn))
        except Exception:  # noqa: BLE001 - fail closed
            logger.warning("serving decision failed (%s %s %s)", p.urn, privilege, urn, exc_info=True)
            return False

    def _manager(self, p, c: dict) -> None:
        if not (self._allowed(p, "MANAGE_DATASET", c["source_urn"]) or
                self._allowed(p, "MANAGE_SYSTEM_OPERATIONS", None)):
            raise ServingError(f"managing serving table {c['name']} needs MANAGE_DATASET on {c['source']}", 403)

    def _visible(self, p, c: dict) -> None:
        if not self._allowed(p, "VIEW_ENTITY_PAGE", c["source_urn"]):
            raise ServingError("no such serving table", 404)

    def _record(self, action: str, actor: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("data", action, actor, f"urn:li:servingTable:{name}", details)

    def _pending_count(self, name: str) -> int:
        write, _ = self._engines()
        with write.connect() as conn:
            return conn.execute(select(func.count()).select_from(pending_writes)
                                .where(pending_writes.c.serving_table == name)).scalar() or 0

    def _public(self, c: dict, fresh: Optional[dict] = None) -> dict:
        now = self.clock()
        lag = fresh["lag_ms"] if fresh else (now - c["behind_since_ms"] if c["behind_since_ms"] else 0)
        return {"name": c["name"], "table": c["source"], "source_urn": c["source_urn"],
                "primary_key": json.loads(c["primary_key"]), "indexes": json.loads(c["indexes"]),
                "writable": bool(c["writable"]), "enabled": bool(c["enabled"]), "created_by": c["created_by"],
                "created_ms": c["created_ms"], "rows": c["rows"], "synced_ms": c["synced_ms"],
                "synced_snapshot": c["synced_snapshot"], "synced_snapshot_ms": c["synced_snapshot_ms"],
                "source_snapshot": fresh["source_snapshot"] if fresh else c["source_snapshot"],
                "behind": bool(lag), "lag_ms": lag, "behind_snapshots": fresh["behind_snapshots"] if fresh else None,
                "columns": [{"name": n, "type": t} for n, t in json.loads(c["types"] or "[]")],
                "last_sync": {"mode": c["last_mode"], "written": c["last_written"], "removed": c["last_removed"],
                              "duration_ms": c["last_duration_ms"]},
                "error": c["error"]}

    def list(self, p) -> list[dict]:
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(select(serving_tables).order_by(serving_tables.c.name)).all()]
        return [self._public(c) for c in rows if self._allowed(p, "VIEW_ENTITY_PAGE", c["source_urn"])]

    def status(self, p, name: str) -> dict:
        c = self._get(name)
        self._visible(p, c)
        try:
            fresh = self._freshness(c, self._load(c))
        except Exception as e:  # noqa: BLE001 - the catalog is down: what was last seen
            logger.warning("serving table %s: freshness unknown: %s", name, e)
            fresh = None
        out = self._public(c, fresh)
        out["pending_writes"] = self._pending_count(name)
        return out

    def create(self, p, body: dict) -> dict:
        name, table = body.get("name"), body.get("table")
        pk, indexes, writable = body.get("primary_key"), body.get("indexes") or [], body.get("writable", False)
        if not isinstance(name, str) or not NAME.match(name):
            raise ServingError("name: lowercase letters, digits and '_' (up to 40), starting with a letter")
        if not isinstance(table, str) or not TABLE.match(table):
            raise ServingError("table: namespace.table in the lakehouse catalog")
        if not isinstance(pk, list) or not pk or not all(isinstance(x, str) and x for x in pk) or len(set(pk)) != len(pk):
            raise ServingError("primary_key: a list of distinct column names")
        if not isinstance(indexes, list) or len(indexes) > MAX_INDEXES or not all(
                isinstance(i, list) and i and all(isinstance(x, str) and x for x in i) for i in indexes):
            raise ServingError(f"indexes: up to {MAX_INDEXES} lists of column names")
        if not isinstance(writable, bool):
            raise ServingError("writable: true or false")
        urn = self.urn_of(table)
        c = {"name": name, "source": table, "source_urn": urn}
        self._manager(p, c)
        if self.access is not None and not self.access.known(urn):
            raise ServingError(f"{table} is not in the catalog", 404)
        if self._config(name) is not None:
            raise ServingError(f"a serving table {name} exists", 409)
        self._engines()
        with self.engine.begin() as conn:
            conn.execute(insert(serving_tables).values(
                name=name, source=table, source_urn=urn, primary_key=json.dumps(pk), indexes=json.dumps(indexes),
                writable=writable, enabled=True, created_by=p.urn, created_ms=self.clock()))
        try:
            result = self.sync(name, full=True)
        except Exception as e:
            self._drop(self._get(name))                       # nothing half-made
            if isinstance(e, ServingError):
                raise
            raise ServingError(f"{name} could not be filled: {e}", 400)
        self._record("serving.created", p.urn, name, {"table": table, "primary_key": pk, "indexes": indexes,
                                                       "writable": writable, "rows": result.get("rows")})
        return self._public(self._get(name))

    def _drop(self, c: dict) -> None:
        write, _ = self._engines()
        with write.begin() as conn:
            for t in self._physical_tables(conn, c["name"]):
                conn.execute(text(f'DROP TABLE IF EXISTS "{SCHEMA}"."{t}"'))
            conn.execute(delete(pending_writes).where(pending_writes.c.serving_table == c["name"]))
        with self.engine.begin() as conn:
            conn.execute(delete(serving_tables).where(serving_tables.c.name == c["name"]))

    @staticmethod
    def _physical_tables(conn, name: str) -> list[str]:
        prefix = f"st_{name}_"
        found = conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"),
                             {"s": SCHEMA}).scalars().all()
        return [t for t in found if t.startswith(prefix) and re.fullmatch(r"[0-9a-f]{8}", t[len(prefix):])]

    def drop(self, p, name: str) -> None:
        c = self._get(name)
        self._visible(p, c)
        self._manager(p, c)
        pending = self._pending_count(name)
        if pending and not self._allowed(p, "MANAGE_DATASET", c["source_urn"]):
            raise ServingError(f"{name} has {pending} writes not yet in the lakehouse table; sync it first", 409)
        self._drop(c)
        self._record("serving.dropped", p.urn, name, {"table": c["source"], "pending_dropped": pending})

    def sync_now(self, p, name: str, full: bool = False) -> dict:
        c = self._get(name)
        self._visible(p, c)
        self._manager(p, c)
        out = self.sync(name, force=True, full=full)
        self._record("serving.synced", p.urn, name, {k: out.get(k) for k in ("mode", "written", "removed", "snapshot")})
        return out

    # ---- keeping it in step ----------------------------------------------------------------------
    def _load(self, c: dict):
        return self.catalog().load_table(tuple(c["source"].split(".")))

    def _freshness(self, c: dict, table) -> dict:
        cur = table.current_snapshot()
        sid = cur.snapshot_id if cur else 0
        if cur is None or sid == c["synced_snapshot"]:
            return {"source_snapshot": sid, "lag_ms": 0, "behind_snapshots": 0, "behind_since_ms": None}
        waiting, s = [], cur
        while s is not None and s.snapshot_id != c["synced_snapshot"]:
            waiting.append(s)
            s = table.snapshot_by_id(s.parent_snapshot_id) if s.parent_snapshot_id is not None else None
        oldest = min(x.timestamp_ms for x in waiting)
        return {"source_snapshot": sid, "lag_ms": max(0, self.clock() - oldest), "behind_snapshots": len(waiting),
                "behind_since_ms": oldest}

    def _advisory(self, conn, name: str) -> bool:
        k = int.from_bytes(hashlib.sha256(f"cairn-serving:{name}".encode()).digest()[:8], "big", signed=True)
        return bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": k}).scalar())

    def sync(self, name: str, force: bool = False, full: bool = False) -> dict:
        """Writes pending writes back to the lakehouse table, then brings the row store to its current snapshot,
        reading only what changed where it can. Nothing is read when the snapshot has not moved (unless forced)."""
        write, _ = self._engines()
        lock = self._locks.setdefault(name, threading.Lock())
        with lock, write.connect() as guard:
            if not self._advisory(guard, name):               # another Cairn replica is syncing it
                return {"serving_table": name, "changed": False, "busy": True}
            try:
                return self._sync(name, force, full)
            finally:
                guard.execute(text("SELECT pg_advisory_unlock_all()"))
                guard.commit()

    def _sync(self, name: str, force: bool, full: bool) -> dict:
        started = time.monotonic()
        c = self._get(name)
        wrote_back = self._write_back(c)
        table = self._load(c)
        snap = table.current_snapshot()
        sid = snap.snapshot_id if snap else 0
        types = [[f.name, str(f.field_type)] for f in table.schema().fields]
        schema_changed = c["types"] is None or json.loads(c["types"]) != types or c["pg_table"] is None
        if not (force or full or schema_changed or wrote_back) and c["synced_snapshot"] == sid:
            return {"serving_table": name, "changed": False, "mode": "none", "written": 0, "removed": 0,
                    "rows": c["rows"], "snapshot": sid}
        self._check_columns(c, types)
        if full or schema_changed:
            mode, written, removed, pg_table = "full", *self._rebuild(c, table, snap, types)
        else:
            pg_table = c["pg_table"]
            mode, written, removed = self._incremental(c, table, snap, types)
        write, _ = self._engines()
        phys = self._phys(c, pg_table, types)
        with write.connect() as conn:
            rows = conn.execute(select(func.count()).select_from(phys)).scalar()
        with self.engine.begin() as conn:
            conn.execute(update(serving_tables).where(serving_tables.c.name == name).values(
                pg_table=pg_table, types=json.dumps(types), synced_ms=self.clock(), synced_snapshot=sid,
                synced_snapshot_ms=snap.timestamp_ms if snap else None, source_snapshot=sid, behind_since_ms=None,
                rows=rows, last_mode=mode, last_written=written, last_removed=removed,
                last_duration_ms=int((time.monotonic() - started) * 1000), error=None))
        if mode == "full":                                    # the old row store, once nothing points at it
            with write.begin() as conn:
                for t in self._physical_tables(conn, name):
                    if t != pg_table:
                        conn.execute(text(f'DROP TABLE IF EXISTS "{SCHEMA}"."{t}"'))
        return {"serving_table": name, "changed": True, "mode": mode, "written": written, "removed": removed,
                "rows": rows, "snapshot": sid, "written_back": wrote_back}

    def _check_columns(self, c: dict, types: list) -> None:
        known = dict(types)
        pk, indexes = json.loads(c["primary_key"]), json.loads(c["indexes"])
        for col in [*pk, *[x for i in indexes for x in i]]:
            if col not in known:
                raise ServingError(f"{c['source']} has no column {col} (any more)")
            if _base(known[col]) not in PRIMITIVE:
                raise ServingError(f"{col} is a {known[col]}: keys and indexes are on simple columns")
        for col in known:
            if col.startswith("_cairn_") or len(col.encode()) > 63:
                raise ServingError(f"{c['source']}: column {col} cannot be served (a reserved name or too long)")
        if c["writable"]:
            odd = [col for col, t in types if _base(t) not in WRITABLE]
            if odd:
                raise ServingError(f"a writable serving table has simple columns only; {odd[0]} is a {known[odd[0]]}")

    def _batches(self, table, snap, tasks=None):
        """The rows of a snapshot (or of some of its files), batch by batch, as the row store keeps them."""
        from pyiceberg.expressions import AlwaysTrue
        from pyiceberg.io.pyarrow import ArrowScan

        if snap is None:
            return
        if tasks is None:
            tasks = list(table.scan(snapshot_id=snap.snapshot_id).plan_files())
        types = {f.name: str(f.field_type) for f in table.schema().fields}
        for batch in ArrowScan(table.metadata, table.io, table.schema(), AlwaysTrue()).to_record_batches(tasks):
            yield [{k: _db_value(v, types.get(k, "string")) for k, v in r.items()} for r in batch.to_pylist()]

    def _upsert(self, conn, phys: Table, pk: list[str], rows: list[dict]) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        if not rows:
            return
        stmt = pg_insert(phys)
        stmt = stmt.on_conflict_do_update(index_elements=[phys.c[k] for k in pk], set_={
            col.name: stmt.excluded[col.name] for col in phys.columns if col.name not in pk})
        conn.execute(stmt, rows)

    def _prepare(self, raw: list[dict], cols: list[str], pk: list[str], skip: set) -> dict:
        """key -> row (with its hash) for a batch, the last row of a repeated key winning; null keys skipped."""
        out: dict[tuple, dict] = {}
        for r in raw:
            key = tuple(encode(r.get(k)) for k in pk)
            if any(k is None for k in key) or key in skip:
                continue
            row = {col: r.get(col) for col in cols}
            row[HIDDEN] = _row_hash(cols, row)
            out[key] = row
        return out

    def _pending_keys(self, name: str) -> set:
        write, _ = self._engines()
        with write.connect() as conn:
            rows = conn.execute(select(pending_writes.c.row).where(pending_writes.c.serving_table == name)).scalars().all()
        c = self._get(name)
        pk = json.loads(c["primary_key"])
        return {tuple(json.loads(r).get(k) for k in pk) for r in rows}

    def _rebuild(self, c: dict, table, snap, types: list) -> tuple[int, int, str]:
        write, _ = self._engines()
        pg_table = f"st_{c['name']}_{secrets.token_hex(4)}"
        phys = self._phys(c, pg_table, types)
        cols, pk = [n for n, _ in types], json.loads(c["primary_key"])
        written = 0
        with write.begin() as conn:
            for t in self._physical_tables(conn, c["name"]):     # left by a rebuild that did not finish
                if t != c["pg_table"]:
                    conn.execute(text(f'DROP TABLE IF EXISTS "{SCHEMA}"."{t}"'))
            phys.create(conn)
            for raw in self._batches(table, snap) or []:
                rows = list(self._prepare(raw, cols, pk, set()).values())
                for i in range(0, len(rows), BATCH):
                    self._upsert(conn, phys, pk, rows[i:i + BATCH])
                written += len(raw)
        return written, 0, pg_table

    def _incremental(self, c: dict, table, snap, types: list) -> tuple[str, int, int]:
        write, _ = self._engines()
        phys = self._phys(c, c["pg_table"], types)
        cols, pk = [n for n, _ in types], json.loads(c["primary_key"])
        skip = self._pending_keys(c["name"])
        new_tasks = list(table.scan(snapshot_id=snap.snapshot_id).plan_files()) if snap else []
        old = table.snapshot_by_id(c["synced_snapshot"]) if c["synced_snapshot"] else None
        mode = "changed"
        added = None
        if old is not None:
            old_tasks = list(table.scan(snapshot_id=old.snapshot_id).plan_files())
            old_paths = {t.file.file_path for t in old_tasks}
            new_paths = {t.file.file_path for t in new_tasks}
            deletes = lambda ts: {d.file_path for t in ts for d in (t.delete_files or ())}  # noqa: E731
            if old_paths <= new_paths and deletes(old_tasks) == deletes(new_tasks):
                mode = "append"
                added = [t for t in new_tasks if t.file.file_path not in old_paths]
        written = removed = 0
        if mode == "append":
            with write.begin() as conn:
                for raw in self._batches(table, snap, added) if added else []:
                    rows = list(self._prepare(raw, cols, pk, skip).values())
                    for i in range(0, len(rows), BATCH):
                        self._upsert(conn, phys, pk, rows[i:i + BATCH])
                    written += len(rows)
            return mode, written, 0
        with write.connect() as conn:
            have = {tuple(encode(v) for v in r[:-1]): r[-1] for r in conn.execute(
                select(*[phys.c[k] for k in pk], phys.c[HIDDEN])).all()}
        seen: set = set()
        with write.begin() as conn:
            for raw in self._batches(table, snap) or []:
                batch = self._prepare(raw, cols, pk, skip)
                seen.update(batch)
                rows = [r for k, r in batch.items() if have.get(k) != r[HIDDEN]]
                for i in range(0, len(rows), BATCH):
                    self._upsert(conn, phys, pk, rows[i:i + BATCH])
                written += len(rows)
                for k, r in batch.items():
                    have[k] = r[HIDDEN]                       # a key repeated in a later batch is compared again
            gone = [k for k in have if k not in seen and k not in skip]
            types_of = dict(types)
            for i in range(0, len(gone), BATCH):
                keys = [tuple(to_python(v, types_of[col], col) for v, col in zip(k, pk)) for k in gone[i:i + BATCH]]
                conn.execute(delete(phys).where(tuple_(*[phys.c[k] for k in pk]).in_(keys)))
            removed = len(gone)
        return mode, written, removed

    def _write_back(self, c: dict) -> int:
        """Pending writes into the lakehouse table: one upsert by key; then they are no longer pending."""
        write, _ = self._engines()
        with write.connect() as conn:
            pend = conn.execute(select(pending_writes).where(pending_writes.c.serving_table == c["name"])
                                .order_by(pending_writes.c.written_ms)).all()
        if not pend:
            return 0
        import pyarrow as pa

        table = self._load(c)
        types = [[f.name, str(f.field_type)] for f in table.schema().fields]
        self._check_columns(c, types)
        pk = json.loads(c["primary_key"])
        rows = []
        for r in pend:
            vals = json.loads(r.row)
            rows.append({n: to_python(vals.get(n), t, n) for n, t in types})
        df = pa.Table.from_pylist(rows, schema=table.schema().as_arrow())
        # An upsert by key, in one commit: the rows with these keys out, the written rows in. (Not Table.upsert,
        # which reads the matches with the last snapshot's schema and refuses after a column was added.)
        from pyiceberg.expressions import And, EqualTo, In, Or

        if len(pk) == 1:
            match = In(pk[0], [r[pk[0]] for r in rows])
        else:
            each = [And(*[EqualTo(k, r[k]) for k in pk]) for r in rows]
            match = each[0] if len(each) == 1 else Or(*each)
        with table.transaction() as tx:
            tx.delete(match)
            tx.append(df)
        with write.begin() as conn:
            for r in pend:                                    # only what was written back (not a newer write)
                conn.execute(delete(pending_writes).where(
                    pending_writes.c.serving_table == c["name"], pending_writes.c.key_hash == r.key_hash,
                    pending_writes.c.written_ms == r.written_ms))
        self._record("serving.written_back", "urn:li:corpuser:cairn", c["name"], {"rows": len(rows)})
        return len(rows)

    def run_due(self) -> int:
        """The scheduler's tick: syncs each serving table whose source has a new snapshot (or pending writes)."""
        n = 0
        with self.engine.connect() as conn:
            names = conn.execute(select(serving_tables.c.name).where(serving_tables.c.enabled.is_(True))).scalars().all()
        for name in names:
            try:
                if self.sync(name).get("changed"):
                    n += 1
            except Exception as e:  # noqa: BLE001 - recorded; the others go on
                logger.warning("serving table %s: %s", name, e)
                values: dict = {"error": str(e)[:2000]}
                try:
                    c = self._get(name)
                    f = self._freshness(c, self._load(c))
                    values.update(source_snapshot=f["source_snapshot"], behind_since_ms=f["behind_since_ms"])
                except Exception:  # noqa: BLE001 - the error says enough
                    pass
                with self.engine.begin() as conn:
                    conn.execute(update(serving_tables).where(serving_tables.c.name == name).values(**values))
        return n

    # ---- reading ------------------------------------------------------------------------------------
    def _decide(self, p, c: dict):
        """(mask plan, row-filter SQL or None) for the caller, as the data preview decides a read; fails closed."""
        from cairn.dataaccess.service import DataAccessError

        urn, a = c["source_urn"], self.access
        if a is None:
            raise ServingError("reads cannot be decided here", 503)
        try:
            if not a.known(urn) or not a.can_read(p.urn, urn):
                raise ServingError(f"reading {c['name']} needs read access to {c['source']}", 403)
            if a._policies(p.urn, [urn]):
                raise ServingError(f"{c['source']} has an aggregation or projection policy: it has no rows to serve; "
                                   "query it in aggregate through Trino", 403)
            if a.row_filters is None:
                raise ServingError("row filters cannot be decided here", 503)
            cond = a._conditions(p.urn, urn, "postgres")
            plan = a.plan_for(p.urn, urn)
        except ServingError:
            raise
        except DataAccessError as e:
            raise ServingError(str(e), e.status)
        except Exception:  # noqa: BLE001 - fail closed
            logger.warning("serving read decision failed for %s on %s", p.urn, urn, exc_info=True)
            raise ServingError("the read could not be decided", 503)
        return plan, (cond.sql(dialect="postgres") if cond is not None else None)

    def _ready(self, c: dict) -> None:
        if not c["enabled"] or c["pg_table"] is None or c["synced_ms"] is None:
            raise ServingError(f"{c['name']} has not been filled yet", 409)

    def _run(self, c: dict, plan, cond: Optional[str], columns: Optional[list], where: list, used: set,
             limit: int, order: str = "asc") -> dict:
        types = dict(json.loads(c["types"]))
        phys = self._phys(c)
        pk = json.loads(c["primary_key"])
        cols = list(columns) if columns else list(types)
        unknown = [x for x in cols if x not in types]
        if unknown:
            raise ServingError(f"{c['name']} has no column {unknown[0]}")
        masked_all = {x for x in types if plan is not None and plan.masks_field(x)}
        touched = sorted(used & masked_all)
        if touched:
            raise ServingError(f"{touched[0]} is masked for you: it cannot be looked up, ranged or filtered on", 403)
        q = select(*[phys.c[x] for x in cols])
        if where:
            q = q.where(and_(*where))
        if cond:
            q = q.where(literal_column(f"({cond})"))
        keycols = [phys.c[k] for k in pk]
        q = q.order_by(*[k.desc() if order == "desc" else k.asc() for k in keycols]).limit(limit + 1)
        _, read = self._engines()
        try:
            with read.connect() as conn:
                got = conn.execute(q).all()
        except Exception as e:  # noqa: BLE001 - a row filter PostgreSQL cannot run refuses
            logger.warning("serving read of %s failed: %s", c["name"], e)
            if cond:
                raise ServingError("your row filter on this table could not be applied here; read it through Trino",
                                   403)
            raise ServingError("the read failed", 503)
        out = []
        for r in got[:limit]:
            row = {}
            for x, v in zip(cols, r):
                v = encode(v)
                if x in masked_all:
                    v = mask_value(v, types[x], plan.strategy_for(x) if hasattr(plan, "strategy_for") else plan.strategy)
                row[x] = v
            out.append(row)
        return {"serving_table": c["name"], "columns": cols, "rows": out, "truncated": len(got) > limit,
                "masked": sorted(set(cols) & masked_all), "row_filtered": cond is not None,
                "snapshot": c["synced_snapshot"], "synced_ms": c["synced_ms"]}

    def rows(self, p, name: str, keys: list[str], start: Optional[str], end: Optional[str], filters: list[str],
             columns: Optional[str], limit: int = 100, order: str = "asc") -> dict:
        c = self._get(name)
        self._visible(p, c)
        self._ready(c)
        plan, cond = self._decide(p, c)
        types = dict(json.loads(c["types"]))
        pk = json.loads(c["primary_key"])
        phys = self._phys(c)
        if not 1 <= limit <= MAX_LIMIT:
            raise ServingError(f"limit: 1 to {MAX_LIMIT}")
        if order not in ("asc", "desc"):
            raise ServingError("order: asc or desc")
        where, used = [], set()
        if keys:
            if len(pk) != 1:
                raise ServingError(f"{name} has a key of {len(pk)} columns: look keys up with POST .../lookup "
                                   "or filter on each key column")
            if len(keys) > MAX_KEYS:
                raise ServingError(f"at most {MAX_KEYS} keys")
            where.append(phys.c[pk[0]].in_([to_python(k, types[pk[0]], pk[0]) for k in keys]))
            used.add(pk[0])
        if start is not None:
            where.append(phys.c[pk[0]] >= to_python(start, types[pk[0]], pk[0]))
            used.add(pk[0])
        if end is not None:
            where.append(phys.c[pk[0]] < to_python(end, types[pk[0]], pk[0]))
            used.add(pk[0])
        for f in filters:
            col, op, value = (f.split(":", 2) + [None, None])[:3]
            if col not in types:
                raise ServingError(f"filter: {name} has no column {col}")
            if op not in OPS:
                raise ServingError(f"filter: the operator is one of {', '.join(sorted(OPS))}")
            if _base(types[col]) not in PRIMITIVE:
                raise ServingError(f"filter: {col} is a {types[col]}")
            used.add(col)
            colx = phys.c[col]
            if op == "null":
                where.append(colx.is_(None))
            elif op == "notnull":
                where.append(colx.is_not(None))
            elif value is None:
                raise ServingError("filter: <column>:<op>:<value>")
            elif op == "in":
                where.append(colx.in_([to_python(v, types[col], col) for v in value.split("|")]))
            elif op == "prefix":
                if _base(types[col]) != "string":
                    raise ServingError("filter: prefix is for text columns")
                where.append(colx.startswith(value, autoescape=True))
            else:
                v = to_python(value, types[col], col)
                where.append({"eq": colx == v, "ne": colx != v, "lt": colx < v, "le": colx <= v, "gt": colx > v,
                              "ge": colx >= v}[op])
        cols = [x.strip() for x in columns.split(",") if x.strip()] if columns else None
        return self._run(c, plan, cond, cols, where, used, limit, order)

    def lookup(self, p, name: str, keys: Any, columns: Any = None) -> dict:
        c = self._get(name)
        self._visible(p, c)
        self._ready(c)
        plan, cond = self._decide(p, c)
        types = dict(json.loads(c["types"]))
        pk = json.loads(c["primary_key"])
        if not isinstance(keys, list) or not keys:
            raise ServingError("keys: a list of key maps")
        if len(keys) > MAX_KEYS:
            raise ServingError(f"at most {MAX_KEYS} keys per lookup")
        if columns is not None and (not isinstance(columns, list) or not all(isinstance(x, str) for x in columns)):
            raise ServingError("columns: a list of column names")
        wanted = list(dict.fromkeys([*pk, *(columns or types)]))
        parsed = []
        for k in keys:
            if not isinstance(k, dict) or set(k) != set(pk):
                raise ServingError(f"each key names exactly the columns {', '.join(pk)}")
            parsed.append(tuple(to_python(k[x], types[x], x) for x in pk))
        phys = self._phys(c)
        where = [tuple_(*[phys.c[x] for x in pk]).in_(parsed)] if len(pk) > 1 else [phys.c[pk[0]].in_([x[0] for x in parsed])]
        res = self._run(c, plan, cond, wanted, where, set(pk), MAX_KEYS)
        masked = set(res["masked"])
        found = {tuple(r[x] for x in pk): r for r in res["rows"]}
        out = []
        for k, raw in zip(keys, parsed):
            r = found.get(tuple(encode(v) for v in raw))
            if r is None:
                out.append({"key": k, "status": "missing", "row": None})
            else:
                out.append({"key": k, "status": "found",
                            "row": {x: r[x] for x in (columns or types)} if columns else
                            {x: r[x] for x in types}})
        return {"serving_table": name, "results": out, "masked": sorted(masked & set(columns or types)),
                "row_filtered": res["row_filtered"], "snapshot": res["snapshot"], "synced_ms": res["synced_ms"]}

    # ---- writing (hybrid tables) --------------------------------------------------------------------
    def write(self, p, name: str, rows: Any) -> dict:
        c = self._get(name)
        self._visible(p, c)
        if not self._allowed(p, "MANAGE_DATASET", c["source_urn"]):
            raise ServingError(f"writing {name} needs MANAGE_DATASET on {c['source']} (its owners)", 403)
        if not c["writable"]:
            raise ServingError(f"{name} is not writable (declare it with \"writable\": true)", 409)
        self._ready(c)
        if not isinstance(rows, list) or not rows or len(rows) > MAX_KEYS:
            raise ServingError(f"rows: a list of 1 to {MAX_KEYS} rows")
        types_list = json.loads(c["types"])
        types, pk = dict(types_list), json.loads(c["primary_key"])
        cols = [n for n, _ in types_list]
        given = []
        for r in rows:
            if not isinstance(r, dict) or not r:
                raise ServingError("each row is a map of columns")
            extra = [k for k in r if k not in types]
            if extra:
                raise ServingError(f"{name} has no column {extra[0]}")
            if any(r.get(k) is None for k in pk):
                raise ServingError(f"each row names its key: {', '.join(pk)}")
            given.append({k: to_python(v, types[k], k) for k, v in r.items()})
        phys = self._phys(c)
        write, _ = self._engines()
        now = self.clock()
        with write.begin() as conn:
            for g in given:
                key = tuple(g[k] for k in pk)
                cur = conn.execute(select(*[phys.c[x] for x in cols]).where(
                    and_(*[phys.c[k] == v for k, v in zip(pk, key)])).with_for_update()).first()
                merged = {**(dict(zip(cols, cur)) if cur is not None else {x: None for x in cols}), **g}
                merged[HIDDEN] = _row_hash(cols, merged)
                self._upsert(conn, phys, pk, [merged])
                ekey = tuple(encode(v) for v in key)
                kh = _key_hash(ekey)
                conn.execute(delete(pending_writes).where(pending_writes.c.serving_table == name,
                                                          pending_writes.c.key_hash == kh))
                conn.execute(insert(pending_writes).values(
                    serving_table=name, key_hash=kh, written_by=p.urn, written_ms=now,
                    row=json.dumps({x: encode(merged[x]) for x in cols}, default=str)))
        self._record("serving.rows_written", p.urn, name, {"rows": len(given)})
        return {"serving_table": name, "written": len(given), "pending_writes": self._pending_count(name)}


# --------------------------------------------------------------------------- the API
def build_serving_router(svc: ServingTables, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException, Query
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/serving", tags=["serving"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except ServingError as e:
            raise HTTPException(e.status, str(e))

    def person(p):
        if p.is_service:
            raise HTTPException(403, "serving tables are read and managed as a person or a pipeline user")
        return p

    @router.get("")
    async def list_(p=Depends(current_principal)):
        return {"serving_tables": await call(lambda: svc.list(person(p)))}

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: svc.create(person(p), body))

    @router.get("/{name}")
    async def status(name: str, p=Depends(current_principal)):
        return await call(lambda: svc.status(person(p), name))

    @router.delete("/{name}", status_code=204)
    async def drop(name: str, p=Depends(current_principal)):
        await call(lambda: svc.drop(person(p), name))

    @router.post("/{name}/sync")
    async def sync(name: str, body: dict = Body(default_factory=dict), p=Depends(current_principal)):
        return await call(lambda: svc.sync_now(person(p), name, bool((body or {}).get("full"))))

    @router.get("/{name}/rows")
    async def rows(name: str, request: Request, key: list[str] = Query(default=[]),
                   filter: list[str] = Query(default=[]), to: Optional[str] = None,  # noqa: A002 - the query word
                   columns: Optional[str] = None, limit: int = 100, order: str = "asc", p=Depends(current_principal)):
        start = request.query_params.get("from")
        return await call(lambda: svc.rows(person(p), name, key, start, to, filter, columns, limit, order))

    @router.post("/{name}/lookup")
    async def lookup(name: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: svc.lookup(person(p), name, body.get("keys"), body.get("columns")))

    @router.post("/{name}/rows")
    async def write(name: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: svc.write(person(p), name, body.get("rows")))

    return router
