"""``/marketplace`` router."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from datahub_enterprise.common.security import Principal
from datahub_enterprise.marketplace.service import AccessRequestIn, MarketplaceError, MarketplaceService, RequestPolicy


class ActionIn(BaseModel):
    comment: Optional[str] = None


def build_router(svc: MarketplaceService, current_principal, can_view, subject_of) -> APIRouter:
    """``subject_of(principal)`` → resolved subject (groups, personas) for auto-approval checks."""
    router = APIRouter(prefix="/marketplace", tags=["marketplace"])

    def _wrap(fn):
        try:
            return fn()
        except MarketplaceError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/products")
    def products(q: str = "*", domain: Optional[str] = None, certified: bool = False,
                 access: Optional[str] = Query(None, pattern="^(mine|requestable)$"),
                 sort: str = Query("relevance", pattern="^(relevance|consumers|name)$"),
                 p: Principal = Depends(current_principal)):
        return svc.list_products(p.urn, q, domain, certified, access, sort, can_view=can_view(p))

    @router.get("/products/{urn:path}/policy", response_model=RequestPolicy)
    def get_policy(urn: str, p: Principal = Depends(current_principal)):
        return svc.get_policy(urn)

    @router.put("/products/{urn:path}/policy", response_model=RequestPolicy)
    def put_policy(urn: str, policy: RequestPolicy, p: Principal = Depends(current_principal)):
        return _wrap(lambda: svc.put_policy(urn, policy, p.urn))

    @router.post("/products/{urn:path}/subscription")
    def subscribe(urn: str, p: Principal = Depends(current_principal)):
        if not can_view(p)(urn):
            raise HTTPException(403, "cannot view this product")
        return svc.subscribe(p.urn, urn)

    @router.delete("/products/{urn:path}/subscription")
    def unsubscribe(urn: str, p: Principal = Depends(current_principal)):
        return svc.unsubscribe(p.urn, urn)

    @router.get("/products/{urn:path}")
    def product(urn: str, p: Principal = Depends(current_principal)):
        if not can_view(p)(urn):
            raise HTTPException(403, "cannot view this product")
        return _wrap(lambda: svc.get_product(p.urn, urn))

    @router.post("/access-requests/preview")
    def preview(req: AccessRequestIn, p: Principal = Depends(current_principal)):
        return svc.preview(p.urn, req, subject_of(p))

    @router.post("/access-requests", status_code=201)
    def create(req: AccessRequestIn, p: Principal = Depends(current_principal)):
        if not can_view(p)(req.product_urn):
            raise HTTPException(403, "cannot view this product")
        return _wrap(lambda: svc.create_request(p.urn, req, subject_of(p)))

    @router.get("/access-requests")
    def list_requests(scope: str = Query("mine", pattern="^(mine|inbox|all)$"), state: Optional[str] = None,
                      p: Principal = Depends(current_principal)):
        if scope == "all" and not svc.is_admin(p.urn):
            raise HTTPException(403, "admins only")
        return svc.list_requests(p.urn, scope, state, subject_of(p).get("groups"))

    @router.get("/access-requests/{rid}")
    def get_request(rid: str, p: Principal = Depends(current_principal)):
        r = _wrap(lambda: svc.get_request(rid))
        groups = subject_of(p).get("groups") or []
        if p.urn != r["requester"] and not svc._is_approver(p.urn, r, groups):
            raise HTTPException(403, "not your request")
        return r

    @router.post("/access-requests/{rid}/{action}")
    def act(rid: str, action: str, body: Optional[ActionIn] = None, p: Principal = Depends(current_principal)):
        return _wrap(lambda: svc.act(rid, action, p.urn, body.comment if body else None,
                                     subject_of(p).get("groups")))

    return router
