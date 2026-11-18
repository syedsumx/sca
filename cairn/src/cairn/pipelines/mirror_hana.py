"""SAP HANA as a mirror source: a HANA table kept in a lakehouse table through change triggers.

Used by :class:`cairn.pipelines.mirror.Mirrors` for connections whose URL is ``hana://``:
``hana://user:password@host:39041`` (``/TENANT`` to reach a tenant through the system database's port;
``?encrypt=true`` for TLS, as SAP HANA Cloud requires, with ``&validate=false`` only for a test server).

HANA has no public interface to its redo log: SAP's own replication from a HANA source (Smart Data Integration's
HANA adapter) captures changes with triggers, and so does Cairn. When a mirror starts, Cairn creates, as the
connection's user:

* a change table ``CAIRN_CT_<mirror id>`` in the user's own schema: a sequence number and the table's key;
* three row triggers on the table (``CAIRN_<mirror id>_I``, ``_U``, ``_D``, in the table's schema) that put the key of
  every inserted, updated or deleted row there (an update puts the old key and the new one).

The connection's user needs ``SELECT`` and ``TRIGGER`` on the table (or its schema) and may create tables in its own
schema (every HANA user can, unless restricted). Nothing else changes on the server, and deleting the mirror drops
the triggers and the change table.

* **The copy.** Taken after the triggers exist. HANA creates a trigger only once every transaction writing the table
  has ended, so every write is either in the copy or behind a trigger.
* **Changes.** Each run reads the change table's committed rows in order, reads the rows those keys name back from
  the table (their current, committed values), upserts those found and deletes the others, commits to the lakehouse,
  and only then deletes the change rows it read. A crash in between reads them again, which writes the same rows:
  the lakehouse table converges on the source, whatever order transactions committed in. Intermediate values of a
  row changed several times between runs are not kept (a mirror holds the latest state, as with every source).
* **Not seen.** ``TRUNCATE TABLE`` fires no trigger: delete rows with ``DELETE``, or delete and create the mirror.
  A column added at the source is picked up on the next change of each row (and copied with it); the lakehouse table
  gains it at once, NULL until then.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlsplit

MAX_ROWS = 20_000
URL = re.compile(r"^(hana|hana\+\w+|sap\+hana)://")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]{0,126}$")
KEY_TYPES = {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "DECIMAL", "VARCHAR", "NVARCHAR", "ALPHANUM", "DATE", "TIME",
             "SECONDDATE", "TIMESTAMP", "VARBINARY", "CHAR", "NCHAR", "BOOLEAN", "SMALLDECIMAL", "REAL", "DOUBLE"}


def q(name: str) -> str:
    """A HANA identifier, quoted."""
    return '"' + name.replace('"', '""') + '"'


@dataclass
class Col:
    name: str               # as HANA has it
    out: str                # the lakehouse column: lower case
    kind: str               # int dec float bool date ts time text bin
    sqltype: str            # the column's type as DDL says it (for the change table's key columns)
    precision: Optional[int] = None
    scale: int = 0


@dataclass
class Desc:
    schema: str
    table: str
    columns: list[Col]
    key: list[str]          # HANA names
    user: str               # the connection's user: the change table is in its schema
    by_name: dict = field(default_factory=dict)

    def __post_init__(self):
        self.by_name = {c.name: c for c in self.columns}

    @property
    def qualified(self) -> str:
        return f"{q(self.schema)}.{q(self.table)}"

    @property
    def out_key(self) -> list[str]:
        return [self.by_name[k].out for k in self.key]


# ---- connections ------------------------------------------------------------------------------
def settings(url: str) -> dict:
    u = urlsplit(URL.sub("hana://", url))
    if u.scheme != "hana":
        raise ValueError("not a hana:// URL")
    params = {k: v[0] for k, v in parse_qs(u.query).items()}
    database = (u.path or "/").strip("/")
    if database and not IDENT.match(database):
        raise ValueError("a hana:// URL's path names a tenant database")
    return {"host": u.hostname or "localhost", "port": u.port or 39041, "user": unquote(u.username or ""),
            "password": unquote(u.password or ""), "database": database or None,
            "encrypt": params.get("encrypt", "false").lower() == "true",
            "validate": params.get("validate", "true").lower() != "false"}


def connect(url: str):
    try:
        from hdbcli import dbapi
    except ImportError:
        raise ValueError("SAP HANA mirrors need SAP's hdbcli driver, which is under SAP's licence and not installed "
                         "with Cairn: install it in the API's image (pip install 'cairn-platform[hana]')") from None
    s = settings(url)
    options = {"encrypt": s["encrypt"], "sslValidateCertificate": s["validate"], "connectTimeout": 10_000,
               "communicationTimeout": 600_000}
    if s["database"]:
        options["databaseName"] = s["database"]
    conn = dbapi.connect(address=s["host"], port=s["port"], user=s["user"], password=s["password"], **options)
    conn.setautocommit(True)
    return conn


# ---- what a table needs -----------------------------------------------------------------------
def kind(typename: str, length: Optional[int], scale: Optional[int]) -> tuple[str, Optional[int], int]:
    """How a HANA column lands: ``(kind, precision, scale)``."""
    t = typename.upper()
    if t in ("TINYINT", "SMALLINT", "INTEGER", "BIGINT"):
        return "int", None, 0
    if t == "DECIMAL" and scale is not None and length:
        if length > 38:
            return "text", None, 0
        return "dec", int(length), int(scale)
    if t in ("REAL", "DOUBLE"):
        return "float", None, 0
    if t == "BOOLEAN":
        return "bool", None, 0
    if t == "DATE":
        return "date", None, 0
    if t in ("SECONDDATE", "TIMESTAMP"):
        return "ts", None, 0
    if t == "TIME":
        return "time", None, 0
    if t in ("VARBINARY", "BINARY", "BLOB"):
        return "bin", None, 0
    return "text", None, 0         # (N)VARCHAR, ALPHANUM, (N)CLOB, TEXT, floating DECIMAL and SMALLDECIMAL (exact)


def sqltype(typename: str, length: Optional[int], scale: Optional[int]) -> str:
    t = typename.upper()
    if t == "DECIMAL" and scale is not None and length:
        return f"DECIMAL({int(length)},{int(scale)})"
    if t in ("VARCHAR", "NVARCHAR", "ALPHANUM", "VARBINARY", "CHAR", "NCHAR", "BINARY"):
        return f"{t}({int(length or 1)})"
    return t


def arrow_schema(desc: Desc):
    import pyarrow as pa

    types = {"int": pa.int64(), "float": pa.float64(), "bool": pa.bool_(), "date": pa.date32(),
             "ts": pa.timestamp("us")}
    return pa.schema([pa.field(c.out, pa.decimal128(c.precision, c.scale) if c.kind == "dec"
                               else types.get(c.kind, pa.string())) for c in desc.columns])


def value(col: Col, v: Any) -> Any:
    if v is None:
        return None
    if hasattr(v, "read"):                                 # a LOB
        v = v.read()
    if col.kind == "dec":
        return v if isinstance(v, Decimal) else Decimal(str(v))
    if col.kind == "float":
        return float(v)
    if col.kind == "bin":
        return bytes(v).hex()
    if col.kind == "time":
        return v.isoformat() if isinstance(v, dt.time) else str(v)
    if col.kind == "text":
        if isinstance(v, (bytes, bytearray, memoryview)):
            return bytes(v).decode("utf-8", "replace")
        return v if isinstance(v, str) else str(v)
    return v


def _rows(cur, sql: str, params: tuple = ()) -> list:
    cur.execute(sql, params)
    return cur.fetchall()


def check(conn, schema: str, table: str) -> Desc:
    """The table and what a mirror needs of it; a ``ValueError`` says what to grant or change otherwise."""
    cur = conn.cursor()
    try:
        user = _rows(cur, "SELECT CURRENT_USER FROM DUMMY")[0][0]
        got = _rows(cur, "SELECT SCHEMA_NAME, TABLE_NAME, IS_TEMPORARY FROM SYS.TABLES WHERE "
                         "(SCHEMA_NAME = ? AND TABLE_NAME = ?) OR (SCHEMA_NAME = ? AND TABLE_NAME = ?)",
                    (schema, table, schema.upper(), table.upper()))
        if not got:
            raise ValueError(f"{schema}.{table} is not a table the connection's user can see: "
                             f"GRANT SELECT, TRIGGER ON {q(schema)}.{q(table)} TO {q(user)}")
        schema, table = got[0][0], got[0][1]
        if str(got[0][2]).upper() == "TRUE":
            raise ValueError(f"{schema}.{table} is a temporary table")
        cols = _rows(cur, "SELECT COLUMN_NAME, DATA_TYPE_NAME, LENGTH, SCALE FROM SYS.TABLE_COLUMNS "
                          "WHERE SCHEMA_NAME = ? AND TABLE_NAME = ? ORDER BY POSITION", (schema, table))
        key = [r[0] for r in _rows(cur, "SELECT COLUMN_NAME FROM SYS.CONSTRAINTS WHERE SCHEMA_NAME = ? AND "
                                        "TABLE_NAME = ? AND IS_PRIMARY_KEY = 'TRUE' ORDER BY POSITION",
                                   (schema, table))]
        privs = {(r[0], r[1]) for r in _rows(cur, "SELECT OBJECT_TYPE, PRIVILEGE FROM SYS.EFFECTIVE_PRIVILEGES "
                                                  "WHERE USER_NAME = CURRENT_USER AND ((OBJECT_TYPE = 'TABLE' AND "
                                                  "SCHEMA_NAME = ? AND OBJECT_NAME = ?) OR (OBJECT_TYPE = 'SCHEMA' "
                                                  "AND SCHEMA_NAME = ?))", (schema, table, schema))}
        owner = _rows(cur, "SELECT SCHEMA_OWNER FROM SYS.SCHEMAS WHERE SCHEMA_NAME = ?", (schema,))
    finally:
        cur.close()
    columns = []
    for name, typename, length, scale in cols:
        k, precision, sc = kind(typename, length, scale)
        columns.append(Col(name=name, out=name.lower(), kind=k, sqltype=sqltype(typename, length, scale),
                           precision=precision, scale=sc))
    if len({c.out for c in columns}) != len(columns):
        raise ValueError(f"{schema}.{table} has columns whose names differ only in case")
    if not key:
        raise ValueError(f"{schema}.{table} has no primary key: a mirror applies changes by key")
    desc = Desc(schema=schema, table=table, columns=columns, key=key, user=user)
    for k in key:
        t = desc.by_name[k].sqltype.split("(")[0]
        if t not in KEY_TYPES:
            raise ValueError(f"{schema}.{table}'s key column {k} is of type {t}, which a change table cannot hold")
    is_owner = bool(owner) and owner[0][0] == user
    missing = [p for p in ("SELECT", "TRIGGER") if not is_owner and not any(x[1] == p for x in privs)]
    if missing:
        raise ValueError(f"the connection's user needs GRANT {', '.join(missing)} ON {q(schema)}.{q(table)} "
                         f"TO {q(user)}")
    return desc


# ---- the change table and its triggers -----------------------------------------------------------
def names(mid: str) -> tuple[str, list[str]]:
    """The change table's name and the three triggers'."""
    m = mid.upper()
    return f"CAIRN_CT_{m}", [f"CAIRN_{m}_I", f"CAIRN_{m}_U", f"CAIRN_{m}_D"]


def _ct(desc: Desc, mid: str) -> str:
    return f"{q(desc.user)}.{q(names(mid)[0])}"


def capture(conn, desc: Desc, mid: str) -> bool:
    """Makes the change table and the triggers that are missing. True if any was made (a new start: the table must be
    copied after them)."""
    ct, triggers = names(mid)
    cur = conn.cursor()
    made = False
    try:
        if not _rows(cur, "SELECT 1 FROM SYS.TABLES WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?", (desc.user, ct)):
            cols = ", ".join(f"{q(k)} {desc.by_name[k].sqltype}" for k in desc.key)
            cur.execute(f"CREATE COLUMN TABLE {_ct(desc, mid)} (CAIRN_SEQ BIGINT GENERATED BY DEFAULT AS IDENTITY "
                        f"PRIMARY KEY, {cols})")
            made = True
        have = {r[0] for r in _rows(cur, "SELECT TRIGGER_NAME FROM SYS.TRIGGERS WHERE SCHEMA_NAME = ? AND "
                                         "SUBJECT_TABLE_SCHEMA = ? AND SUBJECT_TABLE_NAME = ?",
                                    (desc.schema, desc.schema, desc.table))}
        keys = ", ".join(q(k) for k in desc.key)
        for name, event, ref, rows in ((triggers[0], "INSERT", "REFERENCING NEW ROW CAIRN_NEW", ["CAIRN_NEW"]),
                                       (triggers[1], "UPDATE", "REFERENCING NEW ROW CAIRN_NEW, OLD ROW CAIRN_OLD",
                                        ["CAIRN_OLD", "CAIRN_NEW"]),
                                       (triggers[2], "DELETE", "REFERENCING OLD ROW CAIRN_OLD", ["CAIRN_OLD"])):
            if name in have:
                continue
            body = " ".join(f"INSERT INTO {_ct(desc, mid)} ({keys}) VALUES "
                            f"({', '.join(f':{r}.{q(k)}' for k in desc.key)});" for r in rows)
            # HANA creates the trigger once every transaction writing the table has ended.
            cur.execute(f"CREATE TRIGGER {q(desc.schema)}.{q(name)} AFTER {event} ON {desc.qualified} {ref} "
                        f"FOR EACH ROW BEGIN {body} END")
            made = True
    finally:
        cur.close()
    return made


def drop(conn, schema: str, user: str, mid: str) -> None:
    """The triggers and the change table go (the triggers first: nothing writes the change table after)."""
    ct, triggers = names(mid)
    cur = conn.cursor()
    try:
        have = {r[0] for r in _rows(cur, "SELECT TRIGGER_NAME FROM SYS.TRIGGERS WHERE SCHEMA_NAME = ?", (schema,))}
        for t in triggers:
            if t in have:
                cur.execute(f"DROP TRIGGER {q(schema)}.{q(t)}")
        if _rows(cur, "SELECT 1 FROM SYS.TABLES WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?", (user, ct)):
            cur.execute(f"DROP TABLE {q(user)}.{q(ct)}")
    finally:
        cur.close()


def left_behind(schema: str, user: str, mid: str) -> str:
    """The statements that remove what a mirror made, for an operator when Cairn could not."""
    ct, triggers = names(mid)
    return "; ".join([f"DROP TRIGGER {q(schema)}.{q(t)}" for t in triggers] + [f"DROP TABLE {q(user)}.{q(ct)}"])


def copy(conn, desc: Desc, batch: int = 5000):
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT {', '.join(q(c.name) for c in desc.columns)} FROM {desc.qualified}")
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                return
            yield [{c.out: value(c, v) for c, v in zip(desc.columns, r)} for r in rows]
    finally:
        cur.close()


def pending(conn, desc: Desc, mid: str, max_rows: int = MAX_ROWS) -> list[tuple]:
    """Committed change rows, oldest first: ``[(seq, key values...)]``."""
    cur = conn.cursor()
    try:
        return [tuple(r) for r in _rows(cur, f"SELECT TOP {int(max_rows)} CAIRN_SEQ, "
                                             f"{', '.join(q(k) for k in desc.key)} FROM {_ct(desc, mid)} "
                                             "ORDER BY CAIRN_SEQ")]
    finally:
        cur.close()


def current(conn, desc: Desc, keys: list[tuple], chunk: int = 200) -> list[tuple[str, dict]]:
    """For each key: ``("upsert", row)`` with the row as it is now, or ``("delete", key)`` if it is gone."""
    out = []
    cols = ", ".join(q(c.name) for c in desc.columns)
    pos = [desc.columns.index(desc.by_name[k]) for k in desc.key]
    cur = conn.cursor()
    try:
        for i in range(0, len(keys), chunk):
            part = keys[i:i + chunk]
            cond = " OR ".join("(" + " AND ".join(f"{q(k)} = ?" for k in desc.key) + ")" for _ in part)
            found = {}
            for r in _rows(cur, f"SELECT {cols} FROM {desc.qualified} WHERE {cond}", tuple(v for k in part for v in k)):
                found[tuple(_norm(r[p]) for p in pos)] = r
            for k in part:
                r = found.get(tuple(_norm(v) for v in k))
                if r is None:
                    out.append(("delete", {desc.by_name[n].out: value(desc.by_name[n], v) for n, v in zip(desc.key, k)}))
                else:
                    out.append(("upsert", {c.out: value(c, v) for c, v in zip(desc.columns, r)}))
    finally:
        cur.close()
    return out


def _norm(v: Any) -> Any:
    if isinstance(v, memoryview):
        return bytes(v)
    if isinstance(v, str):
        return v.rstrip(" ")                               # CHAR keys come back padded
    return v


def done(conn, desc: Desc, mid: str, seqs: list[int], chunk: int = 1000) -> None:
    """The change rows read are in the lakehouse: they go (only those: rows of transactions that committed later with
    a lower number stay for the next run)."""
    cur = conn.cursor()
    try:
        for i in range(0, len(seqs), chunk):
            part = seqs[i:i + chunk]
            cur.execute(f"DELETE FROM {_ct(desc, mid)} WHERE CAIRN_SEQ IN ({', '.join('?' for _ in part)})",
                        tuple(part))
    finally:
        cur.close()


def changes(conn, desc: Desc, mid: str, max_rows: int = MAX_ROWS) -> tuple[list[tuple[str, dict]], list[int]]:
    """``([(op, row)], seqs read)``: each changed key once, as the table holds it now."""
    got = pending(conn, desc, mid, max_rows)
    if not got:
        return [], []
    keys, seen = [], set()
    for r in got:
        k = tuple(r[1:])
        nk = tuple(_norm(v) for v in k)
        if nk not in seen:
            seen.add(nk)
            keys.append(k)
    return current(conn, desc, keys), [int(r[0]) for r in got]
