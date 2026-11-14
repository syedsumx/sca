"""``/catalog/products``: data products in Cairn Core, listed, made, changed and removed through Cairn."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import ValidationError

from cairn.common.core import CoreError
from cairn.common.security import Principal
from cairn.marketplace.products import (ASSET_PRIVILEGE, FIELDS, NEW_PRODUCT, PRIVILEGE, DataProducts, ProductError,
                                        ProductIn, product_urn)


def build_router(products: DataProducts, current_principal, authorize, can_view) -> APIRouter:
    router = APIRouter(prefix="/catalog/products", tags=["catalog"])

    def _wrap(fn):
        try:
            return fn()
        except ProductError as e:
            raise HTTPException(e.status, str(e))
        except CoreError as e:
            raise HTTPException(502, f"Cairn Core did not take the change or could not be read: {str(e)[:300]}")

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

    def _spec(body: dict) -> ProductIn:
        try:
            return ProductIn.model_validate(body)
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False))

    def _change(pid: str, body: dict, current: dict) -> tuple[ProductIn, set]:
        """A PUT's body over the product as it is: a field left out keeps its value. The product is named by the
        path, whatever its id (one made in Cairn Core's UI may have an id Cairn would not make)."""
        given = {k for k in FIELDS if k in body}
        merged = {**{k: current[k] for k in FIELDS}, **{k: v for k, v in body.items() if k != "id"}}
        for k in ("owners", "assets", "output_ports"):
            if merged.get(k) is None:
                merged[k] = []                          # an explicit null clears
        if "assets" in given and "output_ports" not in given:
            merged["output_ports"] = [x for x in current["output_ports"] if x in (merged["assets"] or [])]
        spec = _spec({**merged, "id": "x"})
        return spec.model_copy(update={"id": pid}), given

    def _assets_allowed(p: Principal, assets: list[str]) -> None:
        """Each asset put in or taken out: seen by the caller, and ``EDIT_ENTITY_DATA_PRODUCTS`` on it (Cairn Core's
        rule for changing an asset's data products). A decision that cannot be had is an error, never a yes."""
        check = can_view(p)
        hidden = [a for a in assets if not check(a)]
        if hidden:
            raise HTTPException(403, f"you cannot see {len(hidden)} of the assets: {', '.join(hidden[:5])}")
        refused = [a for a in assets if not authorize(p, ASSET_PRIVILEGE, a, raise_on_deny=False)]
        if refused:
            raise HTTPException(403, f"{ASSET_PRIVILEGE} is needed on each asset put in or taken out of a data "
                                     f"product; missing on {len(refused)}: {', '.join(refused[:5])}")

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
        # Made again after it was removed: the domain it was in has a say too (its steward removed it).
        old = _wrap(lambda: products.removed(spec.id))
        if old is not None and old["domain"] != spec.domain:
            _may_change(p, old["domain"], old["urn"])
        _assets_allowed(p, spec.assets)
        return _wrap(lambda: products.save(p.urn, spec, create=True))

    @router.put("/{pid}")
    def update(pid: str, body: dict = Body(...), p: Principal = Depends(current_principal)):
        current = _wrap(lambda: products.get(pid))
        spec, given = _change(pid, body, current)
        _may_change(p, current["domain"], current["urn"])
        if spec.domain != current["domain"]:
            _may_change(p, spec.domain, current["urn"])
        _assets_allowed(p, sorted(set(spec.assets) ^ set(current["assets"])))
        return _wrap(lambda: products.save(p.urn, spec, create=False, given=given))

    @router.delete("/{pid}", status_code=204)
    def delete(pid: str, p: Principal = Depends(current_principal)):
        current = _wrap(lambda: products.get(pid))
        _may_change(p, current["domain"], current["urn"])
        _wrap(lambda: products.delete(p.urn, pid))

    return router


__all__ = ["build_router"]
