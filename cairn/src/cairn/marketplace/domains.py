"""Domains edited through Cairn (``/catalog/domains``), written to Cairn Core.

A domain is a Cairn Core ``domain`` entity: its properties (name, description, the domain it is in) and owners are
aspects in the metadata graph, and an asset's domain is the asset's ``domains`` aspect. Cairn keeps no copy.

* **Who may change a domain:** ``MANAGE_DOMAINS`` (Cairn Core's privilege name) on the domain, or on a domain above it;
  a policy without resources covers every domain. Making a top-level domain needs it without a resource; moving one
  needs it where it is and where it goes.
* **An asset's domain** is set or unset with ``EDIT_ENTITY_DOMAINS`` on the asset, by someone who can see it. A data
  product's domain is changed on the product (``/catalog/products``: ``MANAGE_DATA_PRODUCTS``).
* **Deleting** a domain is refused while domains are inside it, and while assets or data products are in it unless
  ``force`` is given and the caller may take each of them out (``EDIT_ENTITY_DOMAINS`` on each asset,
  ``MANAGE_DATA_PRODUCTS`` on the domain for its products); they are taken out first. Cairn Core has no soft delete
  for domains, so the domain is deleted with Core's own ``deleteDomain``.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from cairn.common.proposals import make_mcp
from cairn.marketplace.glossary import CoreEditor, EditError, entity_id, people, principals, tail

MANAGE = "MANAGE_DOMAINS"
EDIT_DOMAINS = "EDIT_ENTITY_DOMAINS"
MANAGE_PRODUCTS = "MANAGE_DATA_PRODUCTS"
ASPECTS = ["domainProperties", "ownership"]
MAX_DEPTH = 32
DELETE = "mutation cairnDeleteDomain($urn: String!) { deleteDomain(urn: $urn) }"


def domain_urn(i: str) -> str:
    return f"urn:li:domain:{i}"


class DomainIn(BaseModel):
    """A domain as written: everything but ``id`` can change."""

    id: str = Field(description="the domain's id: urn:li:domain:<id>")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20_000)
    parent: Optional[str] = Field(default=None, description="the domain it is in (its id), none at the top")
    owners: list[str] = Field(default_factory=list, description="people and groups")

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return entity_id(v, "domain")

    @field_validator("parent")
    @classmethod
    def _parent(cls, v: Optional[str]) -> Optional[str]:
        return entity_id(v, "domain") if v not in (None, "") else None

    @field_validator("owners")
    @classmethod
    def _owners(cls, v: list[str]) -> list[str]:
        return principals(v)

    @model_validator(mode="after")
    def _not_own_parent(self) -> "DomainIn":
        if self.parent == self.id:
            raise ValueError("a domain cannot be in itself")
        return self


def domain_record(urn: str, a: dict) -> dict:
    props = a.get("domainProperties") or {}
    parent = props.get("parentDomain")
    owners, stewards = people(a.get("ownership"))
    return {"id": tail(urn), "urn": urn, "name": props.get("name") or tail(urn), "description": props.get("description") or "",
            "parent": tail(parent) if parent else None, "owners": sorted(set(owners) | set(stewards))}


class Domains(CoreEditor):
    category = "domains"

    def _domains(self) -> dict[str, dict]:
        return {tail(r["urn"]): r for r in self._scroll("domain", ASPECTS) if r.get("domainProperties")}

    def _domain(self, did: str) -> Optional[dict]:
        a = self._aspects(domain_urn(did), ASPECTS)
        return a if a.get("domainProperties") else None

    def ancestors(self, did: Optional[str]) -> list[str]:
        out: list[str] = []
        while did and len(out) < MAX_DEPTH:
            urn = domain_urn(did)
            if urn in out:
                break
            out.append(urn)
            parent = (self._aspects(urn, ["domainProperties"]).get("domainProperties") or {}).get("parentDomain")
            did = tail(parent) if parent else None
        return out

    def may_manage(self, p, urn: str, parent: Optional[str]) -> bool:
        return self._may(p, MANAGE, [urn, *self.ancestors(parent)])

    def _require(self, p, urn: str, parent: Optional[str], what: str) -> None:
        if not self.may_manage(p, urn, parent):
            raise EditError(f"{MANAGE} is needed on {what} (or a domain above it)", 403)

    def list(self, p) -> list[dict]:
        check = self.can_view(p)
        out = [domain_record(r["urn"], r) for r in self._domains().values() if check(r["urn"])]
        return sorted(out, key=lambda d: (d["name"].lower(), d["id"]))

    def get(self, p, did: str) -> dict:
        a = self._domain(did)
        if a is None or not self.can_view(p)(domain_urn(did)):
            raise EditError(f"no domain {did}", 404)
        return domain_record(domain_urn(did), a)

    def rights(self, p, urn: Optional[str]) -> dict:
        if not urn:
            ok = self.authorize(p, MANAGE, None)
            return {"may_manage": ok, "reason": "" if ok else f"{MANAGE} is needed to make a top-level domain"}
        did = entity_id(urn, "domain")
        a = self._domain(did)
        if a is None:
            raise EditError(f"no domain {did}", 404)
        parent = (a.get("domainProperties") or {}).get("parentDomain")
        ok = self.may_manage(p, domain_urn(did), tail(parent) if parent else None)
        return {"may_manage": ok, "reason": "" if ok else f"{MANAGE} is needed on this domain or a domain above it"}

    def save(self, p, spec: DomainIn, create: bool) -> dict:
        urn = domain_urn(spec.id)
        before = self._aspects(urn, ASPECTS)
        exists = bool(before.get("domainProperties"))
        if create and exists:
            raise EditError(f"a domain {spec.id} exists", 409)
        if not create and not exists:
            raise EditError(f"no domain {spec.id}", 404)
        if spec.parent and self._domain(spec.parent) is None:
            raise EditError(f"no domain {spec.parent} to put it in", 422)
        props_before = before.get("domainProperties") or {}
        old_parent = tail(props_before["parentDomain"]) if props_before.get("parentDomain") else None
        if exists:
            self._require(p, urn, old_parent, spec.id)
        if not exists or spec.parent != old_parent:
            self._require(p, urn, spec.parent, spec.parent or "the top level")
        if spec.parent and urn in self.ancestors(spec.parent):
            raise EditError("a domain cannot be put in a domain inside it", 422)
        stamp = self._stamp(p.urn)
        props: dict[str, Any] = {k: v for k, v in props_before.items() if k != "parentDomain"}
        props.update({"name": spec.name, "description": spec.description})
        props.setdefault("created", stamp)
        if spec.parent:
            props["parentDomain"] = domain_urn(spec.parent)
        old = before.get("ownership") or {}
        kept = {o["owner"]: o for o in old.get("owners") or [] if o.get("owner")}
        own = {"owners": [kept.get(o) or {"owner": o, "type": "TECHNICAL_OWNER"} for o in spec.owners],
               "lastModified": stamp}
        self._write(p.urn, [make_mcp(urn, "domainProperties", props), make_mcp(urn, "ownership", own)],
                    "domains.created" if create else "domains.updated", urn,
                    {"parent": spec.parent, "owners": len(spec.owners)})
        return domain_record(urn, {"domainProperties": props, "ownership": own})

    def contents(self, p, did: str) -> dict:
        """The domain's assets and data products that ``p`` may see (Core's search), and how many are hidden."""
        self.get(p, did)
        found = self._contents(did)
        check = self.can_view(p)
        seen = [x for x in found if check(x["urn"])]
        return {"domain": domain_urn(did), "assets": [x for x in seen if x["type"] != "DATA_PRODUCT"],
                "products": [x for x in seen if x["type"] == "DATA_PRODUCT"], "hidden": len(found) - len(seen)}

    def _contents(self, did: str) -> list[dict]:
        return self.search([{"and": [{"field": "domains", "values": [domain_urn(did)]}]}])

    def delete(self, p, did: str, force: bool = False) -> dict:
        urn = domain_urn(did)
        a = self._domain(did)
        if a is None:
            raise EditError(f"no domain {did}", 404)
        parent = (a.get("domainProperties") or {}).get("parentDomain")
        self._require(p, urn, tail(parent) if parent else None, did)
        inside = sorted(k for k, r in self._domains().items()
                        if (r.get("domainProperties") or {}).get("parentDomain") == urn)
        if inside:
            raise EditError(f"the domain holds the domains {', '.join(inside[:5])}: move or delete them first", 409)
        found = self._contents(did)
        products = [x for x in found if x["type"] == "DATA_PRODUCT"]
        assets = [x for x in found if x["type"] != "DATA_PRODUCT"]
        if found and not force:
            raise EditError(f"the domain holds {len(assets)} assets and {len(products)} data products: move them "
                            "out first, or delete with force", 409)
        refused = [x["urn"] for x in assets if not self.can_view(p)(x["urn"]) or not self.authorize(p, EDIT_DOMAINS, x["urn"])]
        if products and not self.authorize(p, MANAGE_PRODUCTS, urn):
            refused += [x["urn"] for x in products]
        if refused:
            raise EditError(f"you may not take {len(refused)} of its assets and products out of the domain: "
                            f"{', '.join(refused[:5])}", 403)
        mcps = []
        for x in found:
            have = (self._aspects(x["urn"], ["domains"]).get("domains") or {}).get("domains") or []
            mcps.append(make_mcp(x["urn"], "domains", {"domains": [d for d in have if d != urn]}))
        if mcps:
            self._write(p.urn, mcps, "domains.emptied", urn, {"assets": len(assets), "products": len(products)})
        self._mutate(p.urn, "deleteDomain", DELETE, {"urn": urn}, "domains.deleted", urn, {"emptied": len(found)})
        return {"id": did, "urn": urn, "taken_out": len(found)}

    def set_domain(self, p, asset: str, domain: Optional[str]) -> dict:
        """Put ``asset`` in ``domain`` (an id or URN), or with none, take it out of its domain."""
        self._visible(p, asset)
        if asset.startswith("urn:li:dataProduct:"):
            raise EditError("a data product's domain is changed on the product (/catalog/products)", 422)
        if asset.startswith("urn:li:domain:"):
            raise EditError("a domain is put in another domain as its parent", 422)
        target = None
        if domain not in (None, ""):
            try:
                did = entity_id(str(domain), "domain")
            except ValueError as e:
                raise EditError(str(e), 422)
            if self._domain(did) is None or not self.can_view(p)(domain_urn(did)):
                raise EditError(f"no domain {did}", 404)
            target = domain_urn(did)
        if not self.authorize(p, EDIT_DOMAINS, asset):
            raise EditError(f"{EDIT_DOMAINS} is needed on {asset}", 403)
        have = (self._aspects(asset, ["domains"]).get("domains") or {}).get("domains") or []
        want = [target] if target else []
        if have == want:
            return {"asset": asset, "domain": target, "changed": False}
        self._write(p.urn, [make_mcp(asset, "domains", {"domains": want})],
                    "domains.asset_set" if target else "domains.asset_unset", asset,
                    {"domain": target, "was": have[0] if have else None})
        return {"asset": asset, "domain": target, "changed": True}


__all__ = ["DomainIn", "Domains", "domain_record", "domain_urn", "MANAGE", "EDIT_DOMAINS"]
