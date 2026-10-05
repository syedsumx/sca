"""A small Postgres-backed job queue.

Jobs are claimed with ``FOR UPDATE SKIP LOCKED``, so any number of workers can poll safely.
A job left ``running`` by a crashed worker is reclaimed once its lease runs out.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from tether.db.models import JobRow

LEASE = timedelta(minutes=5)


def enqueue(session: Session, kind: str, payload: dict[str, Any], *, dedupe: bool = True) -> JobRow:
    """Add a job. With ``dedupe``, an identical queued or running job is reused."""
    if dedupe:
        existing = session.scalars(
            select(JobRow).where(
                JobRow.kind == kind,
                JobRow.payload == payload,
                JobRow.status.in_(["queued", "running"]),
            )
        ).first()
        if existing is not None:
            return existing
    job = JobRow(kind=kind, payload=payload, status="queued")
    session.add(job)
    session.flush()
    return job


def claim(session: Session) -> JobRow | None:
    now = datetime.now(UTC)
    job = session.scalars(
        select(JobRow)
        .where(
            or_(
                (JobRow.status == "queued") & (JobRow.run_after <= now),
                (JobRow.status == "running") & (JobRow.locked_at < now - LEASE),
            )
        )
        .order_by(JobRow.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).first()
    if job is None:
        return None
    job.status = "running"
    job.locked_at = now
    job.attempts += 1
    session.flush()
    return job


def complete(session: Session, job_id: int) -> None:
    job = session.get(JobRow, job_id)
    if job is not None:
        job.status = "done"
        job.locked_at = None


def fail(session: Session, job_id: int, error: str) -> bool:
    """Record a failure. Returns True if the job will be retried (exponential backoff)."""
    job = session.get(JobRow, job_id)
    if job is None:
        return False
    job.last_error = error[:4000]
    job.locked_at = None
    if job.attempts < job.max_attempts:
        job.status = "queued"
        job.run_after = datetime.now(UTC) + timedelta(seconds=2**job.attempts)
        return True
    job.status = "failed"
    return False
