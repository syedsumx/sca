"""MySQL as a mirror source: a MySQL table kept in a lakehouse table through the binary log.

Used by :class:`cairn.pipelines.mirror.Mirrors` for connections whose URL is ``mysql://`` (or
``mysql+pymysql://``). The server keeps a row-based binary log (``log_bin`` on, ``binlog_format = ROW`` and
``binlog_row_image = FULL``, MySQL 8's defaults, and ``binlog_row_metadata = FULL``, which is not); the
connection's user has ``REPLICATION SLAVE``,
``REPLICATION CLIENT`` and ``SELECT`` on the table. MySQL has no replication slots: where a mirror has read to is
its own record (the position after the last transaction it committed), so the server's
``binlog_expire_logs_seconds`` must keep logs longer than a mirror may be paused. A mirror that finds its position
purged fails and says so; recreate it to copy the table again.

Offsets are binary log positions, ``(file number << 32) | position``, and every lakehouse commit records the next
one, so a crash between the commit and the mirror's own record replays changes that are then skipped.
"""

from __future__ import annotations

import random
import re
from typing import Any
from urllib.parse import unquote, urlsplit

MAX_ROWS = 20_000


def settings(url: str) -> dict:
    u = urlsplit(re.sub(r"^mysql\+\w+://", "mysql://", url))
    if u.scheme != "mysql":
        raise ValueError("not a mysql:// URL")
    return {"host": u.hostname or "localhost", "port": u.port or 3306, "user": unquote(u.username or ""),
            "passwd": unquote(u.password or "")}


def connect(url: str):
    import pymysql

    s = settings(url)
    return pymysql.connect(host=s["host"], port=s["port"], user=s["user"], password=s["passwd"], connect_timeout=10,
                           autocommit=True, charset="utf8mb4")


def offset(log_file: str, pos: int) -> int:
    return (int(log_file.rsplit(".", 1)[1]) << 32) | int(pos)


def position(conn) -> tuple[str, int]:
    """The binary log's current file and position."""
    with conn.cursor() as cur:
        try:
            cur.execute("SHOW BINARY LOG STATUS")           # MySQL 8.2 and later
        except Exception:  # noqa: BLE001 - older servers
            cur.execute("SHOW MASTER STATUS")
        row = cur.fetchone()
    if not row:
        raise RuntimeError("the server keeps no binary log (log_bin is off)")
    return row[0], int(row[1])


def check(conn, schema: str, table: str) -> list[str]:
    """The table's primary key, after checking the server can be mirrored."""
    with conn.cursor() as cur:
        cur.execute("SELECT @@log_bin, @@binlog_format, @@binlog_row_image, @@binlog_row_metadata")
        log_bin, fmt, image, meta = cur.fetchone()
        if not log_bin or str(fmt).upper() != "ROW" or str(image).upper() != "FULL" or str(meta).upper() != "FULL":
            raise ValueError("the source needs log_bin on, binlog_format = ROW, binlog_row_image = FULL and "
                             "binlog_row_metadata = FULL (so each change carries its column names)")
        cur.execute("SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = %s "
                    "AND TABLE_NAME = %s AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY ORDINAL_POSITION", (schema, table))
        return [r[0] for r in cur.fetchall()]


def copy(conn, schema: str, table: str, batch: int = 5000):
    """The table's rows, in batches of dicts."""
    import pymysql

    with conn.cursor(pymysql.cursors.SSDictCursor) as cur:
        cur.execute(f"SELECT * FROM `{schema}`.`{table}`")
        while True:
            rows = cur.fetchmany(batch)
            if not rows:
                return
            yield rows


def changes(url: str, schema: str, table: str, log_file: str, pos: int, key: list[str]) -> tuple[list, Any]:
    """Whole transactions after ``(log_file, pos)``: ``([(offset, op, row)], (file, pos) after the last one or
    None)``. ``op`` is upsert, delete or truncate."""
    from pymysqlreplication import BinLogStreamReader
    from pymysqlreplication.event import QueryEvent, XidEvent
    from pymysqlreplication.row_event import DeleteRowsEvent, UpdateRowsEvent, WriteRowsEvent

    stream = BinLogStreamReader(connection_settings=settings(url), server_id=random.randint(100_000, 2_000_000_000),
                                resume_stream=True, log_file=log_file, log_pos=pos, blocking=False,
                                only_events=[WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent, XidEvent, QueryEvent])
    done: list = []
    pending: list = []
    end = None
    truncate = re.compile(rf"^\s*TRUNCATE\s+(TABLE\s+)?(`?{re.escape(schema)}`?\.)?`?{re.escape(table)}`?\s*$", re.I)
    try:
        for ev in stream:
            at = offset(stream.log_file, ev.packet.log_pos)
            if isinstance(ev, XidEvent):                 # a transaction committed: what it changed counts
                done += pending
                pending = []
                end = (stream.log_file, ev.packet.log_pos)
                if len(done) >= MAX_ROWS:
                    break
                continue
            if isinstance(ev, QueryEvent):
                q = ev.query if isinstance(ev.query, str) else ev.query.decode("utf-8", "replace")
                db = ev.schema.decode() if isinstance(ev.schema, bytes) else ev.schema
                if truncate.match(q) and (db == schema or f"{schema}." in q or f"`{schema}`." in q):
                    done += pending + [(at, "truncate", {})]      # DDL commits on its own
                    pending = []
                    end = (stream.log_file, ev.packet.log_pos)
                continue
            if ev.schema != schema or ev.table != table:
                continue
            for row in ev.rows:
                if isinstance(ev, WriteRowsEvent):
                    pending.append((at, "upsert", row["values"]))
                elif isinstance(ev, DeleteRowsEvent):
                    pending.append((at, "delete", row["values"]))
                else:
                    before, after = row["before_values"], row["after_values"]
                    if any(before.get(k) != after.get(k) for k in key):   # the key changed: the old row goes
                        pending.append((at, "delete", {k: before.get(k) for k in key}))
                    pending.append((at, "upsert", after))
    finally:
        stream.close()
    return done, end
