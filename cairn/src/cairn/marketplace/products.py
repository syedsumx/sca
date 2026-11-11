"""Data products as Cairn Core entities, made and changed through Cairn (``/catalog/products``).

A data product is a Cairn Core ``dataProduct``: its properties (name, description, the assets it holds), its domain
and its owners are aspects in the metadata graph, so search, lineage, the Marketplace and every other reader of the
catalog see it at once. This module writes those aspects; it keeps nothing of its own.

* **Who may change a product:** ``MANAGE_DATA_PRODUCTS`` on its domain (on the product itself when it has none), for
  the domain it is in now and the one it moves to. Assets added to a product must be ones the caller can see.
* **Output ports** (the assets consumers are meant to read) are a subset of the assets, kept in the product's custom
  properties (``cairn.output_ports``, a JSON list), which every Cairn Core version stores.
* **Destroying** a product marks it removed (Cairn Core's ``status`` aspect): it leaves search and the Marketplace;
  making it again with the same id brings it back.

Products made in Cairn Core's own UI are read and changed the same way: what Cairn does not manage (other custom
properties, an asset's association details) is kept as it is.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from cairn.common.proposals import make_mcp

PRIVILEGE = "MANAGE_DATA_PRODUCTS"
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

    def _aspects(self, urn: str) -> dict:
        return {a: self.graph.get_aspect(urn, a) for a in ASPECTS}

    def _exists(self, aspects: dict) -> bool:
        return bool(aspects.get("dataProductProperties")) and not (aspects.get("status") or {}).get("removed")

    def list(self) -> list[dict]:
        rows = self.graph.scroll_entities("dataProduct", ASPECTS)
        out = [record(r["urn"], r) for r in rows if self._exists(r)]
        return sorted(out, key=lambda p: (p["name"].lower(), p["id"]))

    def get(self, pid: str) -> dict:
        urn = product_urn(pid)
        aspects = self._aspects(urn)
        if not self._exists(aspects):
            raise ProductError(f"no data product {pid}", 404)
        return record(urn, aspects)

    def save(self, actor: str, spec: ProductIn, create: bool) -> dict:
        """Create (``create``: refused when it exists) or change a product. The caller has been authorized."""
        urn = product_urn(spec.id)
        before = self._aspects(urn)
        exists = self._exists(before)
        if create and exists:
            raise ProductError(f"a data product {spec.id} exists", 409)
        if not create and not exists:
            raise ProductError(f"no data product {spec.id}", 404)
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
        mcps = [make_mcp(urn, "dataProductProperties", props),
                make_mcp(urn, "domains", {"domains": [spec.domain] if spec.domain else []}),
                make_mcp(urn, "ownership", {"owners": [{"owner": o, "type": "DATAOWNER"} for o in spec.owners],
                                            "lastModified": stamp}),
                make_mcp(urn, "status", {"removed": False})]
        self.graph.emit(mcps)
        if self.audit is not None:
            self.audit.record("marketplace", "data_product.created" if create else "data_product.updated", actor, urn,
                              {"assets": len(spec.assets), "domain": spec.domain})
        return record(urn, {"dataProductProperties": props, "domains": {"domains": [spec.domain] if spec.domain else []},
                            "ownership": {"owners": [{"owner": o} for o in spec.owners]}})

    def delete(self, actor: str, pid: str) -> None:
        urn = product_urn(pid)
        if not self._exists(self._aspects(urn)):
            raise ProductError(f"no data product {pid}", 404)
        self.graph.emit([make_mcp(urn, "status", {"removed": True})])
        if self.audit is not None:
            self.audit.record("marketplace", "data_product.deleted", actor, urn, {})


__all__ = ["DataProducts", "ProductError", "ProductIn", "PRIVILEGE", "product_urn", "record"]
