"""``/history``: an asset's metadata changes, their diffs, and reverting one. Reading needs view on the
asset; reverting needs the privilege for that kind of edit (``EDIT_ENTITY_TAGS`` for tags, ...);
the catalog-wide feed of recent changes is for admins."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from cairn.common.security import Principal
from cairn.governance.history import PRIVILEGES, ChangeHistory, HistoryError


class Revert(BaseModel):
    force: bool = False


def build_router(svc: ChangeHistory, current_principal, can_view, authorize, is_admin) -> APIRouter:
    router = APIRouter(prefix="/history", tags=["history"])

    def _wrap(fn):
        try:
            return fn()
        except HistoryError as e:
            raise HTTPException(e.status, str(e))

    def _view(p: Principal, urn: str) -> None:
        if not can_view(p)(urn):
            raise HTTPException(404, "not found")

    @router.get("")
    def for_asset(urn: str, aspect: Optional[str] = None, limit: int = 100, p: Principal = Depends(current_principal)):
        _view(p, urn)
        return svc.list(urn, aspect, limit)

    @router.get("/recent")
    def recent(actor: Optional[str] = None, since_ms: Optional[int] = None, limit: int = 100,
               p: Principal = Depends(current_principal)):
        if not is_admin(p.urn):
            raise HTTPException(403, "the catalog-wide change feed is for admins")
        return svc.recent(actor, since_ms, limit)

    @router.get("/{cid}")
    def get_change(cid: str, p: Principal = Depends(current_principal)):
        change = _wrap(lambda: svc.get(cid))
        _view(p, change["urn"])
        return change

    @router.post("/{cid}/revert")
    def revert(cid: str, body: Optional[Revert] = None, p: Principal = Depends(current_principal)):
        change = _wrap(lambda: svc.get(cid))
        _view(p, change["urn"])
        authorize(p, PRIVILEGES.get(change["aspect"], "MANAGE_POLICIES"), change["urn"])
        return _wrap(lambda: svc.revert(cid, p.urn, force=bool(body and body.force)))

    return router
