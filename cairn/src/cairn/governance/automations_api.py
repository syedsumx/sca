"""``/governance/automations``: define, preview, run and undo no-code automations."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError

from cairn.common.security import Principal
from cairn.governance.automations import AutomationError, AutomationService, AutomationSpec

MANAGE = "MANAGE_GLOBAL_SETTINGS"


class EnabledIn(BaseModel):
    enabled: bool


def build_router(svc: AutomationService, current_principal, authorize) -> APIRouter:
    router = APIRouter(prefix="/governance/automations", tags=["automations"])

    def _wrap(fn):
        try:
            return fn()
        except AutomationError as e:
            raise HTTPException(e.status, str(e))

    def _spec(body: dict) -> AutomationSpec:
        try:
            return AutomationSpec.model_validate(body)
        except (ValidationError, ValueError) as e:
            raise HTTPException(422, str(e))

    def _admin(p: Principal) -> None:
        authorize(p, MANAGE, None)

    @router.get("")
    def list_all(p: Principal = Depends(current_principal)):
        _admin(p)
        return svc.list()

    @router.post("/preview")
    def preview(body: dict, p: Principal = Depends(current_principal)):
        """Which datasets match, and what a run would write. Changes nothing."""
        _admin(p)
        return svc.preview(_spec(body))

    @router.post("")
    def create(body: dict, enabled: bool = True, p: Principal = Depends(current_principal)):
        _admin(p)
        return svc.create(_spec(body), p.urn, enabled)

    @router.put("/{aid}")
    def put(aid: str, body: dict, enabled: Optional[bool] = None, p: Principal = Depends(current_principal)):
        _admin(p)
        return _wrap(lambda: svc.update(aid, _spec(body), p.urn, enabled))

    @router.post("/{aid}/enabled")
    def set_enabled(aid: str, body: EnabledIn, p: Principal = Depends(current_principal)):
        _admin(p)
        return _wrap(lambda: svc.set_enabled(aid, body.enabled, p.urn))

    @router.delete("/{aid}")
    def remove(aid: str, p: Principal = Depends(current_principal)):
        _admin(p)
        _wrap(lambda: svc.delete(aid, p.urn))
        return {"deleted": aid}

    @router.post("/{aid}/run")
    def run(aid: str, p: Principal = Depends(current_principal)):
        _admin(p)
        return _wrap(lambda: svc.run(aid, "manual"))

    @router.get("/{aid}/runs")
    def runs(aid: str, p: Principal = Depends(current_principal)):
        _admin(p)
        _wrap(lambda: svc.get(aid))
        return svc.runs(aid)

    @router.post("/runs/{rid}/undo")
    def undo(rid: str, p: Principal = Depends(current_principal)):
        _admin(p)
        return _wrap(lambda: svc.undo(rid, p.urn))

    return router
