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
from typing import Any, Optional

from sqlalchemy import Column, Integer, String, Table, select
from sqlalchemy.engine import Engine

from aegis.common.db import JSONText, metadata

GENESIS = "0" * 64

audit_log = Table(
    "aegis_audit_log",
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
                conn.exec_driver_sql("LOCK TABLE aegis_audit_log IN SHARE ROW EXCLUSIVE MODE")
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
