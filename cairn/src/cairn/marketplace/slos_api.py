"""``/marketplace/slos``: data product SLOs. Anyone who can see a product sees its SLOs; its owners (or
an administrator) set them."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from cairn.common.security import Principal
from cairn.marketplace.slos import SLOError, SLOIn, SLOService


def build_router(svc: SLOService, marketplace, current_principal, can_view) -> APIRouter:
    router = APIRouter(prefix="/marketplace/slos", tags=["marketplace"])

    def _owner(p: Principal, product_urn: str) -> None:
        from cairn.marketplace.service import MarketplaceError

        try:
            marketplace._require_owner_or_admin(p.urn, product_urn)
        except MarketplaceError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    def list_(product: Optional[str] = None, p: Principal = Depends(current_principal)):
        """Every SLO the caller may see, worst first, or one product's."""
        check = can_view(p)
        rows = [svc.status(s) for s in svc.definitions(product)] if product else svc.dashboard()
        return [r for r in rows if check(r["product_urn"])]

    @router.put("/{sid}")
    def put(sid: str, body: SLOIn, p: Principal = Depends(current_principal)):
        if body.id != sid:
            raise HTTPException(422, "the SLO id must match the path")
        _owner(p, body.product_urn)
        return svc.put(body, p.urn)

    @router.delete("/{sid}", status_code=204)
    def delete(sid: str, p: Principal = Depends(current_principal)):
        try:
            slo = svc.get(sid)
        except SLOError as e:
            raise HTTPException(e.status, str(e))
        _owner(p, slo.product_urn)
        svc.delete(sid, p.urn)

    return router
