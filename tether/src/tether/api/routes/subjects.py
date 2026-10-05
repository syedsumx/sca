"""Subject-centric endpoints: search, lineage, artifacts, consent, deletion requests."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from tether.api import presenters, schemas
from tether.api.deps import Principal, get_principal, get_session
from tether.deletion import service as deletion
from tether.lineage.query import artifacts_by_type, subject_graph
from tether.policy.consent import PolicyUpdate, update_policy
from tether.services.subjects import get_subject, search_subjects

router = APIRouter(prefix="/subjects", tags=["subjects"])
SessionDep = Annotated[Session, Depends(get_session)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]


@router.get("", response_model=list[schemas.SubjectOut])
def list_subjects(
    session: SessionDep,
    q: Annotated[str | None, Query(description="Match on id, name or email")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[schemas.SubjectOut]:
    return [schemas.SubjectOut.model_validate(s) for s in search_subjects(session, q, limit)]


@router.get("/{subject_id}", response_model=schemas.SubjectOut)
def subject(subject_id: str, session: SessionDep) -> schemas.SubjectOut:
    return schemas.SubjectOut.model_validate(get_subject(session, subject_id))


@router.get("/{subject_id}/lineage", response_model=schemas.LineageOut)
def lineage(subject_id: str, session: SessionDep) -> schemas.LineageOut:
    """The full graph of everything that touched this subject (including tombstones)."""
    graph = subject_graph(session, subject_id)
    if not graph.nodes:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no lineage for subject {subject_id}")
    return schemas.LineageOut(
        subject_id=subject_id,
        nodes=[presenters.node(n) for n in sorted(graph.nodes, key=lambda n: n.id)],
        edges=[presenters.edge(e) for e in graph.edges],
    )


@router.get("/{subject_id}/artifacts", response_model=schemas.ArtifactsOut)
def artifacts(subject_id: str, session: SessionDep) -> schemas.ArtifactsOut:
    """Flat list of every artifact carrying the subject, grouped by type."""
    grouped = artifacts_by_type(session, subject_id)
    return schemas.ArtifactsOut(
        subject_id=subject_id,
        counts={t: len(v) for t, v in sorted(grouped.items())},
        artifacts={t: [presenters.node(n) for n in v] for t, v in sorted(grouped.items())},
    )


@router.post("/{subject_id}/consent", response_model=schemas.PolicyOut)
def consent(
    subject_id: str, body: schemas.ConsentIn, session: SessionDep, _: PrincipalDep
) -> schemas.PolicyOut:
    """Update consent or policy tags. Revoking consent blocks retrieval immediately."""
    changes = PolicyUpdate.model_validate(body.model_dump(exclude_unset=True))
    result = update_policy(session, subject_id, changes)
    return schemas.PolicyOut(
        subject_id=subject_id,
        consent_status=result.tags.consent_status.value,
        license=result.tags.license,
        retention_until=result.tags.retention_until,
        restrictions=result.restrictions,
    )


@router.post(
    "/{subject_id}/deletion-requests",
    response_model=schemas.DeletionRequestOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"description": "Existing request returned (idempotent)"}},
)
def create_deletion_request(
    subject_id: str,
    session: SessionDep,
    response: Response,
    _: PrincipalDep,
    body: schemas.DeletionRequestIn | None = None,
) -> schemas.DeletionRequestOut:
    """Start a deletion. Retrieval is blocked before this returns.

    Re-submitting for the same subject is a no-op that returns the existing request
    (with its certificate once it is certified). A failed request is retried.
    """
    result = deletion.submit(session, subject_id, body.reason if body else None)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    cert = deletion.get_certificate(session, result.request.id)
    return presenters.deletion_request(result.request, cert, created=result.created)
