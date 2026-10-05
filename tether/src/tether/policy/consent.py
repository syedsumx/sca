"""Consent and policy-tag updates for a subject.

Revoking consent puts the subject on the live block list immediately. Granting it again
lifts that particular restriction, but a deletion restriction always stays.
"""

from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from tether import audit
from tether.db.models import LineageNode, PolicyTagRow
from tether.lineage.ids import NodeType, node_id
from tether.policy.enforcement import RestrictionReason, restrict, restrictions_for, unrestrict
from tether.policy.tags import ConsentStatus, PolicyTags


class PolicyUpdate(BaseModel):
    """Partial update. Only the fields that are explicitly set change; ``None`` clears
    ``license`` or ``retention_until``."""

    consent_status: ConsentStatus | None = None
    license: str | None = None
    retention_until: date | None = None


@dataclass(frozen=True)
class SubjectPolicy:
    subject_id: str
    tags: PolicyTags
    restrictions: list[str]


def get_policy(session: Session, subject_id: str) -> SubjectPolicy | None:
    row = session.get(PolicyTagRow, subject_id)
    if row is None:
        return None
    return SubjectPolicy(subject_id, _tags(row), restrictions_for(session, subject_id))


def _tags(row: PolicyTagRow) -> PolicyTags:
    return PolicyTags(
        consent_status=ConsentStatus(row.consent_status),
        license=row.license,
        retention_until=row.retention_until,
    )


def update_policy(session: Session, subject_id: str, changes: PolicyUpdate) -> SubjectPolicy:
    row = session.get(PolicyTagRow, subject_id)
    if row is None:
        row = PolicyTagRow(subject_id=subject_id, consent_status=ConsentStatus.PENDING.value)
        session.add(row)
    before = _tags(row).to_json()
    provided = changes.model_fields_set
    if "consent_status" in provided and changes.consent_status is not None:
        row.consent_status = changes.consent_status.value
    if "license" in provided:
        row.license = changes.license
    if "retention_until" in provided:
        row.retention_until = changes.retention_until
    session.flush()
    tags = _tags(row)

    if tags.consent_status == ConsentStatus.REVOKED:
        restrict(session, subject_id, RestrictionReason.CONSENT_REVOKED, "consent revoked")
    else:
        unrestrict(session, subject_id, RestrictionReason.CONSENT_REVOKED)
    session.execute(
        update(LineageNode)
        .where(LineageNode.id == node_id(NodeType.SUBJECT, subject_id))
        .values(policy=tags.to_json())
    )
    audit.append(
        session,
        "subject.policy_updated",
        {"before": before, "after": tags.to_json()},
        subject_id=subject_id,
    )
    return SubjectPolicy(subject_id, tags, restrictions_for(session, subject_id))


def sync_consent_restrictions(session: Session) -> int:
    """Make the block list match ``policy_tags`` (used after seeding or bulk imports)."""
    revoked = session.scalars(
        select(PolicyTagRow.subject_id).where(
            PolicyTagRow.consent_status == ConsentStatus.REVOKED.value
        )
    ).all()
    for sid in revoked:
        restrict(session, sid, RestrictionReason.CONSENT_REVOKED, "consent revoked at source")
    return len(revoked)
