"""Convert domain rows into API schemas."""

from tether.api import schemas
from tether.db.models import (
    DeletionCertificateRow,
    DeletionRequestRow,
    LineageEdge,
    LineageNode,
    ModelVersionRow,
)
from tether.deletion.certificate import verify_certificate


def node(n: LineageNode) -> schemas.NodeOut:
    return schemas.NodeOut(
        id=n.id,
        type=n.node_type,
        ref=n.ref,
        status=n.status,
        subject_ids=list(n.subject_ids),
        policy=dict(n.policy),
        attrs=dict(n.attrs),
    )


def edge(e: LineageEdge) -> schemas.EdgeOut:
    return schemas.EdgeOut(source=e.src_id, target=e.dst_id, type=e.edge_type)


def certificate(c: DeletionCertificateRow) -> schemas.CertificateOut:
    return schemas.CertificateOut(
        certificate_id=c.id,
        request_id=c.request_id,
        subject_id=c.subject_id,
        issued_at=c.issued_at,
        sha256=c.sha256,
        verified=verify_certificate(c.content, c.sha256),
        content=c.content,
    )


def deletion_request(
    r: DeletionRequestRow, cert: DeletionCertificateRow | None, created: bool = False
) -> schemas.DeletionRequestOut:
    return schemas.DeletionRequestOut(
        id=r.id,
        subject_id=r.subject_id,
        status=r.status,
        reason=r.reason,
        created=created,
        error=r.error,
        history=r.history,
        inventory=r.inventory,
        purged=r.purged,
        flagged=r.flagged,
        attempts=r.attempts,
        created_at=r.created_at,
        updated_at=r.updated_at,
        completed_at=r.completed_at,
        certificate=certificate(cert) if cert else None,
    )


def model(m: ModelVersionRow) -> schemas.ModelOut:
    return schemas.ModelOut(
        id=m.id,
        name=m.name,
        version=m.version,
        trained_on=list(m.trained_on),
        status=m.status,
        needs_retraining=m.status == "needs_retraining",
        retraining_reasons=[
            schemas.RetrainingReason.model_validate(r) for r in m.retraining_reasons
        ],
        created_at=m.created_at,
    )
