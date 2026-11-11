"""``/catalog/products``: data products in Cairn Core, listed, made, changed and removed through Cairn."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import ValidationError

from cairn.common.security import Principal
from cairn.marketplace.products import PRIVILEGE, DataProducts, ProductError, ProductIn, product_urn


def build_router(products: DataProducts, current_principal, authorize, can_view) -> APIRouter:
    router = APIRouter(prefix="/catalog/products", tags=["catalog"])

    def _wrap(fn):
        try:
            return fn()
        except ProductError as e:
            raise HTTPException(e.status, str(e))

    def _may_change(p: Principal, domain: Optional[str], urn: str) -> None:
        # Cairn Core's own rule: data products are managed on their domain.
        authorize(p, PRIVILEGE, domain or urn)

    def _spec(body: dict, pid: Optional[str] = None) -> ProductIn:
        try:
            return ProductIn.model_validate({**body, "id": pid} if pid is not None else body)
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False))

    def _assets_visible(p: Principal, spec: ProductIn) -> None:
        check = can_view(p)
        hidden = [a for a in spec.assets if not check(a)]
        if hidden:
            raise HTTPException(403, f"you cannot see {len(hidden)} of the assets: {', '.join(hidden[:5])}")

    @router.get("")
    def list_products(p: Principal = Depends(current_principal)):
        check = can_view(p)
        return {"products": [x for x in _wrap(products.list) if check(x["urn"])]}

    @router.get("/{pid}")
    def get_product(pid: str, p: Principal = Depends(current_principal)):
        if not can_view(p)(product_urn(pid)):
            raise HTTPException(404, f"no data product {pid}")
        return _wrap(lambda: products.get(pid))

    @router.post("", status_code=201)
    def create(body: dict = Body(...), p: Principal = Depends(current_principal)):
        spec = _spec(body)
        _may_change(p, spec.domain, product_urn(spec.id))
        _assets_visible(p, spec)
        return _wrap(lambda: products.save(p.urn, spec, create=True))

    @router.put("/{pid}")
    def update(pid: str, body: dict = Body(...), p: Principal = Depends(current_principal)):
        spec = _spec(body, pid)
        current = _wrap(lambda: products.get(pid))
        _may_change(p, current["domain"], current["urn"])
        if spec.domain != current["domain"]:
            _may_change(p, spec.domain, current["urn"])
        _assets_visible(p, spec)
        return _wrap(lambda: products.save(p.urn, spec, create=False))

    @router.delete("/{pid}", status_code=204)
    def delete(pid: str, p: Principal = Depends(current_principal)):
        current = _wrap(lambda: products.get(pid))
        _may_change(p, current["domain"], current["urn"])
        _wrap(lambda: products.delete(p.urn, pid))

    return router


__all__ = ["build_router"]
