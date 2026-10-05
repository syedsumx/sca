"""Retrieval-time policy enforcement.

``subject_restrictions`` is the live block list. A subject lands on it as soon as consent
is revoked or a deletion request arrives, before any purge has run. Every read path
(RAG retrieval and agent memory) filters through it with a single SQL predicate, so the
block takes effect immediately.
"""

from enum import StrEnum

from sqlalchemy import (
    ColumnElement,
    Date,
    and_,
    cast,
    delete,
    func,
    literal_column,
    not_,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import InstrumentedAttribute, Session

from tether.db.models import SubjectRestriction


class RestrictionReason(StrEnum):
    CONSENT_REVOKED = "consent_revoked"
    DELETION_PENDING = "deletion_pending"
    DELETED = "deleted"


def restrict(
    session: Session, subject_id: str, reason: RestrictionReason, detail: str = ""
) -> None:
    stmt = (
        insert(SubjectRestriction)
        .values(subject_id=subject_id, reason=reason.value, detail=detail or None)
        .on_conflict_do_nothing()
    )
    session.execute(stmt)


def unrestrict(session: Session, subject_id: str, reason: RestrictionReason) -> None:
    session.execute(
        delete(SubjectRestriction).where(
            SubjectRestriction.subject_id == subject_id,
            SubjectRestriction.reason == reason.value,
        )
    )


def restricted_subject_ids(session: Session) -> set[str]:
    return set(session.scalars(select(SubjectRestriction.subject_id).distinct()))


def is_restricted(session: Session, subject_id: str) -> bool:
    stmt = select(SubjectRestriction.subject_id).where(SubjectRestriction.subject_id == subject_id)
    return session.scalars(stmt.limit(1)).first() is not None


def restrictions_for(session: Session, subject_id: str) -> list[str]:
    stmt = select(SubjectRestriction.reason).where(SubjectRestriction.subject_id == subject_id)
    return sorted(session.scalars(stmt))


def allowed_predicate(
    subject_ids_col: InstrumentedAttribute[list[str]],
    policy_col: InstrumentedAttribute[dict[str, object]] | None = None,
) -> ColumnElement[bool]:
    """SQL predicate that is true when a row may be served.

    A row is hidden when any of its subjects is restricted. When ``policy_col`` is given,
    the row is also hidden if its own policy says consent is revoked or retention has
    expired. The block list is read inside the same statement, so no stale cache exists.
    """
    blocked = select(
        func.coalesce(
            func.array_agg(SubjectRestriction.subject_id), literal_column("'{}'::varchar[]")
        )
    )
    conditions: list[ColumnElement[bool]] = [
        not_(subject_ids_col.overlap(blocked.scalar_subquery()))
    ]
    if policy_col is not None:
        consent = policy_col["consent_status"].astext
        retention = policy_col["retention_until"].astext
        conditions.append(or_(consent.is_(None), consent != "revoked"))
        conditions.append(or_(retention.is_(None), cast(retention, Date) >= func.current_date()))
    return and_(*conditions)
