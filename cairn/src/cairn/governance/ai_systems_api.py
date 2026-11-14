"""``/governance/ai-systems``: the AI system inventory, its risk assessments and approvals."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from cairn.common.security import Principal
from cairn.governance.ai_systems import AISystemError, AISystemIn, AISystemService


class Decision(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=2000)


def build_router(svc: AISystemService, current_principal, authorize) -> APIRouter:
    router = APIRouter(prefix="/governance/ai-systems", tags=["ai-governance"])

    def _wrap(fn):
        try:
            return fn()
        except AISystemError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/assessment")
    def assessment(p: Principal = Depends(current_principal)):
        return svc.template.model_dump()

    @router.get("/report")
    def report(format: Optional[str] = None, p: Principal = Depends(current_principal)):
        authorize(p, "MANAGE_POLICIES", None)
        if format == "csv":
            return PlainTextResponse(svc.report_csv(), media_type="text/csv",
                                     headers={"Content-Disposition": 'attachment; filename="ai-systems.csv"'})
        return svc.report()

    @router.get("")
    def list_(state: Optional[str] = None, p: Principal = Depends(current_principal)):
        return svc.list(state)

    @router.post("", status_code=201)
    def create(body: AISystemIn, p: Principal = Depends(current_principal)):
        return _wrap(lambda: svc.create(body, p.urn))

    @router.get("/{sid}")
    def get(sid: str, p: Principal = Depends(current_principal)):
        return _wrap(lambda: svc.get(sid))

    @router.put("/{sid}")
    def update(sid: str, body: AISystemIn, p: Principal = Depends(current_principal)):
        return _wrap(lambda: svc.update(sid, body, p.urn))

    @router.post("/{sid}/submit")
    def submit(sid: str, p: Principal = Depends(current_principal)):
        return _wrap(lambda: svc.submit(sid, p.urn))

    @router.post("/{sid}/decision")
    def decide(sid: str, body: Decision, p: Principal = Depends(current_principal)):
        return _wrap(lambda: svc.decide(sid, body.approve, p.urn, body.note))

    @router.post("/{sid}/retire")
    def retire(sid: str, p: Principal = Depends(current_principal)):
        return _wrap(lambda: svc.retire(sid, p.urn))

    return router
