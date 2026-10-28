"""``/marketplace/products/<urn>/publish/<target>`` (owners and admins) and ``/marketplace/publications``."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from cairn.common.security import Principal
from cairn.marketplace.publish import ProductPublisher, PublishError, PublishOptions
from cairn.marketplace.service import MarketplaceError, MarketplaceService


class PublishIn(BaseModel):
    options: PublishOptions
    connection: Optional[str] = None       # run the statements there; empty = record a publication done by hand


def build_router(publisher: ProductPublisher, marketplace: MarketplaceService, current_principal,
                 authorize) -> APIRouter:
    router = APIRouter(prefix="/marketplace", tags=["marketplace"])

    def _owner(p: Principal, urn: str) -> None:
        if p.via == "extension":
            raise HTTPException(403, "extensions cannot publish data products")
        try:
            marketplace._require_owner_or_admin(p.urn, urn)
        except MarketplaceError as e:
            raise HTTPException(403, "only the product's owners or admins can publish it") from e

    def _wrap(fn):
        try:
            return fn()
        except PublishError as e:
            raise HTTPException(e.status, str(e))

    @router.post("/products/{urn:path}/publish/{target}/plan")
    def plan(urn: str, target: str, options: PublishOptions, p: Principal = Depends(current_principal)):
        _owner(p, urn)
        return _wrap(lambda: publisher.plan(urn, target, options))

    @router.post("/products/{urn:path}/publish/{target}")
    def publish(urn: str, target: str, body: PublishIn, p: Principal = Depends(current_principal)):
        _owner(p, urn)
        return _wrap(lambda: publisher.publish(urn, target, body.options, p.urn, body.connection))

    @router.get("/publications")
    def list_(product: Optional[str] = None, p: Principal = Depends(current_principal)):
        if product:
            _owner(p, product)
        else:
            authorize(p, "MANAGE_GLOBAL_SETTINGS", None)
        return publisher.publications(product)

    return router
