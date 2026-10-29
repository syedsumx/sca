"""``/workspaces``: business units with delegated admins. Everyone sees the workspaces they belong to;
global admins (``MANAGE_POLICIES``) see and set all of them; a workspace's admins manage its members."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from cairn.common.security import Principal
from cairn.governance.workspaces import WorkspaceError, WorkspaceIn, WorkspaceService


def build_router(svc: WorkspaceService, current_principal, is_admin) -> APIRouter:
    router = APIRouter(prefix="/workspaces", tags=["workspaces"])

    def _wrap(fn):
        try:
            return fn()
        except WorkspaceError as e:
            raise HTTPException(e.status, str(e))

    def _visible(wid: str, p: Principal):
        if not is_admin(p.urn) and wid not in svc.roles(p.urn):
            raise HTTPException(404, "no such workspace")

    @router.get("")
    def list_workspaces(p: Principal = Depends(current_principal)):
        if is_admin(p.urn):
            roles = svc.roles(p.urn)
            return [{"id": w.id, "name": w.name, "domains": w.domains, "role": roles.get(w.id, "global_admin")}
                    for w in svc.list()]
        return svc.mine(p.urn)

    @router.get("/{wid}")
    def get_workspace(wid: str, p: Principal = Depends(current_principal)):
        _visible(wid, p)
        return _wrap(lambda: svc.summary(wid, p.urn))

    @router.get("/{wid}/assets")
    def assets(wid: str, type: str = "", start: int = 0, count: int = 50, p: Principal = Depends(current_principal)):
        _visible(wid, p)
        rows = _wrap(lambda: svc.assets(wid))
        if type:
            rows = [r for r in rows if r["type"] == type]
        return {"total": len(rows), "items": rows[max(0, start):max(0, start) + min(max(count, 1), 500)]}

    @router.put("/{wid}")
    def put_workspace(wid: str, body: WorkspaceIn, p: Principal = Depends(current_principal)):
        if body.id != wid:
            raise HTTPException(422, "the id in the body differs from the path")
        return _wrap(lambda: svc.put(body, p.urn, is_admin(p.urn)).model_dump())

    @router.delete("/{wid}", status_code=204)
    def delete_workspace(wid: str, p: Principal = Depends(current_principal)):
        if not is_admin(p.urn):
            raise HTTPException(403, "only a global admin removes a workspace")
        _wrap(lambda: svc.delete(wid, p.urn))

    return router
