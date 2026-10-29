"""``/governance/privacy``: subject requests, retention policies and the record of processing.
Opening requests, policies and the record need ``MANAGE_POLICIES`` (a privacy officer); dataset owners
see and mark their own parts."""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from cairn.common.security import Principal
from cairn.governance.privacy import PrivacyError, PrivacyService, RequestIn, RetentionPolicy


class Mark(BaseModel):
    dataset: str
    state: Literal["DONE", "NOT_APPLICABLE"]
    note: str = Field(default="", max_length=500)


class Lookup(BaseModel):
    identifier_kind: str
    identifier: str = Field(min_length=1, max_length=300)


def build_router(svc: PrivacyService, current_principal, authorize) -> APIRouter:
    router = APIRouter(prefix="/governance/privacy", tags=["privacy"])

    def _officer(p: Principal) -> None:
        authorize(p, "MANAGE_POLICIES", None)

    def _wrap(fn):
        try:
            return fn()
        except PrivacyError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/identifiers")
    def identifiers(p: Principal = Depends(current_principal)):
        _officer(p)
        return {k: v for k, v in svc.identifiers.items()}

    @router.post("/requests", status_code=201)
    def open_request(body: RequestIn, p: Principal = Depends(current_principal)):
        """Find one person's data (access), and hand its deletion to the owners (deletion)."""
        _officer(p)
        return _wrap(lambda: svc.open(body, p.urn))

    @router.get("/requests")
    def list_requests(state: Optional[str] = None, p: Principal = Depends(current_principal)):
        _officer(p)
        return svc.list(state)

    @router.post("/requests/lookup")
    def lookup(body: Lookup, p: Principal = Depends(current_principal)):
        """Earlier requests for the same person."""
        _officer(p)
        return svc.find(body.identifier_kind, body.identifier)

    @router.get("/requests/{rid}")
    def get_request(rid: str, format: Optional[str] = None, p: Principal = Depends(current_principal)):
        _officer(p)
        if format == "csv":
            return PlainTextResponse(_wrap(lambda: svc.report_csv(rid)), media_type="text/csv",
                                     headers={"Content-Disposition": f'attachment; filename="{rid}.csv"'})
        return _wrap(lambda: svc.get(rid))

    @router.post("/requests/{rid}/parts")
    def mark(rid: str, body: Mark, p: Principal = Depends(current_principal)):
        """A dataset owner marks their part of a deletion done (or not applicable)."""
        officer = authorize(p, "MANAGE_POLICIES", None, raise_on_deny=False)
        return _wrap(lambda: svc.mark(rid, body.dataset, body.state, p.urn, body.note, bool(officer)))

    @router.get("/my-parts")
    def my_parts(p: Principal = Depends(current_principal)):
        """Deletions waiting on the caller, as a dataset owner."""
        return svc.my_parts(p.urn)

    @router.get("/retention")
    def retention(p: Principal = Depends(current_principal)):
        _officer(p)
        return {"policies": [x.model_dump() for x in svc.policies()], "datasets": svc.retention_report()}

    @router.put("/retention/{pid}")
    def put_retention(pid: str, body: RetentionPolicy, p: Principal = Depends(current_principal)):
        _officer(p)
        if body.id != pid:
            raise HTTPException(422, "the policy id must match the path")
        return svc.put_policy(body, p.urn)

    @router.delete("/retention/{pid}", status_code=204)
    def delete_retention(pid: str, p: Principal = Depends(current_principal)):
        _officer(p)
        svc.delete_policy(pid, p.urn)

    @router.post("/retention/check")
    def check_retention(p: Principal = Depends(current_principal)):
        _officer(p)
        return svc.check_retention(p.urn)

    @router.get("/ropa")
    def ropa(format: Optional[str] = None, p: Principal = Depends(current_principal)):
        """The record of processing activities."""
        _officer(p)
        if format == "csv":
            return PlainTextResponse(svc.ropa_csv(), media_type="text/csv",
                                     headers={"Content-Disposition": 'attachment; filename="record-of-processing.csv"'})
        return svc.ropa()

    return router
