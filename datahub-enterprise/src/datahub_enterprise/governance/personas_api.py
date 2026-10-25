"""``/governance/personas`` and ``/governance/purposes`` (Personas & Purposes admin)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from datahub_enterprise.common.security import Principal
from datahub_enterprise.governance.personas import (
    PERMISSION_BUNDLES,
    PersonaEngine,
    PersonaSpec,
    PurposeSpec,
    compile_persona,
    compile_purpose,
)

ADMIN_PRIVILEGE = "MANAGE_POLICIES"


class GrantRequest(BaseModel):
    actor: str = Field(pattern=r"^urn:li:corp(user|Group):")
    expires_at_ms: Optional[int] = None


def build_router(personas: PersonaEngine, current_principal, authorize, graph=None) -> APIRouter:
    router = APIRouter(prefix="/governance", tags=["personas"])

    def admin(p: Principal = Depends(current_principal)) -> Principal:
        authorize(p, ADMIN_PRIVILEGE, None)
        return p

    @router.get("/permission-bundles")
    @router.get("/personas/permission-bundles")
    def bundles(_p: Principal = Depends(current_principal)):
        return PERMISSION_BUNDLES

    # ---- personas -------------------------------------------------------------------
    @router.get("/personas")
    def list_personas(_p: Principal = Depends(admin)):
        return [x.model_dump(mode="json") for x in personas.list_personas()]

    @router.post("/personas", status_code=201)
    def create_persona(spec: PersonaSpec, p: Principal = Depends(admin)):
        return personas.upsert_persona(spec, p.urn).model_dump(mode="json")

    @router.get("/personas/{pid}")
    def get_persona(pid: str, _p: Principal = Depends(admin)):
        x = personas.get_persona(pid)
        if x is None:
            raise HTTPException(404, "persona not found")
        out = x.model_dump(mode="json")
        out["compiled_policies"] = [c.model_dump(mode="json") for c in compile_persona(x)]
        return out

    @router.put("/personas/{pid}")
    def put_persona(pid: str, spec: PersonaSpec, p: Principal = Depends(admin)):
        return personas.upsert_persona(spec, p.urn, pid).model_dump(mode="json")

    @router.delete("/personas/{pid}", status_code=204)
    def delete_persona(pid: str, p: Principal = Depends(admin)):
        if not personas.delete_persona(pid, p.urn):
            raise HTTPException(404, "persona not found")

    @router.api_route("/personas/{pid}/preview", methods=["GET", "POST"])
    def preview(pid: str, _p: Principal = Depends(admin)):
        try:
            return personas.preview(pid, graph)
        except KeyError:
            raise HTTPException(404, "persona not found")

    @router.post("/personas/{pid}/grants", status_code=201)
    def grant(pid: str, req: GrantRequest, p: Principal = Depends(admin)):
        if personas.get_persona(pid) is None:
            raise HTTPException(404, "persona not found")
        return {"grant_id": personas.grant(pid, req.actor, "manual", req.expires_at_ms, p.urn)}

    @router.delete("/personas/grants/{grant_id}", status_code=204)
    def revoke(grant_id: str, p: Principal = Depends(admin)):
        if not personas.revoke_grant(grant_id, p.urn):
            raise HTTPException(404, "grant not found")

    # ---- purposes -------------------------------------------------------------------
    @router.get("/purposes")
    def list_purposes(_p: Principal = Depends(admin)):
        return [x.model_dump(mode="json") for x in personas.list_purposes()]

    @router.post("/purposes", status_code=201)
    def create_purpose(spec: PurposeSpec, p: Principal = Depends(admin)):
        return personas.upsert_purpose(spec, p.urn).model_dump(mode="json")

    @router.get("/purposes/{pid}")
    def get_purpose(pid: str, _p: Principal = Depends(admin)):
        x = personas.get_purpose(pid)
        if x is None:
            raise HTTPException(404, "purpose not found")
        out = x.model_dump(mode="json")
        out["compiled_policies"] = [c.model_dump(mode="json") for c in compile_purpose(x)]
        return out

    @router.put("/purposes/{pid}")
    def put_purpose(pid: str, spec: PurposeSpec, p: Principal = Depends(admin)):
        return personas.upsert_purpose(spec, p.urn, pid).model_dump(mode="json")

    @router.delete("/purposes/{pid}", status_code=204)
    def delete_purpose(pid: str, p: Principal = Depends(admin)):
        if not personas.delete_purpose(pid, p.urn):
            raise HTTPException(404, "purpose not found")

    @router.get("/me/personas")
    def my_personas(p: Principal = Depends(current_principal)):
        import time

        subject = personas.attributes.subject(p.urn) if personas.attributes else {"urn": p.urn}
        ids = personas.memberships(subject, int(time.time() * 1000))
        return [{"id": i, "name": (personas.get_persona(i).name if personas.get_persona(i) else i)} for i in ids]

    return router
