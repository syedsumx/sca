"""``/catalog/products``: data products in Cairn Core, listed, made, changed and removed through Cairn."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import ValidationError

from cairn.common.security import Principal
from cairn.marketplace.products import NEW_PRODUCT, PRIVILEGE, DataProducts, ProductError, ProductIn, product_urn


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

    def _may(p: Principal, domain: Optional[str], urn: str) -> bool:
        """The answer the writes would get, asked the same way: a refusal is ``False``; anything else that goes
        wrong is an error (never a yes)."""
        try:
            _may_change(p, domain, urn)
        except HTTPException as e:
            if e.status_code == 403:
                return False
            raise
        return True

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

    @router.get("/rights")           # before /{pid}, which would take "rights" for a product's id
    def rights(urn: Optional[str] = Query(None), domain: Optional[str] = Query(None),
               p: Principal = Depends(current_principal)):
        """What the caller may do, by the check the writes make. With ``urn``: ``may_manage`` that product (change
        and remove it). Without: ``may_create``, in ``domain`` when one is named, else in at least one domain they can
        see or with no domain at all (``domains``: where)."""
        why = f"{PRIVILEGE} is needed on the product's domain (on the product itself when it has none)"
        if urn:
            if not urn.startswith("urn:li:dataProduct:"):
                raise HTTPException(422, f"{urn}: a data product's URN")
            if not can_view(p)(urn):                    # one they cannot see is not there, for them
                raise HTTPException(404, f"no data product {urn}")
            current = _wrap(lambda: products.get(urn.split(":", 3)[3]))
            ok = _may(p, current["domain"], current["urn"])
            return {"may_manage": ok, "may_create": False, "privilege": PRIVILEGE, "reason": "" if ok else why}
        check = can_view(p)
        if domain:
            domain = domain if domain.startswith("urn:li:domain:") else f"urn:li:domain:{domain}"
            where = [domain] if check(domain) and _may(p, domain, domain) else []
            anywhere = False
        else:
            # A product with no domain is checked on its own URN: one that names no product stands for a new one.
            anywhere = _may(p, None, product_urn(NEW_PRODUCT))
            where = sorted(d for d in _wrap(products.domains) if check(d) and _may(p, d, d))
        ok = anywhere or bool(where)
        return {"may_manage": False, "may_create": ok, "domains": where, "without_domain": anywhere,
                "privilege": PRIVILEGE,
                "reason": "" if ok else f"{PRIVILEGE} is needed on a domain (or on data products) to make one"}

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
