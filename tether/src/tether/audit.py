"""Append-only, hash-chained audit log.

Each entry stores ``hash = sha256(canonical(entry without hash) + prev_hash)``. Changing
or removing any row breaks the chain, and :func:`verify_chain` detects it. The database
blocks UPDATE, DELETE and TRUNCATE with a trigger (see the initial migration).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from tether.canonical import sha256_hex
from tether.db.models import AuditLogRow

log = logging.getLogger("tether.audit")

GENESIS_HASH = "0" * 64
_APPEND_LOCK_KEY = 0x7E7E_A0D1  # pg advisory lock serialising chain appends


def _entry_hash(
    prev_hash: str,
    ts: datetime,
    event_type: str,
    subject_id: str | None,
    entity_id: str | None,
    payload: dict[str, Any],
) -> str:
    return sha256_hex(
        {
            "prev_hash": prev_hash,
            "ts": ts.astimezone(UTC).isoformat(),
            "event_type": event_type,
            "subject_id": subject_id,
            "entity_id": entity_id,
            "payload": payload,
        }
    )


def append(
    session: Session,
    event_type: str,
    payload: dict[str, Any],
    *,
    subject_id: str | None = None,
    entity_id: str | None = None,
) -> AuditLogRow:
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _APPEND_LOCK_KEY})
    prev = session.scalars(
        select(AuditLogRow.hash).order_by(AuditLogRow.id.desc()).limit(1)
    ).first()
    prev_hash = prev or GENESIS_HASH
    ts = datetime.now(UTC)
    row = AuditLogRow(
        ts=ts,
        event_type=event_type,
        subject_id=subject_id,
        entity_id=entity_id,
        payload=payload,
        prev_hash=prev_hash,
        hash=_entry_hash(prev_hash, ts, event_type, subject_id, entity_id, payload),
    )
    session.add(row)
    session.flush()
    log.info("audit %s subject=%s entity=%s", event_type, subject_id, entity_id)
    return row


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    entries: int
    first_broken_id: int | None = None


def verify_chain(session: Session) -> ChainVerification:
    prev_hash = GENESIS_HASH
    count = 0
    for row in session.scalars(select(AuditLogRow).order_by(AuditLogRow.id)):
        count += 1
        expected = _entry_hash(
            prev_hash, row.ts, row.event_type, row.subject_id, row.entity_id, row.payload
        )
        if row.prev_hash != prev_hash or row.hash != expected:
            return ChainVerification(ok=False, entries=count, first_broken_id=row.id)
        prev_hash = row.hash
    return ChainVerification(ok=True, entries=count)
