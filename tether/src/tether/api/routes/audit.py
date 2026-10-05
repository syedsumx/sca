"""Append-only audit log."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from tether import audit
from tether.api import schemas
from tether.api.deps import get_session
from tether.db.models import AuditLogRow

router = APIRouter(prefix="/audit-log", tags=["audit"])


@router.get("", response_model=schemas.AuditLogOut)
def audit_log(
    session: Annotated[Session, Depends(get_session)],
    subject_id: str | None = None,
    event_type: Annotated[str | None, Query(description="Prefix match, e.g. 'deletion.'")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> schemas.AuditLogOut:
    stmt = select(AuditLogRow).order_by(AuditLogRow.id.desc()).limit(limit)
    if subject_id:
        stmt = stmt.where(AuditLogRow.subject_id == subject_id)
    if event_type:
        stmt = stmt.where(AuditLogRow.event_type.startswith(event_type))
    chain = audit.verify_chain(session)
    return schemas.AuditLogOut(
        entries=[
            schemas.AuditEntryOut.model_validate(r, from_attributes=True)
            for r in session.scalars(stmt)
        ],
        chain_ok=chain.ok,
        chain_entries=chain.entries,
        first_broken_id=chain.first_broken_id,
    )
