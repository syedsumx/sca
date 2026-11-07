"""SQL Server as a mirror source: a SQL Server table kept in a lakehouse table through change data capture (CDC).

Used by :class:`cairn.pipelines.mirror.Mirrors` for connections whose URL is ``mssql://`` (or ``mssql+pymssql://``,
``sqlserver://``): ``mssql://user:password@host:1433/database``. A database administrator turns CDC on for the
database and the table (``EXEC sys.sp_cdc_enable_db`` and ``EXEC sys.sp_cdc_enable_table @source_schema = N'dbo',
@source_name = N'orders', @role_name = NULL``), and SQL Server Agent runs the capture job. The connection's user
needs ``SELECT`` on the table and on its change table (``db_owner``, or membership of the gating role if one was
named). Cairn changes nothing on the server.

The copy is taken after reading the highest change number (LSN) captured so far; changes after it are then applied
in commit order, whole transactions at a time, so the lakehouse table converges on the source (a change replayed
over the copy writes the same row again). Where a mirror has read to is its own record (the commit LSN of the last
transaction it applied); CDC's cleanup job keeps changes for three days by default, so a mirror paused for longer
finds them gone, fails and says so: recreate it to copy the table again.

Offsets are commit LSNs (their upper 64 bits), and every lakehouse commit records the next one, so a crash between
the commit and the mirror's own record replays changes that are then skipped.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

MAX_ROWS = 20_000
URL = re.compile(r"^(mssql(\+\w+)?|sqlserver)://")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
ENABLE = ("turn change data capture on for the database and the table (EXEC sys.sp_cdc_enable_db; "
          "EXEC sys.sp_cdc_enable_table @source_schema = N'{schema}', @source_name = N'{table}', @role_name = NULL) "
          "and keep SQL Server Agent running")


def settings(url: str) -> dict:
    u = urlsplit(URL.sub("mssql://", url))
    if u.scheme != "mssql":
        raise ValueError("not an mssql:// URL")
    return {"server": u.hostname or "localhost", "port": u.port or 1433, "user": unquote(u.username or ""),
            "password": unquote(u.password or ""), "database": (u.path or "/").lstrip("/") or "master"}


def connect(url: str):
    import pymssql

    s = settings(url)
    return pymssql.connect(server=s["server"], port=s["port"], user=s["user"], password=s["password"],
                           database=s["database"], login_timeout=10, timeout=120, autocommit=True)


def lsn_hex(lsn: bytes) -> str:
    return "0x" + bytes(lsn).hex().upper()


def lsn_bytes(text: str) -> bytes:
    return bytes.fromhex(text[2:] if text.lower().startswith("0x") else text)


def offset(lsn: bytes) -> int:
    """A commit LSN as a stream offset: its upper 64 bits (the log block; ordered like the LSN)."""
    return min(int.from_bytes(bytes(lsn), "big") >> 16, 2 ** 63 - 1)


def check(conn, schema: str, table: str) -> tuple[list[str], str]:
    """``(primary key, capture instance)``, after checking CDC is on for the table."""
    with conn.cursor() as cur:
        cur.execute("SELECT is_cdc_enabled FROM sys.databases WHERE name = DB_NAME()")
        row = cur.fetchone()
        if not row or not row[0]:
            raise ValueError("change data capture is off for the database: " + ENABLE.format(schema=schema, table=table))
        cur.execute("SELECT TOP 1 ct.capture_instance FROM cdc.change_tables ct "
                    "JOIN sys.tables t ON t.object_id = ct.source_object_id "
                    "JOIN sys.schemas s ON s.schema_id = t.schema_id WHERE s.name = %s AND t.name = %s "
                    "ORDER BY ct.create_date DESC", (schema, table))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"change data capture is off for {schema}.{table}: " + ENABLE.format(schema=schema, table=table))
        instance = str(row[0])
        if not IDENT.match(instance):
            raise ValueError(f"unexpected capture instance name {instance!r}")
        cur.execute("SELECT k.COLUMN_NAME FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS c "
                    "JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k ON k.CONSTRAINT_NAME = c.CONSTRAINT_NAME "
                    "AND k.TABLE_SCHEMA = c.TABLE_SCHEMA AND k.TABLE_NAME = c.TABLE_NAME "
                    "WHERE c.CONSTRAINT_TYPE = 'PRIMARY KEY' AND c.TABLE_SCHEMA = %s AND c.TABLE_NAME = %s "
                    "ORDER BY k.ORDINAL_POSITION", (schema, table))
        return [r[0] for r in cur.fetchall()], instance


ZERO = b"\x00" * 10        # "from the start of the capture instance": nothing was captured when the copy began


def position(conn, instance: str) -> bytes:
    """Where changes after a copy taken now start: the highest LSN the capture job has recorded, or ``ZERO`` (the
    capture instance's start) when it has recorded nothing for this table yet or the instance began after it, as
    right after CDC is turned on for the table."""
    with conn.cursor() as cur:
        cur.execute("SELECT sys.fn_cdc_get_max_lsn(), sys.fn_cdc_get_min_lsn(%s)", (instance,))
        high, low = (bytes(x) if x is not None else None for x in cur.fetchone())
    if high is None or high == ZERO or low is None or low == ZERO or high < low:
        return ZERO
    return high


def copy(conn, schema: str, table: str, batch: int = 5000):
    """The table's rows, in batches of dicts."""
    with conn.cursor(as_dict=True) as cur:
        cur.execute(f"SELECT * FROM [{schema}].[{table}]")
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                return
            yield rows


def changes(conn, instance: str, after: bytes, key: list[str]) -> tuple[list, Optional[bytes]]:
    """Whole transactions committed after ``after``: ``([(offset, op, row)], commit LSN of the last one or None)``.
    ``op`` is upsert or delete."""
    if not IDENT.match(instance):
        raise ValueError("bad capture instance")
    with conn.cursor() as cur:
        cur.execute("SELECT sys.fn_cdc_increment_lsn(%s), sys.fn_cdc_get_min_lsn(%s), sys.fn_cdc_get_max_lsn()",
                    (after, instance))
        start, low, high = (bytes(x) if x is not None else None for x in cur.fetchone())
    if low is None or low == ZERO or high is None or high == ZERO:
        return [], None                                   # the capture job has not recorded this table yet
    if bytes(after) == ZERO:
        start = low                                       # everything the instance has captured
    elif low > start:
        raise RuntimeError("the changes this mirror had read to are gone from the server (purged by CDC cleanup): "
                           "delete the mirror and create it again to copy the table afresh")
    if start is None or start > high:
        return [], None
    with conn.cursor(as_dict=True) as cur:
        cur.execute(f"SELECT * FROM cdc.fn_cdc_get_all_changes_{instance}(%s, %s, N'all update old') "
                    "ORDER BY __$start_lsn, __$seqval, __$operation", (start, high))
        got = cur.fetchall()
    out: list = []
    end: Optional[bytes] = None
    before: dict = {}
    current: Optional[bytes] = None
    for r in got:
        lsn = bytes(r["__$start_lsn"])
        if lsn != current:
            if current is not None:
                end = current                              # the previous transaction is whole
                if len(out) >= MAX_ROWS:
                    break
            current = lsn
        op = int(r["__$operation"])
        row = {k: v for k, v in r.items() if not k.startswith("__$")}
        at = offset(lsn)
        if op == 1:
            out.append((at, "delete", row))
        elif op == 2:
            out.append((at, "upsert", row))
        elif op == 3:
            before[bytes(r["__$seqval"])] = row
        elif op == 4:
            old: Any = before.pop(bytes(r["__$seqval"]), None)
            if old is not None and any(old.get(k) != row.get(k) for k in key):   # the key changed: the old row goes
                out.append((at, "delete", {k: old.get(k) for k in key}))
            out.append((at, "upsert", row))
    else:
        end = current                                      # every transaction read is whole
    return out, end
