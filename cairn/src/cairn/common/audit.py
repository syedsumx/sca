"""Append-only, hash-chained audit log.

Each entry stores ``hash = sha256(prev_hash || canonical_json(entry_body))``. Any
in-place modification or deletion of a row breaks the chain from that point on, which
:func:`verify_chain` detects. For stronger guarantees, periodically anchor the head
hash externally (e.g. object-lock S3, a transparency log).
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional

from sqlalchemy import Column, Integer, String, Table, select
from sqlalchemy.engine import Engine

from cairn.common.db import JSONText, metadata

GENESIS = "0" * 64

audit_log = Table(
    "cairn_audit_log",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("id", String(36), nullable=False, unique=True),
    Column("ts", String(40), nullable=False),
    Column("category", String(64), nullable=False, index=True),
    Column("action", String(128), nullable=False),
    Column("actor", String(512), nullable=False),
    Column("subject", String(1024), nullable=True, index=True),
    Column("details", JSONText, nullable=True),
    Column("prev_hash", String(64), nullable=False),
    Column("hash", String(64), nullable=False),
)


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()


def _entry_hash(prev_hash: str, body: dict) -> str:
    return hashlib.sha256(prev_hash.encode() + _canonical(body)).hexdigest()


class AuditLog:
    def __init__(self, engine: Engine):
        self.engine = engine
        # Serializes appends within a process; across processes Postgres serializes
        # through the unique `id` + reading the head inside the same transaction.
        self._lock = threading.Lock()

    def record(
        self,
        category: str,
        action: str,
        actor: str,
        subject: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> str:
        body = {
            "id": str(uuid.uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "category": category,
            "action": action,
            "actor": actor,
            "subject": subject,
            "details": details or {},
        }
        with self._lock, self.engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                conn.exec_driver_sql("LOCK TABLE cairn_audit_log IN SHARE ROW EXCLUSIVE MODE")
            head = conn.execute(
                select(audit_log.c.hash).order_by(audit_log.c.seq.desc()).limit(1)
            ).scalar()
            prev = head or GENESIS
            digest = _entry_hash(prev, body)
            conn.execute(audit_log.insert().values(**body, prev_hash=prev, hash=digest))
        return body["id"]

    def entries(self, category: Optional[str] = None, limit: int = 100) -> list[dict]:
        stmt = select(audit_log).order_by(audit_log.c.seq.desc()).limit(limit)
        if category:
            stmt = stmt.where(audit_log.c.category == category)
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(stmt)]

    def verify_chain(self) -> dict:
        prev = GENESIS
        count = 0
        with self.engine.connect() as conn:
            for row in conn.execute(select(audit_log).order_by(audit_log.c.seq)):
                r = row._mapping
                body = {
                    k: r[k]
                    for k in ("id", "ts", "category", "action", "actor", "subject", "details")
                }
                if r["prev_hash"] != prev or _entry_hash(prev, body) != r["hash"]:
                    return {"valid": False, "broken_at_seq": r["seq"], "entries_checked": count}
                prev = r["hash"]
                count += 1
        return {"valid": True, "entries_checked": count, "head": prev}

    def export(self, after_seq: int = 0, since: Optional[str] = None, until: Optional[str] = None,
               category: Optional[str] = None, batch: int = 1000) -> Iterator[dict]:
        """Entries in chain order (``seq`` ascending) after ``after_seq``, with their hashes, so a
        receiver can verify the export on its own (:func:`verify_entries`) and resume from the last
        ``seq`` it stored. ``since``/``until`` are ISO-8601 timestamps (inclusive / exclusive)."""
        cursor = after_seq
        while True:
            stmt = select(audit_log).where(audit_log.c.seq > cursor).order_by(audit_log.c.seq).limit(batch)
            if since:
                stmt = stmt.where(audit_log.c.ts >= since)
            if until:
                stmt = stmt.where(audit_log.c.ts < until)
            if category:
                stmt = stmt.where(audit_log.c.category == category)
            with self.engine.connect() as conn:
                rows = [dict(r._mapping) for r in conn.execute(stmt)]
            yield from rows
            if len(rows) < batch:
                return
            cursor = rows[-1]["seq"]


def verify_entries(entries: Iterable[dict]) -> dict:
    """Verify an exported, contiguous slice of the chain (e.g. a JSONL export) without the database.

    Each entry's hash must match its content and the previous entry's hash. The first entry's
    ``prev_hash`` is taken as given; compare it with the ``head`` of the previous export to prove
    nothing was removed between exports."""
    prev: Optional[str] = None
    count = 0
    first_prev = None
    for e in entries:
        body = {k: e.get(k) for k in ("id", "ts", "category", "action", "actor", "subject", "details")}
        if prev is None:
            first_prev = e.get("prev_hash")
        elif e.get("prev_hash") != prev:
            return {"valid": False, "broken_at_seq": e.get("seq"), "entries_checked": count,
                    "reason": "gap or reordering (prev_hash does not match the previous entry)"}
        if _entry_hash(e.get("prev_hash") or "", body) != e.get("hash"):
            return {"valid": False, "broken_at_seq": e.get("seq"), "entries_checked": count,
                    "reason": "entry content does not match its hash"}
        prev = e["hash"]
        count += 1
    return {"valid": True, "entries_checked": count, "starts_after": first_prev, "head": prev}


EXPORT_FIELDS = ("seq", "id", "ts", "category", "action", "actor", "subject", "details", "prev_hash", "hash")


def export_lines(entries: Iterable[dict], fmt: str = "jsonl", header: bool = True) -> Iterator[str]:
    """JSON Lines (one entry per line) or CSV (``details`` as JSON) for :meth:`AuditLog.export`."""
    if fmt == "csv":
        import csv
        import io

        buf = io.StringIO()
        writer = csv.writer(buf)
        if header:
            writer.writerow(EXPORT_FIELDS)
        for e in entries:
            writer.writerow([json.dumps(e.get(k), sort_keys=True) if k == "details" else e.get(k)
                             for k in EXPORT_FIELDS])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
        if buf.tell():
            yield buf.getvalue()
        return
    for e in entries:
        yield json.dumps({k: e.get(k) for k in EXPORT_FIELDS}, sort_keys=True, default=str) + "\n"
