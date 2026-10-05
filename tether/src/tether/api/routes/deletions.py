"""Deletion request status and certificates."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from tether.api import presenters, schemas
from tether.api.deps import get_session
from tether.db.models import DeletionRequestRow
from tether.deletion import service as deletion
from tether.deletion.certificate import render

router = APIRouter(prefix="/deletion-requests", tags=["deletion"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("", response_model=list[schemas.DeletionRequestOut])
def list_requests(
    session: SessionDep, limit: Annotated[int, Query(ge=1, le=500)] = 50
) -> list[schemas.DeletionRequestOut]:
    rows = session.scalars(
        select(DeletionRequestRow).order_by(DeletionRequestRow.created_at.desc()).limit(limit)
    )
    return [presenters.deletion_request(r, deletion.get_certificate(session, r.id)) for r in rows]


@router.get("/{request_id}", response_model=schemas.DeletionRequestOut)
def get_request(request_id: str, session: SessionDep) -> schemas.DeletionRequestOut:
    req = deletion.get_request(session, request_id)
    return presenters.deletion_request(req, deletion.get_certificate(session, request_id))


@router.post("/{request_id}/retry", response_model=schemas.DeletionRequestOut)
def retry(request_id: str, session: SessionDep) -> schemas.DeletionRequestOut:
    req = deletion.retry(session, request_id)
    return presenters.deletion_request(req, deletion.get_certificate(session, request_id))


@router.get(
    "/{request_id}/certificate",
    response_model=schemas.CertificateOut,
    responses={409: {"description": "Not certified yet"}},
)
def certificate(
    request_id: str,
    session: SessionDep,
    download: Annotated[bool, Query(description="Return a downloadable JSON file")] = False,
) -> schemas.CertificateOut | Response:
    deletion.get_request(session, request_id)  # 404 if the request is unknown
    cert = deletion.get_certificate(session, request_id)
    if cert is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "deletion request is not certified yet")
    if download:
        return Response(
            content=render(cert.content, cert.sha256),
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="deletion-certificate-{cert.subject_id}.json"'
                )
            },
        )
    return presenters.certificate(cert)
