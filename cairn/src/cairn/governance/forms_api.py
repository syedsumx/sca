"""``/governance/forms``: compliance forms (manage, assign, progress) and the tasks people complete."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from cairn.common.security import Principal
from cairn.governance.forms import FormError, FormService, FormSpec

MANAGE = "MANAGE_GLOBAL_SETTINGS"


class SubmitRequest(BaseModel):
    answers: dict[str, Any] = Field(default_factory=dict)
    verify: bool = False


def build_router(forms: FormService, current_principal, authorize, can_view, subject_of) -> APIRouter:
    router = APIRouter(prefix="/governance/forms", tags=["forms"])

    def _wrap(fn):
        try:
            return fn()
        except FormError as e:
            raise HTTPException(e.status, str(e))

    def _groups(p: Principal) -> list[str]:
        try:
            return list(subject_of(p.urn).get("groups") or [])
        except Exception:
            return []

    # ---- people completing forms ---------------------------------------------------------
    @router.get("/tasks")
    def my_tasks(state: str = Query("PENDING", pattern="^(PENDING|COMPLETED|VERIFIED)$"),
                 p: Principal = Depends(current_principal)):
        check = can_view(p)
        return [t for t in forms.tasks_for(p.urn, _groups(p), state) if check(t["urn"])]

    @router.get("/status")
    def asset_status(urn: str = Query(pattern=r"^urn:li:"), p: Principal = Depends(current_principal)):
        if not can_view(p)(urn):
            raise HTTPException(404, "asset not found")
        return forms.asset_status(urn)

    @router.get("/{form_id}/tasks/{urn:path}")
    def get_task(form_id: str, urn: str, p: Principal = Depends(current_principal)):
        if not can_view(p)(urn):
            raise HTTPException(404, "asset not found")
        return _wrap(lambda: forms.task(form_id, urn))

    @router.post("/{form_id}/tasks/{urn:path}")
    def submit(form_id: str, urn: str, req: SubmitRequest, p: Principal = Depends(current_principal)):
        if not can_view(p)(urn):
            raise HTTPException(404, "asset not found")
        def may_act(u: str) -> bool:      # stewards with edit rights may complete it too
            return authorize(p, "EDIT_ENTITY", u, raise_on_deny=False)
        member_of = set(_groups(p))
        actor = p.urn

        def run():
            task = forms.task(form_id, urn)
            assigned_via_group = bool(member_of & set(task["assignees"]))
            return forms.submit(form_id, urn, req.answers, actor, req.verify,
                                may_act=(lambda u: True) if assigned_via_group else may_act)
        return _wrap(run)

    # ---- people managing forms -----------------------------------------------------------
    @router.get("")
    def list_forms(include_archived: bool = False, p: Principal = Depends(current_principal)):
        authorize(p, MANAGE, None)
        return forms.list(include_archived)

    @router.post("", status_code=201)
    def create(spec: FormSpec, p: Principal = Depends(current_principal)):
        authorize(p, MANAGE, None)
        return _wrap(lambda: forms.create(spec, p.urn))

    @router.get("/{form_id}")
    def get(form_id: str, p: Principal = Depends(current_principal)):
        authorize(p, MANAGE, None)
        return _wrap(lambda: forms.get(form_id))

    @router.put("/{form_id}")
    def put(form_id: str, spec: FormSpec, p: Principal = Depends(current_principal)):
        authorize(p, MANAGE, None)
        return _wrap(lambda: forms.update(form_id, spec, p.urn))

    @router.delete("/{form_id}", status_code=204)
    def archive(form_id: str, p: Principal = Depends(current_principal)):
        authorize(p, MANAGE, None)
        _wrap(lambda: forms.archive(form_id, p.urn))

    @router.post("/{form_id}/assign")
    def assign(form_id: str, dry_run: bool = False, p: Principal = Depends(current_principal)):
        authorize(p, MANAGE, None)
        return _wrap(lambda: forms.assign(form_id, p.urn, dry_run))

    @router.get("/{form_id}/progress")
    def progress(form_id: str, p: Principal = Depends(current_principal)):
        authorize(p, MANAGE, None)
        _wrap(lambda: forms.get(form_id))
        return forms.progress(form_id)

    return router

