"""Database mirrors: a whole PostgreSQL schema kept in the lakehouse, table by table, and kept faithful as it changes.

``POST /v1/database-mirrors`` (``MANAGE_INGESTION``)::

    {"name": "crm", "connection": "crm", "schema": "public", "target_namespace": "crm_mirror",
     "include": ["*"], "exclude": ["tmp_*"],
     "defaults": {"history": false, "soft_delete": false},
     "tables": {"customers": {"history": true, "columns": {"exclude": ["notes"], "hash": ["email"]}},
                "audit_log": {"enabled": false}}}

Each ordinary table of ``schema`` whose name matches ``include`` and not ``exclude`` is mirrored into
``target_namespace.<table>``. (A single table is ``cairn.pipelines.mirror``; MySQL, SQL Server and MongoDB are
mirrored one table at a time there.)

* **One publication and one slot.** The publication is a list of tables that Cairn maintains
  (``ALTER PUBLICATION ... SET TABLE``); a table with excluded columns is published with a column list (PostgreSQL
  15 and later), so an excluded column never leaves the source.
* **New tables.** Each tick lists the schema's tables by their oid. A new one with a primary key is added to the
  publication and copied; one without is ``skipped`` with the reason, and picked up once it has a key. A renamed
  table (same oid) keeps its lakehouse table; a dropped one becomes ``source_dropped`` and its lakehouse table stays.
* **The copy** takes the table's ``SHARE`` lock for an instant, long enough to read one snapshot and the log
  position it is exact at: transactions that committed before that position are in the copy and are skipped for the
  table, later ones are applied. Nothing has to converge, which history mode needs.
* **Changes.** One read of the slot per tick, whole transactions, routed by relation to each table's own commit.
  Each table's commit records the last commit position it holds; the slot is advanced only when every table has
  committed. A replay skips, per table, what that table already has. While one table fails, the slot stays where it
  is (the others carry on and skip their replays), so the WAL the server keeps (``retained_bytes``) grows until the
  table is re-synced, disabled or fixed.
* **Schema changes.** A column added is added; one renamed (same attribute number) is renamed; one dropped stays
  in the lakehouse table, null from then on. ``int`` to ``bigint`` and ``real`` to ``double precision`` widen in
  place. Any other type change, a column added with a default, and a column added to a table with excluded columns
  rebuild the table (it is copied again), which the audit log records.
* **History mode** (per table) keeps every version: ``_cairn_valid_from`` (the commit time of the change; for copied
  rows, the source's time at the copy), ``_cairn_valid_to`` (null while current) and ``_cairn_current``.
* **Soft deletes** (per table) keep a deleted row with ``_cairn_deleted`` and ``_cairn_deleted_at`` set.
* **Hashed columns** hold HMAC-SHA-256 (hex) of the value's text under ``CAIRN_MIRROR_HASH_KEY``, the copy and the
  changes alike. The key is never stored with the mirror; without it a table with hashed columns is not mirrored.

The lakehouse tables are governed as any other: their readers need Cairn's read access; the mirror writes as Cairn.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import types
import uuid
import warnings
from functools import reduce
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, String, Table, Text, UniqueConstraint, delete, insert, select, update

from cairn.common.db import metadata
from cairn.pipelines.mirror import BOOL, DATE, IDENT, MAX_CHANGES, NAME, TS, TSTZ, Decoder, MirrorError, lsn_int, typed
from cairn.pipelines.streams import PROP

logger = logging.getLogger(__name__)

database_mirrors = Table(
    "cairn_database_mirrors", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(64), nullable=False, unique=True),
    Column("connection", String(128), nullable=False),
    Column("schema", String(128), nullable=False),
    Column("target_namespace", String(128), nullable=False),
    Column("include", Text, nullable=False),                 # JSON list of globs
    Column("exclude", Text, nullable=False),                 # JSON list of globs
    Column("defaults", Text, nullable=False),                # JSON {history, soft_delete}
    Column("tables", Text, nullable=False),                  # JSON {table: {history, soft_delete, columns, enabled}}
    Column("enabled", Boolean, nullable=False),
    Column("status", String(16), nullable=False),            # starting | running | degraded | paused | failed
    Column("error", Text, nullable=True),
    Column("last_lsn", String(64), nullable=True),
    Column("last_commit_ms", BigInteger, nullable=True),
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)

database_mirror_tables = Table(
    "cairn_database_mirror_tables", metadata,
    Column("id", String(32), primary_key=True),
    Column("mirror_id", String(32), nullable=False, index=True),
    Column("source_oid", BigInteger, nullable=False),        # pg_class.oid: a rename keeps it
    Column("source_name", String(128), nullable=False),
    Column("target", String(256), nullable=False),           # namespace.table
    Column("key", String(1024), nullable=False),             # the primary key's columns
    Column("columns", Text, nullable=False),                 # JSON {attnum: {name, oid, flag}}
    Column("renames", Text, nullable=False),                 # JSON {old column: new}, while old changes may arrive
    Column("gone", Text, nullable=False),                    # JSON {dropped column: flag}: never stored unhashed
    Column("applied", Text, nullable=True),                  # JSON: the options the table was copied with
    Column("status", String(16), nullable=False),  # copying | running | failed | resyncing | source_dropped | skipped
    Column("reason", Text, nullable=True),
    Column("error", Text, nullable=True),
    Column("rows", BigInteger, nullable=False),
    Column("snapshot_done", Boolean, nullable=False),
    Column("copy_lsn", String(64), nullable=True),           # transactions that committed before it are in the copy
    Column("updated_ms", BigInteger, nullable=False),
    UniqueConstraint("mirror_id", "source_oid", name="uq_cairn_database_mirror_tables_oid"),
)

TARGET_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
MAX_HELD_CHANGES = 200_000          # read this far while a failing table holds the slot back, so the others keep up
COPY_BATCH = 5000
FROM, TO, CURRENT = "_cairn_valid_from", "_cairn_valid_to", "_cairn_current"
DELETED, DELETED_AT = "_cairn_deleted", "_cairn_deleted_at"
OWN = (FROM, TO, CURRENT, DELETED, DELETED_AT)
ACTIVE = ("copying", "resyncing", "running", "failed")

# PostgreSQL type oid -> the lakehouse type (everything else is text).
KINDS = {21: "int", 23: "int", 20: "long", 26: "long", 700: "float", 701: "double", 1700: "double", BOOL: "boolean",
         DATE: "date", TS: "timestamp", TSTZ: "timestamptz"}
WIDENS = {("int", "long"), ("float", "double")}


def kind_of(oid: int, flag: Optional[str] = None) -> str:
    return "string" if flag == "hash" else KINDS.get(int(oid), "string")


def _arrow(kind: str):
    import pyarrow as pa

    return {"int": pa.int32(), "long": pa.int64(), "float": pa.float32(), "double": pa.float64(),
            "boolean": pa.bool_(), "date": pa.date32(), "timestamp": pa.timestamp("us"),
            "timestamptz": pa.timestamp("us", tz="UTC"), "string": pa.string()}[kind]


def _iceberg(kind: str):
    from pyiceberg import types as t

    return {"int": t.IntegerType(), "long": t.LongType(), "float": t.FloatType(), "double": t.DoubleType(),
            "boolean": t.BooleanType(), "date": t.DateType(), "timestamp": t.TimestampType(),
            "timestamptz": t.TimestamptzType(), "string": t.StringType()}[kind]


def value(oid: int, text: Optional[str]) -> Any:
    """A value as PostgreSQL wrote it, typed for the lakehouse. What the type cannot hold there (an ``infinity``
    date or timestamp) is null."""
    v = typed(int(oid), text)
    if isinstance(v, str) and KINDS.get(int(oid), "string") != "string":
        return None
    return v


def digest(key: bytes, text: Optional[str]) -> Optional[str]:
    """A hashed column's value: HMAC-SHA-256 of the value's text, in hex (null stays null)."""
    return None if text is None else hmac.new(key, text.encode(), hashlib.sha256).hexdigest()


def hash_key_from_env() -> Optional[bytes]:
    """``CAIRN_MIRROR_HASH_KEY`` (32 bytes, base64; ``_FILE`` works), read as the tokenisation key is."""
    from cairn.governance.tokens import _master

    return _master("CAIRN_MIRROR_HASH_KEY")


class Unplaced(Exception):
    """A change carries a column the table's known shape has no place for."""


def _lit(v: Any) -> Any:
    return v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v


def key_in(cols: list[str], keys: list[tuple]):
    """Rows whose key is one of ``keys`` (a balanced tree for keys of several columns: no deep nesting)."""
    from pyiceberg.expressions import And, EqualTo, In, Or

    if len(cols) == 1:
        return In(cols[0], {_lit(k[0]) for k in keys})
    terms = [reduce(And, (EqualTo(c, _lit(v)) for c, v in zip(cols, k))) for k in keys]
    while len(terms) > 1:
        terms = [Or(terms[i], terms[i + 1]) if i + 1 < len(terms) else terms[i] for i in range(0, len(terms), 2)]
    return terms[0]


class DatabaseMirrors:
    """Mirrors of whole PostgreSQL schemas into the lakehouse."""

    def __init__(self, engine, streams, source_url: Callable[[str], str], audit=None,
                 hash_key: Callable[[], Optional[bytes]] = hash_key_from_env,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.engine, self.streams, self.source_url, self.audit, self.clock = engine, streams, source_url, audit, clock
        self.hash_key = hash_key
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    # ---- small things ------------------------------------------------------------------------------------------
    @staticmethod
    def _names(mid: str) -> tuple[str, str]:
        return f"cairn_dbmirror_{mid}", f"cairn_dbmirror_{mid}"     # slot, publication

    def _connect(self, connection: str):
        import psycopg

        url = self.source_url(connection)
        if not url:
            raise MirrorError(f"no connection {connection!r}", 404)
        url = re.sub(r"^postgresql\+\w+://", "postgresql://", url)
        if not url.startswith(("postgresql://", "postgres://")):
            raise MirrorError(f"{connection} is not a PostgreSQL connection: a database mirror reads PostgreSQL "
                              "(other databases are mirrored one table at a time, /v1/mirrors)")
        return psycopg.connect(url, autocommit=True, connect_timeout=10)

    def _audit(self, action: str, actor: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("data", f"database_mirror.{action}", actor, f"urn:li:databaseMirror:{name}", details)

    def _row(self, name: str):
        with self.engine.connect() as c:
            row = c.execute(select(database_mirrors).where(database_mirrors.c.name == name)).first()
        if row is None:
            raise MirrorError("no such database mirror", 404)
        return row

    def _tables(self, mid: str) -> list:
        with self.engine.connect() as c:
            return c.execute(select(database_mirror_tables).where(database_mirror_tables.c.mirror_id == mid)
                             .order_by(database_mirror_tables.c.source_name)).all()

    def _set(self, tid: str, **values) -> None:
        for k in ("columns", "renames", "gone", "applied"):
            if k in values and not isinstance(values[k], (str, type(None))):
                values[k] = json.dumps(values[k], sort_keys=True)
        with self.engine.begin() as c:
            c.execute(update(database_mirror_tables).where(database_mirror_tables.c.id == tid)
                      .values(updated_ms=self.clock(), **values))

    def _mirror_set(self, mid: str, **values) -> None:
        with self.engine.begin() as c:
            c.execute(update(database_mirrors).where(database_mirrors.c.id == mid).values(**values))

    # ---- definitions -------------------------------------------------------------------------------------------
    @staticmethod
    def _options(body: dict) -> dict:
        """The changeable part of a definition, checked: include, exclude, defaults, tables."""
        def globs(v, default):
            v = default if v is None else v
            if not isinstance(v, list) or not all(isinstance(g, str) and 0 < len(g) <= 128 for g in v) or len(v) > 200:
                raise MirrorError("include and exclude are lists of table name patterns (* and ?)")
            return v

        def flags(d, what):
            if not isinstance(d, dict):
                raise MirrorError(f"{what} is an object")
            for k in ("history", "soft_delete", "enabled"):
                if k in d and not isinstance(d[k], bool):
                    raise MirrorError(f"{what}.{k} is true or false")
            if d.get("history") and d.get("soft_delete"):
                raise MirrorError(f"{what}: history mode keeps deleted rows' versions already; choose history or "
                                  "soft_delete")
            return d

        defaults = flags(body.get("defaults") or {}, "defaults")
        if set(defaults) - {"history", "soft_delete"}:
            raise MirrorError("defaults takes history and soft_delete")
        tables = body.get("tables") or {}
        if not isinstance(tables, dict):
            raise MirrorError("tables is an object of table name to its options")
        for name, o in tables.items():
            if not IDENT.match(str(name)):
                raise MirrorError(f"tables: {name!r} is not a table name")
            flags(o, f"tables.{name}")
            if set(o) - {"history", "soft_delete", "enabled", "columns"}:
                raise MirrorError(f"tables.{name} takes history, soft_delete, enabled and columns")
            cols = o.get("columns") or {}
            if not isinstance(cols, dict) or set(cols) - {"exclude", "hash"}:
                raise MirrorError(f"tables.{name}.columns takes exclude and hash")
            for k in ("exclude", "hash"):
                v = cols.get(k) or []
                if not isinstance(v, list) or not all(isinstance(c, str) and IDENT.match(c) for c in v):
                    raise MirrorError(f"tables.{name}.columns.{k} is a list of column names")
                if any(c in OWN for c in v):
                    raise MirrorError(f"tables.{name}.columns.{k}: {', '.join(OWN)} are Cairn's own columns")
            both = set(cols.get("exclude") or []) & set(cols.get("hash") or [])
            if both:
                raise MirrorError(f"tables.{name}: {', '.join(sorted(both))} cannot be both excluded and hashed")
        return {"include": globs(body.get("include"), ["*"]), "exclude": globs(body.get("exclude"), []),
                "defaults": defaults, "tables": tables}

    @staticmethod
    def _opts(defn: dict, source_name: str, target: str) -> dict:
        """One table's options. After a rename the options under its first name (its lakehouse table's) still apply
        until options are given under the new one."""
        tables, defaults = defn["tables"], defn["defaults"]
        o = tables.get(source_name)
        if o is None:
            o = tables.get(target.split(".")[1], {})
        cols = o.get("columns") or {}
        history = bool(o.get("history", False if o.get("soft_delete") else defaults.get("history", False)))
        soft = False if history else bool(o.get("soft_delete", defaults.get("soft_delete", False)))
        return {"history": history, "soft_delete": soft, "enabled": bool(o.get("enabled", True)),
                "exclude": sorted(cols.get("exclude") or []), "hash": sorted(cols.get("hash") or [])}

    @staticmethod
    def _defn(r) -> dict:
        return {"include": json.loads(r.include), "exclude": json.loads(r.exclude), "defaults": json.loads(r.defaults),
                "tables": json.loads(r.tables)}

    def _selected(self, defn: dict, table: str) -> bool:
        return (any(fnmatch.fnmatchcase(table, g) for g in defn["include"])
                and not any(fnmatch.fnmatchcase(table, g) for g in defn["exclude"]))

    @staticmethod
    def _server(conn) -> int:
        return int(conn.execute("SHOW server_version_num").fetchone()[0])

    def _check_options(self, conn, schema: str, defn: dict, mid: Optional[str] = None) -> None:
        """What the options ask of tables that exist now: refused here rather than found skipped later."""
        tables, columns = self._discover(conn, schema)
        by_name = {t["name"]: t for t in tables.values()}
        version = self._server(conn)
        # A column that was renamed at the source may still be named in the options as it was called.
        former = {t.source_oid: {c["as"] for c in json.loads(t.columns).values() if c.get("as")}
                  for t in (self._tables(mid) if mid else [])}
        for name in defn["tables"]:
            t = by_name.get(name)
            o = self._opts(defn, name, f"x.{name.lower()}")
            if t is not None:
                have = {c["name"] for c in columns.get(t["oid"], {}).values()} | former.get(t["oid"], set())
                missing = [c for c in o["exclude"] + o["hash"] if c not in have]
                if missing:
                    raise MirrorError(f"{schema}.{name} has no column {', '.join(missing)}")
            problem = self._refusal(o, t["key"] if t is not None else [], version)
            if problem:
                raise MirrorError(f"{schema}.{name}: {problem}", 409)

    def _refusal(self, o: dict, key: list[str], version: int) -> Optional[str]:
        """Why a table cannot be mirrored with these options (None: it can)."""
        if o["exclude"] and version < 150000:
            return ("columns can be excluded at the source from PostgreSQL 15 (a publication's column list); this "
                    "server is older, and Cairn does not copy a column out to drop it afterwards")
        in_key = [c for c in o["exclude"] if c in key]
        if in_key:
            return (f"{', '.join(in_key)} is part of the primary key and cannot be excluded: updates and deletes "
                    "would fail at the source")
        in_key = [c for c in o["hash"] if c in key]
        if in_key:
            return f"{', '.join(in_key)} is part of the primary key and cannot be hashed: changes are applied by key"
        if o["hash"] and self.hash_key() is None:
            return ("columns are to be hashed and there is no hashing key: set CAIRN_MIRROR_HASH_KEY (32 random "
                    "bytes, base64)")
        return None

    def create(self, actor: str, body: dict) -> dict:
        from psycopg import sql

        name, connection = str(body.get("name") or ""), str(body.get("connection") or "")
        schema, ns = str(body.get("schema") or ""), str(body.get("target_namespace") or "")
        if not NAME.match(name):
            raise MirrorError("name: lowercase letters, digits and '_'")
        if not IDENT.match(schema):
            raise MirrorError("schema is the source schema's name")
        if not TARGET_NAME.match(ns):
            raise MirrorError("target_namespace is a lakehouse namespace (lowercase letters, digits and '_')")
        defn = self._options(body)
        with self.engine.connect() as c:
            if c.execute(select(database_mirrors.c.id).where(database_mirrors.c.name == name)).first():
                raise MirrorError(f"{name} exists", 409)
        mid = uuid.uuid4().hex[:16]
        slot, pub = self._names(mid)
        with self._connect(connection) as conn:
            if conn.execute("SHOW wal_level").fetchone()[0] != "logical":
                raise MirrorError("the source runs without wal_level = logical", 409)
            if conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone() is None:
                raise MirrorError(f"the source has no schema {schema}", 404)
            self._check_options(conn, schema, defn)
            conn.execute(sql.SQL("CREATE PUBLICATION {}").format(sql.Identifier(pub)))   # tables are added as found
            try:
                conn.execute("SELECT pg_create_logical_replication_slot(%s, 'pgoutput')", (slot,))
            except Exception:
                conn.execute(sql.SQL("DROP PUBLICATION IF EXISTS {}").format(sql.Identifier(pub)))
                raise
        with self.engine.begin() as c:
            c.execute(insert(database_mirrors).values(
                id=mid, name=name, connection=connection, schema=schema, target_namespace=ns,
                include=json.dumps(defn["include"]), exclude=json.dumps(defn["exclude"]),
                defaults=json.dumps(defn["defaults"]), tables=json.dumps(defn["tables"]), enabled=True,
                status="starting", error=None, last_lsn=None, last_commit_ms=None, created_by=actor,
                created_ms=self.clock()))
        self._audit("created", actor, name, {"connection": connection, "schema": schema, "target_namespace": ns,
                                             **defn})
        self.run(name)
        return self.get(name)

    def update(self, actor: str, name: str, body: dict) -> dict:
        """Changes what is mirrored and how: include, exclude, defaults and tables (each as given, or kept). A table
        whose options change is copied again; one no longer selected is taken out of the publication."""
        r = self._row(name)
        for k in ("connection", "schema", "target_namespace"):
            if k in body and body[k] != getattr(r, k):
                raise MirrorError(f"{k} cannot be changed: delete the mirror and create it again", 409)
        defn = self._options({**self._defn(r), **{k: body[k] for k in ("include", "exclude", "defaults", "tables")
                                                  if body.get(k) is not None}})
        with self._connect(r.connection) as conn:
            self._check_options(conn, r.schema, defn, r.id)
        self._mirror_set(r.id, include=json.dumps(defn["include"]), exclude=json.dumps(defn["exclude"]),
                         defaults=json.dumps(defn["defaults"]), tables=json.dumps(defn["tables"]))
        self._audit("updated", actor, name, defn)
        self.run(name)
        return self.get(name)

    def _public(self, r) -> dict:
        return {"name": r.name, "connection": r.connection, "schema": r.schema,
                "target_namespace": r.target_namespace, **self._defn(r), "enabled": r.enabled, "status": r.status,
                "error": r.error, "last_lsn": r.last_lsn, "last_commit_ms": r.last_commit_ms,
                "created_by": r.created_by, "created_ms": r.created_ms}

    def list(self) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(database_mirrors).order_by(database_mirrors.c.name)).all()
        return [self._public(r) for r in rows]

    def get(self, name: str) -> dict:
        r = self._row(name)
        out, defn = self._public(r), self._defn(r)
        out["mirrored"] = []
        for t in self._tables(r.id):
            cols = json.loads(t.columns).values()
            o = json.loads(t.applied) if t.applied else self._opts(defn, t.source_name, t.target)
            out["mirrored"].append({
                "table": t.source_name, "target": t.target, "key": [k for k in t.key.split(",") if k],
                "status": t.status, "reason": t.reason, "error": t.error, "rows": t.rows,
                "history": o["history"], "soft_delete": o["soft_delete"],
                "excluded": sorted(c["name"] for c in cols if c.get("flag") == "exclude"),
                "hashed": sorted(c["name"] for c in cols if c.get("flag") == "hash"),
                "copy_lsn": t.copy_lsn, "updated_ms": t.updated_ms})
        out["rows"] = sum(t["rows"] for t in out["mirrored"])
        out["holding"] = [t["table"] for t in out["mirrored"] if t["status"] == "failed"]
        out["retained_bytes"] = None
        try:
            with self._connect(r.connection) as conn:
                got = conn.execute("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn)::bigint "
                                   "FROM pg_replication_slots WHERE slot_name = %s", (self._names(r.id)[0],)).fetchone()
                out["retained_bytes"] = int(got[0]) if got and got[0] is not None else None
        except Exception:  # noqa: BLE001 - the source is away: shown without it
            pass
        return out

    def set_enabled(self, actor: str, name: str, enabled: bool) -> dict:
        r = self._row(name)
        self._mirror_set(r.id, enabled=enabled, status="running" if enabled else "paused")
        self._audit("resumed" if enabled else "paused", actor, name, {})
        return self.get(name)

    def delete(self, actor: str, name: str) -> None:
        from psycopg import sql

        r = self._row(name)
        slot, pub = self._names(r.id)
        with self._connect(r.connection) as conn:
            conn.execute("SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name = %s",
                         (slot,))
            conn.execute(sql.SQL("DROP PUBLICATION IF EXISTS {}").format(sql.Identifier(pub)))
        with self.engine.begin() as c:
            c.execute(delete(database_mirror_tables).where(database_mirror_tables.c.mirror_id == r.id))
            c.execute(delete(database_mirrors).where(database_mirrors.c.id == r.id))
        self._audit("deleted", actor, name, {})

    def resync(self, actor: str, name: str, table: str) -> dict:
        """Copies one table again. The other tables are untouched."""
        r = self._row(name)
        t = next((t for t in self._tables(r.id) if t.source_name == table), None)
        if t is None:
            raise MirrorError(f"{name} mirrors no table {table}", 404)
        if t.status in ("source_dropped", "skipped"):
            raise MirrorError(f"{table} is {t.status} ({t.reason}): there is nothing to copy", 409)
        self._set(t.id, status="resyncing", snapshot_done=False, error=None, reason="re-sync asked for")
        self._audit("table_resync", actor, name, {"table": table, "target": t.target})
        self.run(name)
        return self.get(name)

    # ---- the source's catalog ----------------------------------------------------------------------------------
    @staticmethod
    def _discover(conn, schema: str) -> tuple[dict, dict]:
        """The schema's ordinary tables by oid (``{oid, name, key}``) and their columns
        (``{oid: {attnum: {name, oid, default}}}``; generated columns are not replicated and are left out)."""
        tables = {int(r[0]): {"oid": int(r[0]), "name": r[1], "key": list(r[2])} for r in conn.execute(
            "SELECT c.oid::bigint, c.relname, ARRAY(SELECT a.attname::text FROM pg_index i JOIN pg_attribute a "
            "ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) WHERE i.indrelid = c.oid AND i.indisprimary "
            "ORDER BY array_position(i.indkey, a.attnum)) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind = 'r' AND c.relpersistence = 'p'", (schema,)).fetchall()}
        columns: dict[int, dict] = {oid: {} for oid in tables}
        for rel, num, name, typ, default in conn.execute(
                "SELECT a.attrelid::bigint, a.attnum, a.attname::text, "
                "CASE WHEN t.typtype = 'd' THEN t.typbasetype ELSE a.atttypid END::bigint, "
                "a.atthasdef OR a.atthasmissing "
                "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_type t ON t.oid = a.atttypid "
                "WHERE n.nspname = %s AND c.relkind = 'r' AND c.relpersistence = 'p' AND a.attnum > 0 "
                "AND NOT a.attisdropped AND a.attgenerated = '' ORDER BY a.attrelid, a.attnum", (schema,)).fetchall():
            if int(rel) in columns:
                columns[int(rel)][str(num)] = {"name": name, "oid": int(typ), "default": bool(default)}
        return tables, columns

    def _publish(self, conn, pub: str, schema: str, want: dict[str, Optional[list[str]]], all_columns: dict) -> None:
        """Makes the publication the tables in ``want`` (name to its column list, None for every column)."""
        from psycopg import sql

        lists = self._server(conn) >= 150000
        have = {r[0]: (set(r[1]) if lists else None) for r in conn.execute(
            f"SELECT tablename::text, {'attnames::text[]' if lists else 'NULL'} FROM pg_publication_tables "
            "WHERE pubname = %s AND schemaname = %s", (pub, schema)).fetchall()}
        wanted = {n: (set(cols if cols is not None else all_columns[n]) if lists else None) for n, cols in want.items()}
        if have == wanted:
            return
        if not want:
            for n in have:
                conn.execute(sql.SQL("ALTER PUBLICATION {} DROP TABLE ONLY {}").format(
                    sql.Identifier(pub), sql.Identifier(schema, n)))
            return
        parts = []
        for n, cols in sorted(want.items()):
            part = sql.SQL("ONLY {}").format(sql.Identifier(schema, n))
            if cols is not None:
                part += sql.SQL(" ({})").format(sql.SQL(", ").join(sql.Identifier(c) for c in cols))
            parts.append(part)
        conn.execute(sql.SQL("ALTER PUBLICATION {} SET TABLE {}").format(sql.Identifier(pub), sql.SQL(", ").join(parts)))

    # ---- the lakehouse table -----------------------------------------------------------------------------------
    def _load(self, target: str):
        from pyiceberg.exceptions import NoSuchTableError

        try:
            return self.streams._catalog().load_table(tuple(target.split(".")))
        except NoSuchTableError:
            return None

    @staticmethod
    def _own_columns(o: dict) -> list[tuple[str, str]]:
        if o["history"]:
            return [(FROM, "timestamptz"), (TO, "timestamptz"), (CURRENT, "boolean")]
        return [(DELETED, "boolean"), (DELETED_AT, "timestamptz")] if o["soft_delete"] else []

    def _conform(self, t, columns: dict, o: dict):
        """The lakehouse table with the shape a copy needs: the source's columns that are not excluded (hashed ones
        as text), then the mode's own. Columns the source dropped earlier stay. Returns the table and whether its
        rows can be kept (history and soft-delete tables are reconciled with the source, not emptied)."""
        import pyarrow as pa
        from pyiceberg.expressions import AlwaysTrue

        from cairn.pipelines.pipes import open_table

        want = [(c["name"], kind_of(c["oid"], c.get("flag"))) for _, c in sorted(columns.items(), key=lambda x: int(x[0]))
                if c.get("flag") != "exclude"] + self._own_columns(o)
        table = self._load(t.target)
        if table is None:
            schema = pa.schema([pa.field(n, _arrow(k)) for n, k in want])
            return open_table(self.streams._catalog(), t.target, schema, True), False
        have = {f.name: f.field_type for f in table.schema().fields}
        own = dict(self._own_columns(o))
        keep = bool(own) and all(n in have for n in own)
        if not keep:
            with warnings.catch_warnings():                  # an empty table: nothing to delete, and that is fine
                warnings.filterwarnings("ignore", "Delete operation did not match any records")
                table.delete(AlwaysTrue())
        wanted = {n for n, _ in want}
        renamed = [(old, new) for old, new in json.loads(t.renames).items()
                   if old in have and new in wanted and new not in have and old not in wanted]
        if renamed:                                          # renamed at the source since the last copy
            with table.update_schema() as u:
                for old, new in renamed:
                    u.rename_column(old, new)
            table = self.streams._catalog().load_table(tuple(t.target.split(".")))
            have = {f.name: f.field_type for f in table.schema().fields}
        hidden = {c["name"] for c in columns.values() if c.get("flag") == "exclude"} | \
            {n for n, f in json.loads(t.gone).items() if f == "exclude"}
        drop = [n for n in have if (n in OWN and n not in own) or n in hidden]
        replace = []
        with table.update_schema(allow_incompatible_changes=True) as u:
            for n in drop:
                u.delete_column(n)
            for n, k in want:
                if n not in have:
                    u.add_column(n, _iceberg(k))
                elif have[n] != _iceberg(k):
                    if (self._kind_name(have[n]), k) in WIDENS:
                        u.update_column(n, field_type=_iceberg(k))
                    else:
                        u.delete_column(n)
                        replace.append((n, k))
        if replace:                                        # a column of another type takes the name: a second step
            with self.streams._catalog().load_table(tuple(t.target.split("."))).update_schema() as u:
                for n, k in replace:
                    u.add_column(n, _iceberg(k))
        return self.streams._catalog().load_table(tuple(t.target.split("."))), keep

    @staticmethod
    def _kind_name(field_type) -> str:
        return next((k for k in ("int", "long", "float", "double", "boolean", "date", "timestamp", "timestamptz",
                                 "string") if _iceberg(k) == field_type), "other")

    def _stream(self, t) -> dict:
        return {"id": f"dbmirror_{t.id}", "topic": f"pg.{t.id}", "target": t.target}

    def _floor(self, t, table) -> int:
        """Transactions that committed before this position are in the table already."""
        s = self._stream(t)
        done = self.streams.table_offsets(table, s["id"], s["topic"]).get(0, 0) if table is not None else 0
        return max(done, lsn_int(t.copy_lsn) if t.copy_lsn else 0)

    @staticmethod
    def _lookup(table, key: list[str], keys: list[tuple], history: bool, everything: bool = False) -> dict:
        """The table's rows for these keys (in history mode, their current versions), by key."""
        from pyiceberg.expressions import AlwaysTrue, And, EqualTo, In

        if not keys and not everything:
            return {}
        flt = AlwaysTrue() if everything else In(key[0], {_lit(k[0]) for k in keys})
        if history:
            flt = And(flt, EqualTo(CURRENT, True))
        wanted = None if everything else set(keys)
        out = {}
        for row in table.scan(row_filter=flt).to_arrow().to_pylist():
            k = tuple(row[c] for c in key)
            if wanted is None or k in wanted:
                out[k] = row
        return out

    def _write(self, t, o: dict, ops: list[tuple], topic: Optional[str] = None) -> int:
        """Applies ``(position, time, kind, row, unchanged)`` in order as one commit that records the next position.
        kind: upsert, delete (the row holds the key) or truncate. Returns the changes applied."""
        import pyarrow as pa
        from pyiceberg.exceptions import CommitFailedException
        from pyiceberg.expressions import AlwaysTrue, And, EqualTo

        from cairn.pipelines.pipes import fit

        if not ops:
            return 0
        key = t.key.split(",")
        history, soft = o["history"], o["soft_delete"]
        cat = self.streams._catalog()
        ident = tuple(t.target.split("."))
        table = cat.load_table(ident)
        want = table.schema().as_arrow()
        data_schema = pa.schema([f for f in want if f.name not in OWN])
        # Every upsert as the table would hold it (types, and null for a column the change did not carry), so a
        # row can be compared with the version the table has.
        at = [i for i, op in enumerate(ops) if op[2] == "upsert"]
        unknown = sorted({c for i in at for c in ops[i][3]} - set(data_schema.names))
        if unknown:
            raise MirrorError(f"{t.target} has no column {', '.join(unknown)}", 502)
        normal = pa.Table.from_pylist([ops[i][3] for i in at], schema=data_schema).to_pylist() if at else []
        rows = dict(zip(at, normal))

        def key_of(row: dict) -> tuple:
            k = tuple(row.get(c) for c in key)
            if any(v is None for v in k):
                raise MirrorError(f"a change to {t.source_name} has no value for its key {', '.join(key)}", 502)
            return k

        delete_keys = pa.Table.from_pylist([op[3] for op in ops if op[2] == "delete"],
                                           schema=pa.schema([want.field(c) for c in key])).to_pylist()
        delete_keys = iter(delete_keys)
        steps = []                                           # (time, kind, key, row, unchanged)
        for i, (_, when, kind, _, unchanged) in enumerate(ops):
            if kind == "upsert":
                steps.append((when, kind, key_of(rows[i]), rows[i], unchanged))
            elif kind == "delete":
                steps.append((when, kind, key_of(next(delete_keys)), None, None))
            else:
                steps.append((when, kind, None, None, None))
        if history:
            need = [s[2] for s in steps if s[2] is not None]
        elif soft:
            need = [s[2] for s in steps if s[1] == "delete" or s[4]]
        else:
            need = [s[2] for s in steps if s[4]]
        cur = self._lookup(table, key, need, history)
        local: dict[tuple, Optional[dict]] = {}
        closed: list[dict] = []                              # history: versions that ended in this batch
        replaced: set[tuple] = set()                         # history: keys whose current version in the table ended
        emptied = False
        applied = 0

        def state(k):
            return local[k] if k in local else cur.get(k)

        def close(k, prev, when):
            """Ends a version; returns the time the next one starts (never before the one it follows)."""
            start = prev[FROM]
            end = when if start is None or when > start else start
            if k not in local:
                replaced.add(k)
            if start is None or end > start:                 # a version of no length is not one
                closed.append({**prev, TO: end, CURRENT: False})
            return end

        def remove(k, when) -> bool:
            prev = state(k)
            if history:
                if prev is None:
                    return False
                close(k, prev, when)
                local[k] = None
            elif soft:
                if prev is None or prev.get(DELETED):
                    return False
                local[k] = {**prev, DELETED: True, DELETED_AT: when}
            else:
                local[k] = None
            return True

        for when, kind, k, row, unchanged in steps:
            if kind == "truncate":
                if history or soft:
                    cur = {**self._lookup(table, key, [], history, everything=True), **cur}
                    applied += sum(remove(x, when) for x in set(cur) | set(local))
                else:
                    emptied, cur = True, {}
                    local.clear()
                    applied += 1
                continue
            if kind == "delete":
                applied += remove(k, when)
                continue
            prev = state(k)
            for c in unchanged or ():                        # a large value the update did not touch: as it was
                row[c] = prev.get(c) if prev else None
            if history:
                if prev is not None and all(prev.get(c) == row.get(c) for c in data_schema.names):
                    continue                                 # nothing mirrored changed: no new version
                start = close(k, prev, when) if prev is not None else when
                local[k] = {**row, FROM: start, TO: None, CURRENT: True}
            elif soft:
                local[k] = {**row, DELETED: False, DELETED_AT: None}
            else:
                local[k] = row
            applied += 1

        s = self._stream(t)
        props = {f"{PROP}{s['id']}.{topic or s['topic']}.0": str(max(op[0] for op in ops) + 1)}
        if history:
            gone = list(replaced)
            flt = And(key_in(key, gone), EqualTo(CURRENT, True)) if gone else None
            out = closed + [v for v in local.values() if v is not None]
        else:
            flt = AlwaysTrue() if emptied else (key_in(key, list(local)) if local else None)
            out = [v for v in local.values() if v is not None]
        if flt is None and not out:
            return 0
        data = fit(table, pa.Table.from_pylist(out, schema=want), t.target) if out else None
        for attempt in range(5):
            try:
                with table.transaction() as tx:
                    if flt is not None:
                        with warnings.catch_warnings():       # new keys match nothing: that is normal here
                            warnings.filterwarnings("ignore", "Delete operation did not match any records")
                            tx.delete(flt, snapshot_properties=props)
                    if data is not None:
                        tx.append(data, snapshot_properties=props)
                break
            except CommitFailedException:                    # another writer committed first
                if history:
                    # The versions were worked out from the table as it was: writing them over another run's
                    # would double them. The next tick starts from what the table holds now.
                    raise MirrorError(f"{t.target} was written by another run meanwhile; tried again next tick", 409)
                time.sleep(0.2 * (attempt + 1))              # upserts by key: the same again is the same
                table = cat.load_table(ident)
        else:
            raise MirrorError(f"{t.target}: the commit kept conflicting", 502)
        if self.streams.changes is not None:
            self.streams.changes.record([self.streams.urn(*ident)], "mirror", f"database-mirror:{t.target}")
        return applied

    # ---- the copy ----------------------------------------------------------------------------------------------
    def _copy(self, r, t, columns: dict, o: dict) -> None:
        """Copies a table from one snapshot of the source, and records the log position that snapshot is exact at.

        The table's SHARE lock is held only while the snapshot is taken: every transaction that wrote to the table
        has ended by then, so those that committed before the position are in the snapshot and later ones are not."""
        from psycopg import sql

        _, keep = self._conform(t, columns, o)
        key_bytes = self.hash_key() if o["hash"] or any(c.get("flag") == "hash" for c in columns.values()) else None
        cols = [c for _, c in sorted(columns.items(), key=lambda x: int(x[0])) if c.get("flag") != "exclude"]
        if any(c.get("flag") == "hash" for c in cols) and key_bytes is None:
            raise MirrorError("columns are to be hashed and there is no hashing key (CAIRN_MIRROR_HASH_KEY)", 503)
        source = sql.Identifier(r.schema, t.source_name)
        key = t.key.split(",")
        seen: Optional[set] = set() if keep else None
        copied = 0
        with self._connect(r.connection) as conn:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
            try:
                conn.execute("SET LOCAL lock_timeout = '15s'")
                conn.execute("SAVEPOINT cairn_copy")
                conn.execute(sql.SQL("LOCK TABLE ONLY {} IN SHARE MODE").format(source))
                lsn, when = conn.execute("SELECT pg_current_wal_insert_lsn()::text, clock_timestamp()").fetchone()
                conn.execute("ROLLBACK TO SAVEPOINT cairn_copy")       # the lock goes; the snapshot stays
                when = when.astimezone(dt.timezone.utc)
                cur = conn.cursor(name=f"cairn_dbmirror_copy_{t.id}")   # server-side: rows in batches
                cur.execute(sql.SQL("SELECT {} FROM ONLY {}").format(
                    sql.SQL(", ").join(sql.SQL("{}::text").format(sql.Identifier(c["name"])) for c in cols), source))
                offset = 0
                while True:
                    batch = cur.fetchmany(COPY_BATCH)
                    if not batch:
                        break
                    ops = []
                    for i, values in enumerate(batch):
                        # A value's text is what replication sends for it too (only booleans differ: t and f).
                        values = [v[0] if c["oid"] == BOOL and v else v for c, v in zip(cols, values)]
                        row = {c["name"]: digest(key_bytes, v) if c.get("flag") == "hash" else value(c["oid"], v)
                               for c, v in zip(cols, values)}
                        ops.append((offset + i, when, "upsert", row, None))
                    copied += self._write(t, o, ops, topic=f"pg.{t.id}.copy")
                    if seen is not None:
                        seen.update(tuple(op[3][k] for k in key) for op in ops)
                    offset += len(batch)
                cur.close()
            finally:
                conn.execute("ROLLBACK")
        if seen is not None:                                 # rows the table has that the source no longer does
            table = self._load(t.target)
            have = self._lookup(table, key, [], o["history"], everything=True)
            left = [k for k, row in have.items() if k not in seen and not row.get(DELETED)]
            for i in range(0, len(left), COPY_BATCH):
                ops = [(0, when, "delete", dict(zip(key, k)), None) for k in left[i:i + COPY_BATCH]]
                copied += self._write(t, o, ops, topic=f"pg.{t.id}.copy")
        self._set(t.id, snapshot_done=True, copy_lsn=lsn, status="running", error=None, applied=o, columns=columns,
                  rows=database_mirror_tables.c.rows + copied, renames={})

    # ---- one tick ----------------------------------------------------------------------------------------------
    def _reconcile(self, r, defn: dict, conn, actor: str = "urn:li:corpuser:cairn") -> tuple[dict, dict]:
        """Brings Cairn's record of the tables in step with the source's catalog: new, renamed and dropped tables,
        and each table's columns. Returns the publication's wanted tables (name to column list or None) and every
        column of each."""
        tables, columns = self._discover(conn, r.schema)
        version = self._server(conn)
        known = {t.source_oid: t for t in self._tables(r.id)}
        for t in known.values():                             # renames first: a table is known by its oid
            src = tables.get(t.source_oid)
            if src is not None and src["name"] != t.source_name:
                self._set(t.id, source_name=src["name"])
                self._audit("table_renamed", actor, r.name, {"from": t.source_name, "to": src["name"],
                                                             "target": t.target})
        targets = {t.target: t for t in known.values()}
        for oid, src in tables.items():
            if oid in known or not self._selected(defn, src["name"]):
                continue
            target = f"{r.target_namespace}.{src['name'].lower()}"
            status, reason = "copying", None
            holder = targets.get(target)
            if holder is not None and holder.source_oid not in tables:
                # Dropped and created again: the new table takes its lakehouse table over (and is copied into it).
                with self.engine.begin() as c:
                    c.execute(delete(database_mirror_tables).where(database_mirror_tables.c.id == holder.id))
                holder = None
            if not TARGET_NAME.match(src["name"].lower()):
                status, reason = "skipped", "its name cannot be a lakehouse table's (letters, digits and '_')"
            elif holder is not None:
                status, reason = "skipped", f"{target} is already the mirror of another table of this schema"
            tid = uuid.uuid4().hex[:16]
            with self.engine.begin() as c:
                c.execute(insert(database_mirror_tables).values(
                    id=tid, mirror_id=r.id, source_oid=oid, source_name=src["name"], target=target,
                    key=",".join(src["key"]), columns="{}", renames="{}", gone="{}", applied=None, status=status,
                    reason=reason, error=None, rows=0, snapshot_done=False, copy_lsn=None, updated_ms=self.clock()))
            if holder is None:
                targets[target] = types.SimpleNamespace(id=tid, source_oid=oid)
            self._audit("table_added", actor, r.name, {"table": src["name"], "target": target, "status": status})
        want: dict[str, Optional[list[str]]] = {}
        every: dict[str, list[str]] = {}
        for t in self._tables(r.id):
            src = tables.get(t.source_oid)
            if src is None:
                if t.status != "source_dropped":
                    self._set(t.id, status="source_dropped", reason="the table was dropped at the source", error=None)
                    self._audit("table_dropped", actor, r.name, {"table": t.source_name, "target": t.target})
                continue
            if t.status == "skipped" and (t.reason or "").startswith(("its name cannot", f"{t.target} is already")):
                continue
            o = self._opts(defn, src["name"], t.target)
            every[src["name"]] = [c["name"] for _, c in sorted(columns[t.source_oid].items(), key=lambda x: int(x[0]))]
            now = columns[t.source_oid]
            clash = sorted(c["name"] for c in now.values() if c["name"] in OWN)
            problem = (None if self._selected(defn, src["name"]) else "its name is no longer selected") \
                or (None if o["enabled"] else "disabled in the mirror's tables") \
                or (None if src["key"] else "it has no primary key: a mirror applies changes by key") \
                or (f"its columns {', '.join(clash)} have the names of Cairn's own" if clash else None) \
                or self._refusal(o, src["key"], version)
            if problem:
                if t.status != "skipped" or t.reason != problem:
                    self._set(t.id, status="skipped", reason=problem, error=None, snapshot_done=False)
                    self._audit("table_skipped", actor, r.name, {"table": src["name"], "reason": problem})
                continue
            prev = json.loads(t.columns)
            cols = self._flagged(now, prev, o)
            if not t.snapshot_done:                          # new, picked up again, or its copy is still due
                self._set(t.id, status="resyncing" if t.applied else "copying", error=None,
                          reason=None if t.status == "skipped" else t.reason, key=",".join(src["key"]), columns=cols)
                want[src["name"]] = self._column_list(cols)
                continue
            if json.loads(t.applied or "null") != o:
                rebuild = "its options changed"
            elif self._attnums(now, src["key"]) != self._attnums(prev, t.key.split(",")):
                rebuild = "its primary key changed"
            else:
                try:
                    rebuild = self._evolve(t, prev, cols)
                except Exception as e:  # noqa: BLE001 - this table's trouble: shown on it, the others go on
                    self._set(t.id, status="failed", error=f"{type(e).__name__}: {e}"[:2000])
                    want[src["name"]] = self._column_list(prev)
                    continue
            if rebuild:
                self._audit("table_rebuilt", actor, r.name, {"table": src["name"], "target": t.target,
                                                             "reason": rebuild})
                renamed = {prev[n]["name"]: c["name"] for n, c in cols.items()
                           if n in prev and prev[n]["name"] != c["name"]}
                self._set(t.id, status="resyncing", snapshot_done=False, reason=rebuild, error=None,
                          key=",".join(src["key"]), columns=cols, renames={**json.loads(t.renames), **renamed})
            elif cols != prev or ",".join(src["key"]) != t.key:
                self._set(t.id, columns=cols, key=",".join(src["key"]))
            want[src["name"]] = self._column_list(cols)
        return want, every

    @staticmethod
    def _attnums(now: dict, names: list[str]) -> list[str]:
        by_name = {c["name"]: n for n, c in now.items()}
        return [by_name.get(n, "") for n in names]

    @staticmethod
    def _column_list(cols: dict) -> Optional[list[str]]:
        if not any(c.get("flag") == "exclude" for c in cols.values()):
            return None
        return [c["name"] for _, c in sorted(cols.items(), key=lambda x: int(x[0])) if c.get("flag") != "exclude"]

    @staticmethod
    def _flagged(now: dict, prev: dict, o: dict) -> dict:
        """The source's columns with what happens to each: excluded, hashed or neither. A column keeps its flag
        through a rename (by attribute number) for as long as the options still name it as it was called, so a
        renamed column does not start leaving the source."""
        out = {}
        for num, c in now.items():
            names = [c["name"]] + ([prev[num]["as"]] if prev.get(num, {}).get("as") else []) + \
                ([prev[num]["name"]] if num in prev else [])
            flag, named = None, None
            for n in names:
                flag = "exclude" if n in o["exclude"] else "hash" if n in o["hash"] else None
                if flag:
                    named = n
                    break
            out[num] = {**c, "flag": flag, **({"as": named} if flag and named != c["name"] else {})}
        return out

    def _evolve(self, t, prev: dict, cols: dict) -> Optional[str]:
        """Applies the source's column changes to the lakehouse table in place. Returns why the table must be
        copied again instead, when it must."""
        added = [c for n, c in cols.items() if n not in prev and c.get("flag") != "exclude"]
        dropped = {prev[n]["name"]: prev[n].get("flag") for n in prev if n not in cols}
        renamed = [(prev[n]["name"], c["name"]) for n, c in cols.items()
                   if n in prev and prev[n]["name"] != c["name"] and c.get("flag") != "exclude"]
        retyped = [(c["name"], kind_of(prev[n]["oid"], prev[n].get("flag")), kind_of(c["oid"], c.get("flag")))
                   for n, c in cols.items() if n in prev and c.get("flag") != "exclude"]
        retyped = [x for x in retyped if x[1] != x[2]]
        if not (added or dropped or renamed or retyped):
            return None
        for name, was, now in retyped:
            if (was, now) not in WIDENS:
                return f"the type of {name} changed ({was} to {now})"
        if added and any(c.get("flag") == "exclude" for c in cols.values()):
            return (f"{', '.join(c['name'] for c in added)} was added to a table with excluded columns: it was not "
                    "published until now")
        with_default = [c["name"] for c in added if c.get("default")]
        if with_default:
            return f"{', '.join(with_default)} was added with a default: the rows already there have it too"
        table = self._load(t.target)
        if table is None:
            return "its lakehouse table is gone"
        have = {f.name: f.field_type for f in table.schema().fields}
        for old, new in renamed:
            if old not in have or new in have:
                return f"{old} was renamed to {new}, which the lakehouse table cannot follow in place"
        for c in added:
            if c["name"] in have and have[c["name"]] != _iceberg(kind_of(c["oid"], c.get("flag"))):
                return f"{c['name']} was added again with another type"
        if renamed or added or retyped:
            with table.update_schema() as u:
                for old, new in renamed:
                    u.rename_column(old, new)
                for c in added:
                    if c["name"] not in have:
                        u.add_column(c["name"], _iceberg(kind_of(c["oid"], c.get("flag"))))
                for name, _, now in retyped:
                    u.update_column(name, field_type=_iceberg(now))
        values: dict = {}
        if renamed:
            values["renames"] = {**json.loads(t.renames), **dict(renamed)}
        if dropped:
            values["gone"] = {**json.loads(t.gone), **dropped}
        if values:
            self._set(t.id, **values)
        return None

    def _shape(self, t, cols_by_name: dict, renames: dict, gone: dict, lake: set, rel: dict, raw: dict,
               unchanged: list[str], key_bytes: Optional[bytes]) -> tuple[dict, list[str]]:
        """A decoded row as the lakehouse table holds it: columns by their present names, typed, hashed."""
        names = {n for n, _ in rel["columns"]}
        out, kept = {}, []
        for name, oid in rel["columns"]:
            if name not in raw and name not in unchanged:
                continue
            target, c = name, cols_by_name.get(name)
            if c is None and renames.get(name) in cols_by_name and renames[name] not in names:
                target = renames[name]                       # decoded before the rename
                c = cols_by_name[target]
            if c is not None:
                flag, oid = c.get("flag"), c["oid"]
            elif name in gone:
                flag = gone[name]                            # dropped at the source since: still never unhashed
            elif name in lake:
                flag = None
            else:
                raise Unplaced(name)
            if flag == "exclude":
                continue
            if name in unchanged:
                kept.append(target)
            elif flag == "hash":
                if key_bytes is None:
                    raise MirrorError("columns are to be hashed and there is no hashing key (CAIRN_MIRROR_HASH_KEY)",
                                      503)
                out[target] = digest(key_bytes, raw[name])
            else:
                out[target] = value(oid, raw[name])
        return out, kept

    def _changes(self, t, o: dict, table, txns: list, key_bytes: Optional[bytes]) -> list[tuple]:
        """The table's changes from the transactions it does not have yet, as ``_write`` takes them."""
        floor = self._floor(t, table)
        cols_by_name = {c["name"]: c for c in json.loads(t.columns).values()}
        renames, gone = json.loads(t.renames), json.loads(t.gone)
        lake = {f.name for f in table.schema().fields}
        key = t.key.split(",")
        ops = []
        for lsn, when, changes in txns:
            if lsn < floor:
                continue                                     # in the table already: the copy, or a replay
            for oid, kind, rel, row, unchanged, old in changes:
                if oid != t.source_oid:
                    continue
                if kind == "truncate":
                    ops.append((lsn, when, "truncate", None, None))
                    continue
                shaped, kept = self._shape(t, cols_by_name, renames, gone, lake, rel, row, unchanged or [], key_bytes)
                if kind == "delete":
                    ops.append((lsn, when, "delete", {k: shaped.get(k) for k in key}, None))
                    continue
                if old is not None:
                    was, _ = self._shape(t, cols_by_name, renames, gone, lake, rel, old, [], key_bytes)
                    if any(was.get(k) != shaped.get(k) for k in key):     # the key changed: the old row goes
                        ops.append((lsn, when, "delete", {k: was.get(k) for k in key}, None))
                ops.append((lsn, when, "upsert", shaped, kept))
        return ops

    def run(self, name: str) -> int:
        """Brings one database mirror up to date: the source's tables and columns, copies that are due, then whole
        transactions from the slot, each table its own commit, and the slot advanced when every table has them."""
        with self._guard:
            lock = self._locks.setdefault(name, threading.Lock())
        with lock:                                           # one run of a mirror at a time in this process
            return self._run(name)

    def _run(self, name: str) -> int:
        r = self._row(name)
        if not r.enabled:
            return 0
        slot, pub = self._names(r.id)
        defn = self._defn(r)
        try:
            written, failed = 0, False
            with self._connect(r.connection) as conn:
                want, every = self._reconcile(r, defn, conn)
                self._publish(conn, pub, r.schema, want, every)
                for t in self._tables(r.id):                 # copies that are due
                    if t.status in ("copying", "resyncing") or (t.status == "failed" and not t.snapshot_done):
                        try:
                            self._copy(r, t, json.loads(t.columns), self._opts(defn, t.source_name, t.target))
                        except Exception as e:  # noqa: BLE001 - shown on the table, tried again next tick
                            logger.warning("database mirror %s: copying %s failed: %s", name, t.source_name, e)
                            self._set(t.id, status="failed", error=f"{type(e).__name__}: {e}"[:2000])
                tables = self._tables(r.id)
                holding = any(t.status == "failed" and t.snapshot_done for t in tables)
                limit = MAX_HELD_CHANGES if holding else MAX_CHANGES
                got = conn.execute(
                    "SELECT lsn::text, data FROM pg_logical_slot_peek_binary_changes(%s, NULL, %s, "
                    "'proto_version', '1', 'publication_names', %s)", (slot, limit, pub)).fetchall()
                last_commit = next((i for i in range(len(got) - 1, -1, -1) if bytes(got[i][1])[:1] == b"C"), None)
                if last_commit is None:
                    self._mirror_set(r.id, status="degraded" if holding else "running", error=None)
                    return 0
                txns = self._transactions(got[:last_commit + 1])
                touched = {c[0] for _, _, changes in txns for c in changes}
                key_bytes = None
                for t in tables:
                    if t.source_oid not in touched or not t.snapshot_done or t.status == "skipped":
                        continue
                    o = json.loads(t.applied)
                    try:
                        if any(c.get("flag") == "hash" for c in json.loads(t.columns).values()) or json.loads(t.gone):
                            key_bytes = key_bytes or self.hash_key()
                        table = self._load(t.target)
                        if table is None:
                            raise MirrorError(f"{t.target} is gone from the lakehouse: re-sync the table", 409)
                        n = self._write(t, o, self._changes(t, o, table, txns, key_bytes))
                        written += n
                        if n or t.status == "failed":
                            self._set(t.id, rows=database_mirror_tables.c.rows + n, error=None,
                                      **({"status": "running"} if t.status == "failed" else {}))
                    except Unplaced as e:
                        # A column nobody told Cairn about. If the source has it now, the next tick places it;
                        # if not (renamed again since, or dropped), only a fresh copy is sure to be right.
                        if t.status == "source_dropped":
                            continue
                        failed = True
                        column = str(e)
                        if column not in self._discover_columns(conn, r.schema, t.source_name):
                            reason = f"a change carried a column ({column}) the table no longer has"
                            self._set(t.id, status="resyncing", snapshot_done=False, reason=reason, error=None)
                            self._audit("table_rebuilt", "urn:li:corpuser:cairn", name,
                                        {"table": t.source_name, "target": t.target, "reason": reason})
                    except Exception as e:  # noqa: BLE001 - this table's trouble: the others go on, the slot waits
                        if t.status == "source_dropped":
                            continue                         # its last changes could not be placed: it is gone
                        failed = True
                        logger.warning("database mirror %s: %s failed: %s", name, t.source_name, e)
                        self._set(t.id, status="failed", error=f"{type(e).__name__}: {e}"[:2000])
                end = got[last_commit][0]
                if not failed:
                    # Every table has committed what it was given: only now may the server forget it.
                    conn.execute("SELECT pg_replication_slot_advance(%s, %s::pg_lsn)", (slot, end))
                    if len(got) < limit:                     # nothing older is left to decode
                        with self.engine.begin() as c:
                            c.execute(update(database_mirror_tables)
                                      .where(database_mirror_tables.c.mirror_id == r.id,
                                             database_mirror_tables.c.renames != "{}").values(renames="{}"))
            values = {"status": "degraded" if failed else "running", "error": None, "last_commit_ms": self.clock()}
            if not failed:
                values["last_lsn"] = end
            self._mirror_set(r.id, **values)
            return written
        except Exception as e:  # noqa: BLE001 - shown on the mirror, tried again next tick
            logger.warning("database mirror %s failed: %s", name, e)
            self._mirror_set(r.id, status="failed", error=f"{type(e).__name__}: {e}"[:2000])
            return 0

    @staticmethod
    def _discover_columns(conn, schema: str, table: str) -> list[str]:
        return [r[0] for r in conn.execute(
            "SELECT a.attname::text FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 "
            "AND NOT a.attisdropped AND a.attgenerated = '' ORDER BY a.attnum", (schema, table)).fetchall()]

    @staticmethod
    def _transactions(got: list) -> list[tuple]:
        """Decoded messages as transactions in commit order: ``(commit position, commit time, changes)``, each
        change ``(relation oid, kind, relation, row, unchanged, old key or row)``."""
        dec, txns, current = Decoder(raw=True), [], None
        for _, data in got:
            msg = dec.decode(bytes(data))
            if msg is None:
                continue
            if msg[0] == "begin":
                current = (msg[2]["lsn"], msg[2]["time"], [])
            elif msg[0] == "commit":
                if current is not None and current[2]:
                    txns.append(current)
                current = None
            elif current is None:
                raise MirrorError("pgoutput: a change outside a transaction", 502)
            elif msg[0] == "truncate":
                current[2].extend((oid, "truncate", None, None, None, None) for oid in msg[1])
            else:
                kind, rel, row, unchanged = msg[:4]
                old = msg[4] if kind == "update" else None
                current[2].append((rel["id"], "delete" if kind == "delete" else "upsert", rel, row, unchanged, old))
        return txns

    def run_due(self) -> int:
        """Every enabled database mirror, once (the scheduler's tick)."""
        with self.engine.connect() as c:
            names = [x.name for x in c.execute(select(database_mirrors.c.name)
                                               .where(database_mirrors.c.enabled.is_(True)))]
        return sum(self.run(n) for n in names)


def build_database_mirrors_router(m: DatabaseMirrors, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException, Response
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/database-mirrors", tags=["pipelines"])

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
        return {"database_mirrors": await call(m.list)}

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        manage(p)
        return await call(lambda: m.create(p.urn, body))

    @router.get("/{name}")
    async def get(name: str, p=Depends(current_principal)):
        manage(p)
        return await call(lambda: m.get(name))

    @router.put("/{name}")
    async def put(name: str, body: dict = Body(...), p=Depends(current_principal)):
        manage(p)
        return await call(lambda: m.update(p.urn, name, body))

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

    @router.post("/{name}/tables/{table}/resync")
    async def resync(name: str, table: str, p=Depends(current_principal)):
        manage(p)
        return await call(lambda: m.resync(p.urn, name, table))

    @router.delete("/{name}", status_code=204)
    async def delete_(name: str, p=Depends(current_principal)):
        manage(p)
        await call(lambda: m.delete(p.urn, name))
        return Response(status_code=204)

    return router
