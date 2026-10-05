"""Subject search and overview."""

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from tether.db.models import (
    Customer,
    DeletionRequestRow,
    LineageNode,
    PolicyTagRow,
    SubjectArtifact,
    SubjectRestriction,
)
from tether.lineage.ids import NodeType


@dataclass(frozen=True)
class SubjectSummary:
    subject_id: str
    display_name: str | None
    email: str | None
    consent_status: str | None
    license: str | None
    retention_until: str | None
    restrictions: list[str]
    artifact_count: int
    deletion_request_id: str | None
    deletion_status: str | None


def search_subjects(
    session: Session, q: str | None = None, limit: int = 50
) -> list[SubjectSummary]:
    ids_stmt = (
        select(LineageNode.ref.label("sid"))
        .where(LineageNode.node_type == NodeType.SUBJECT.value)
        .union(select(PolicyTagRow.subject_id), select(DeletionRequestRow.subject_id))
    )
    ids = ids_stmt.subquery()
    stmt = (
        select(ids.c.sid, Customer.name, Customer.email)
        .outerjoin(Customer, Customer.id == ids.c.sid)
        .order_by(ids.c.sid)
        .limit(limit)
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(ids.c.sid.ilike(like), Customer.name.ilike(like), Customer.email.ilike(like))
        )
    rows = session.execute(stmt).all()
    return _summaries(session, [(r[0], r[1], r[2]) for r in rows])


def get_subject(session: Session, subject_id: str) -> SubjectSummary:
    c = session.get(Customer, subject_id)
    return _summaries(session, [(subject_id, c.name if c else None, c.email if c else None)])[0]


def _summaries(
    session: Session, rows: list[tuple[str, str | None, str | None]]
) -> list[SubjectSummary]:
    sids = [r[0] for r in rows]
    if not sids:
        return []
    policies = {
        p.subject_id: p
        for p in session.scalars(select(PolicyTagRow).where(PolicyTagRow.subject_id.in_(sids)))
    }
    restrictions: dict[str, list[str]] = {}
    for r in session.scalars(
        select(SubjectRestriction).where(SubjectRestriction.subject_id.in_(sids))
    ):
        restrictions.setdefault(r.subject_id, []).append(r.reason)
    counts = dict(
        session.execute(
            select(SubjectArtifact.subject_id, func.count())
            .join(LineageNode, LineageNode.id == SubjectArtifact.node_id)
            .where(
                SubjectArtifact.subject_id.in_(sids),
                SubjectArtifact.node_type != NodeType.SUBJECT.value,
                LineageNode.status == "active",
            )
            .group_by(SubjectArtifact.subject_id)
        ).all()
    )
    requests = {
        d.subject_id: d
        for d in session.scalars(
            select(DeletionRequestRow).where(DeletionRequestRow.subject_id.in_(sids))
        )
    }
    out = []
    for sid, name, email in rows:
        p = policies.get(sid)
        d = requests.get(sid)
        out.append(
            SubjectSummary(
                subject_id=sid,
                display_name=name,
                email=email,
                consent_status=p.consent_status if p else None,
                license=p.license if p else None,
                retention_until=p.retention_until.isoformat() if p and p.retention_until else None,
                restrictions=sorted(restrictions.get(sid, [])),
                artifact_count=int(counts.get(sid, 0)),
                deletion_request_id=d.id if d else None,
                deletion_status=d.status if d else None,
            )
        )
    return out
