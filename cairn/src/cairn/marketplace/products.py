"""Data products as Cairn Core entities, made and changed through Cairn (``/catalog/products``).

A data product is a Cairn Core ``dataProduct``: its properties (name, description, the assets it holds), its domain
and its owners are aspects in the metadata graph, so search, lineage, the Marketplace and every other reader of the
catalog see it at once. This module writes those aspects; it keeps nothing of its own.

* **Who may change a product:** ``MANAGE_DATA_PRODUCTS`` on its domain (on the product itself when it has none), for
  the domain it is in now and the one it moves to; making again a product that was removed also needs it on the
  domain the removed one was in. Each asset added to or taken out of a product needs ``EDIT_ENTITY_DATA_PRODUCTS`` on
  that asset (Cairn Core's own rule for an asset's data products), and the caller must be able to see it.
* **A change names what changes:** a field left out of a PUT is left as it is (an explicit ``null`` or ``[]``
  clears it). ``owners`` adds and removes only the ``DATAOWNER`` owners Cairn manages
  (:func:`cairn.common.proposals.cairn_managed_owner`); owners of other types or from other sources are kept.
  Cairn Core's reads give no aspect version, so two writers at once: the last one wins.
* **Output ports** (the assets consumers are meant to read) are a subset of the assets, kept in the product's custom
  properties (``cairn.output_ports``, a JSON list), which every Cairn Core version stores.
* **Destroying** a product marks it removed (Cairn Core's ``status`` aspect): it leaves search and the Marketplace.
  It is refused while people hold access granted through the Marketplace (revoke it first); otherwise its request
  policy, pending requests, subscriptions and access persona go with it, so a product made again with the same id
  starts with none of them, and with no tags, terms or structured properties (certification) of the old one.

Products made in Cairn Core's own UI are read and changed the same way: what Cairn does not manage (other custom
properties, an asset's association details, other owners) is kept as it is. Their ids may be ones Cairn would not
make (see ``ID``): they are changed and removed by that id all the same.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from cairn.common.proposals import cairn_managed_owner, make_mcp, merge_ownership

PRIVILEGE = "MANAGE_DATA_PRODUCTS"
ASSET_PRIVILEGE = "EDIT_ENTITY_DATA_PRODUCTS"      # on an asset, to put it in a data product or take it out
FIELDS = ("name", "description", "domain", "owners", "assets", "output_ports")
# What a product made again must not take over from the removed one (tags, terms, certification and the like).
FRESH = {"globalTags": {"tags": []}, "glossaryTerms": {"terms": []}, "structuredProperties": {"properties": []}}
NEW_PRODUCT = "~new"                 # no product has this id (see ID): the rights of one not made yet
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,99}$")
ASSET = re.compile(r"^urn:li:[A-Za-z]+:.+$")
OWNER = re.compile(r"^urn:li:(corpuser|corpGroup):[^\s]+$")
PORTS_KEY = "cairn.output_ports"
ASPECTS = ["dataProductProperties", "domains", "ownership", "status"]


class ProductError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ProductIn(BaseModel):
    """A data product as written: everything but ``id`` can change."""

    id: str = Field(description="the product's id: urn:li:dataProduct:<id>")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20_000)
    domain: Optional[str] = Field(default=None, description="the domain's URN")
    owners: list[str] = Field(default_factory=list, description="people and groups")
    assets: list[str] = Field(default_factory=list, max_length=1000, description="the assets' URNs")
    output_ports: list[str] = Field(default_factory=list, description="assets consumers read (a subset of assets)")

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID.match(v):
            raise ValueError("letters, digits, '.', '_' and '-', starting with a letter or digit (at most 100)")
        return v

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        if not v.startswith("urn:li:domain:"):
            return f"urn:li:domain:{v}"
        return v

    @field_validator("owners")
    @classmethod
    def _owners(cls, v: list[str]) -> list[str]:
        out = []
        for o in v:
            o = o if o.startswith("urn:li:") else f"urn:li:corpuser:{o}"
            if not OWNER.match(o):
                raise ValueError(f"{o}: an owner is a person (urn:li:corpuser:…) or a group (urn:li:corpGroup:…)")
            out.append(o)
        return sorted(set(out))

    @field_validator("assets", "output_ports")
    @classmethod
    def _assets(cls, v: list[str]) -> list[str]:
        for a in v:
            if not ASSET.match(a) or a.startswith("urn:li:dataProduct:"):
                raise ValueError(f"{a}: an asset's URN (a dataset, dashboard, chart, job, model…)")
        return sorted(set(v))

    @model_validator(mode="after")
    def _ports(self) -> "ProductIn":
        extra = sorted(set(self.output_ports) - set(self.assets))
        if extra:
            raise ValueError(f"output ports must be assets of the product: {', '.join(extra)}")
        return self


def product_urn(pid: str) -> str:
    return f"urn:li:dataProduct:{pid}"


def _custom(props: dict) -> dict[str, str]:
    raw = props.get("customProperties") or {}
    if isinstance(raw, list):                      # GraphQL's shape: [{key, value}]
        return {str(e.get("key")): str(e.get("value")) for e in raw if isinstance(e, dict)}
    return {str(k): str(v) for k, v in raw.items()}


def _ports(raw: Optional[str]) -> list[str]:
    """The output ports kept in a custom property: a JSON list of URNs (URNs hold commas)."""
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def record(urn: str, aspects: dict) -> dict:
    """The product as Cairn shows it, from its Cairn Core aspects."""
    props = aspects.get("dataProductProperties") or {}
    assets = [a.get("destinationUrn") for a in props.get("assets") or [] if a.get("destinationUrn")]
    custom = _custom(props)
    ports = {a.get("destinationUrn") for a in props.get("assets") or [] if a.get("outputPort")}
    ports |= set(_ports(custom.get(PORTS_KEY)))
    domains = (aspects.get("domains") or {}).get("domains") or []
    owners = [o.get("owner") for o in (aspects.get("ownership") or {}).get("owners") or [] if o.get("owner")]
    return {"id": urn.split(":", 3)[3], "urn": urn, "name": props.get("name") or urn.split(":", 3)[3],
            "description": props.get("description") or "", "domain": domains[0] if domains else None,
            "owners": sorted(set(owners)), "assets": sorted(set(assets)), "output_ports": sorted(ports & set(assets)),
            "managed_by_cairn": custom.get("cairn.managed") == "true"}


class DataProducts:
    """Reads and writes data products in the metadata graph (``graph``: Cairn Core, or a test's in-memory graph)."""

    def __init__(self, graph, audit=None, clock: Callable[[], float] = time.time):
        self.graph, self.audit, self.clock = graph, audit, clock
        # The Marketplace (cairn.marketplace.service): asked whether a product may go (``retirable``, raises when
        # people hold access through it) and told when it went (``retire``: its policy, requests, subscriptions).
        self.marketplace = None
        # Called with each entity whose attributes a write changed (the product and the assets that came or went).
        self.invalidate: Optional[Callable[[str], Any]] = None

    def _aspects(self, urn: str) -> dict:
        return {a: self.graph.get_aspect(urn, a) for a in ASPECTS}

    def _exists(self, aspects: dict) -> bool:
        return bool(aspects.get("dataProductProperties")) and not (aspects.get("status") or {}).get("removed")

    def list(self) -> list[dict]:
        rows = self.graph.scroll_entities("dataProduct", ASPECTS)
        out = [record(r["urn"], r) for r in rows if self._exists(r)]
        return sorted(out, key=lambda p: (p["name"].lower(), p["id"]))

    def domains(self) -> list[str]:
        """The URNs of the domains there are (where a product can be put)."""
        return sorted(r["urn"] for r in self.graph.scroll_entities("domain", ["domainProperties"])
                      if r.get("domainProperties"))

    def get(self, pid: str) -> dict:
        urn = product_urn(pid)
        aspects = self._aspects(urn)
        if not self._exists(aspects):
            raise ProductError(f"no data product {pid}", 404)
        return record(urn, aspects)

    def removed(self, pid: str) -> Optional[dict]:
        """The product as it was when it was removed (its domain decides who may make it again), or None."""
        urn = product_urn(pid)
        aspects = self._aspects(urn)
        if aspects.get("dataProductProperties") and (aspects.get("status") or {}).get("removed"):
            return record(urn, aspects)
        return None

    def save(self, actor: str, spec: ProductIn, create: bool, given: Optional[set] = None) -> dict:
        """Create (``create``: refused when it exists) or change a product. The caller has been authorized.
        ``given``: the fields the change names (default: all); the domain and the owners are written only when
        named, and owners are merged (:func:`cairn.common.proposals.merge_ownership`)."""
        given = set(FIELDS) if given is None or create else set(given)
        urn = product_urn(spec.id)
        before = self._aspects(urn)
        exists = self._exists(before)
        if create and exists:
            raise ProductError(f"a data product {spec.id} exists", 409)
        if not create and not exists:
            raise ProductError(f"no data product {spec.id}", 404)
        old = record(urn, before) if exists else {"domain": None, "owners": [], "assets": []}
        now = int(self.clock() * 1000)
        stamp = {"time": now, "actor": actor}
        props_before = before.get("dataProductProperties") or {} if exists else {}
        kept = {a.get("destinationUrn"): a for a in props_before.get("assets") or [] if a.get("destinationUrn")}
        assets = []
        for a in spec.assets:
            assoc = dict(kept.get(a) or {"destinationUrn": a, "created": stamp})
            assoc.pop("outputPort", None)                 # ports live in the custom properties (any Core version)
            assets.append(assoc)
        custom = {k: v for k, v in _custom(props_before).items() if k != PORTS_KEY}
        custom["cairn.managed"] = "true"
        if spec.output_ports:
            custom[PORTS_KEY] = json.dumps(spec.output_ports)
        props: dict[str, Any] = {k: v for k, v in props_before.items() if k not in ("assets", "customProperties")}
        props.update({"name": spec.name, "description": spec.description, "assets": assets, "customProperties": custom})
        domains = {"domains": [spec.domain] if spec.domain else []} if "domain" in given else before.get("domains")
        ownership = before.get("ownership") if exists else None
        if "owners" in given:
            managed = {o.get("owner") for o in (ownership or {}).get("owners") or [] if cairn_managed_owner(o)}
            ownership = merge_ownership(ownership, spec.owners, managed - set(spec.owners), stamp)
        mcps = [make_mcp(urn, "dataProductProperties", props)]
        if "domain" in given:
            mcps.append(make_mcp(urn, "domains", domains))
        if "owners" in given:
            mcps.append(make_mcp(urn, "ownership", ownership))
        mcps.append(make_mcp(urn, "status", {"removed": False}))
        if create:                                        # made again: nothing of the removed one's labels
            for name, empty in FRESH.items():
                if self.graph.get_aspect(urn, name):
                    value = {**empty, "auditStamp": stamp} if name == "glossaryTerms" else empty
                    mcps.append(make_mcp(urn, name, value))
        self.graph.emit(mcps)
        new = record(urn, {"dataProductProperties": props, "domains": domains, "ownership": ownership})
        added, gone = sorted(set(new["assets"]) - set(old["assets"])), sorted(set(old["assets"]) - set(new["assets"]))
        self._changed([urn, *added, *gone])
        if self.audit is not None:
            self.audit.record("marketplace", "data_product.created" if create else "data_product.updated", actor, urn,
                              {"domain": {"from": old["domain"], "to": new["domain"]},
                               "owners": {"added": sorted(set(new["owners"]) - set(old["owners"])),
                                          "removed": sorted(set(old["owners"]) - set(new["owners"]))},
                               "assets": {"added": added, "removed": gone, "count": len(new["assets"])}})
        return new

    def _changed(self, urns: list[str]) -> None:
        if self.invalidate is None:
            return
        for u in urns:
            try:
                self.invalidate(u)
            except Exception:  # noqa: BLE001 - a cache drop never fails a write that went through
                pass

    def delete(self, actor: str, pid: str) -> None:
        """Mark the product removed. Refused (409) while people hold access granted through it in the Marketplace;
        then its request policy, pending requests, subscriptions and access persona are cleared."""
        urn = product_urn(pid)
        before = self._aspects(urn)
        if not self._exists(before):
            raise ProductError(f"no data product {pid}", 404)
        if self.marketplace is not None:
            active = self.marketplace.active_access(urn)
            if active:
                raise ProductError(f"{len(active)} access grant(s) to this product are in force "
                                   f"({', '.join(active[:5])}): revoke them in the Marketplace first", 409)
        self.graph.emit([make_mcp(urn, "status", {"removed": True})])
        cleared = self.marketplace.retire(urn, actor) if self.marketplace is not None else {}
        self._changed([urn, *record(urn, before)["assets"]])
        if self.audit is not None:
            self.audit.record("marketplace", "data_product.deleted", actor, urn, {"cleared": cleared})


__all__ = ["DataProducts", "NEW_PRODUCT", "ProductError", "ProductIn", "PRIVILEGE", "product_urn", "record"]
