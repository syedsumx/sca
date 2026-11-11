"""Cairn Core's GraphQL for browser tests that run without Cairn Core, answered from the in-memory metadata graph that
Cairn's own services read and write (so a product made through Cairn's API is the product the UI then lists).

It answers only the operations the workspace's catalog pages send (by operation name), with the shapes Cairn Core
returns for them; anything else gets ``{}``. The stack job's tests (tests/e2e/test_ui_catalog_live.py) run the same
pages against the real Cairn Core."""

from __future__ import annotations

import re
from typing import Any

TYPES = {"dataset": "DATASET", "dashboard": "DASHBOARD", "chart": "CHART", "dataFlow": "DATA_FLOW", "dataJob": "DATA_JOB",
         "dataProduct": "DATA_PRODUCT", "glossaryTerm": "GLOSSARY_TERM", "glossaryNode": "GLOSSARY_NODE",
         "domain": "DOMAIN", "mlModel": "MLMODEL", "container": "CONTAINER"}
PROPS = {"dataset": "datasetProperties", "dashboard": "dashboardInfo", "chart": "chartInfo", "dataFlow": "dataFlowInfo",
         "dataJob": "dataJobInfo", "dataProduct": "dataProductProperties", "glossaryTerm": "glossaryTermInfo",
         "glossaryNode": "glossaryNodeInfo", "domain": "domainProperties", "mlModel": "mlModelProperties",
         "container": "containerProperties"}


def _kind(urn: str) -> str:
    return urn.split(":")[2]


def _platform(urn: str) -> str | None:
    m = re.search(r"urn:li:dataPlatform:([^,]+)", urn) or re.match(r"urn:li:(?:dataFlow|dashboard|chart):\(([^,]+),", urn)
    return m.group(1) if m else None


class CoreStandIn:
    def __init__(self, graph):
        self.graph = graph

    # ---- what the graph holds ---------------------------------------------------------------------------------------
    def _aspect(self, urn: str, name: str) -> dict:
        return self.graph.aspects.get((urn, name)) or {}

    def _removed(self, urn: str) -> bool:
        return bool(self._aspect(urn, "status").get("removed"))

    def _urns(self) -> list[str]:
        out = {u for (u, a) in self.graph.aspects if _kind(u) in PROPS and a == PROPS[_kind(u)]}
        return sorted(u for u in out if not self._removed(u))

    def _name(self, urn: str) -> str:
        return self._aspect(urn, PROPS.get(_kind(urn), "")).get("name") or urn.split(":", 3)[3]

    def _owners(self, urn: str) -> dict:
        return {"owners": [{"owner": {"urn": o["owner"], "username": o["owner"].split(":")[-1], "name": o["owner"].split(":")[-1]}}
                           for o in self._aspect(urn, "ownership").get("owners") or []]}

    def _domains_of(self, urn: str) -> list[str]:
        return self._aspect(urn, "domains").get("domains") or []

    def _terms_of(self, urn: str) -> list[str]:
        return [t["urn"] for t in self._aspect(urn, "glossaryTerms").get("terms") or []]

    def _field_terms_of(self, urn: str) -> list[str]:
        return [t["urn"] for f in self._aspect(urn, "editableSchemaMetadata").get("editableSchemaFieldInfo") or []
                for t in ((f.get("glossaryTerms") or {}).get("terms") or [])]

    def _entity(self, urn: str) -> dict:
        kind = _kind(urn)
        props = self._aspect(urn, PROPS.get(kind, ""))
        platform = _platform(urn)
        e: dict[str, Any] = {"urn": urn, "type": TYPES.get(kind, kind.upper()), "name": self._name(urn),
                             "properties": {"name": self._name(urn), "description": props.get("description"),
                                            "definition": props.get("definition")},
                             "platform": {"name": platform} if platform else None}
        if kind == "dataProduct":
            custom = props.get("customProperties") or {}
            assets = [a["destinationUrn"] for a in props.get("assets") or []]
            dom = self._domains_of(urn)
            e["properties"].update({"numAssets": len(assets), "externalUrl": None,
                                    "customProperties": [{"key": k, "value": v} for k, v in custom.items()]})
            e.update({"domain": {"domain": {"urn": dom[0], "properties": {"name": self._name(dom[0])}}} if dom else None,
                      "ownership": self._owners(urn), "tags": {"tags": []}, "institutionalMemory": {"elements": []},
                      "entities": {"total": len(assets), "searchResults": [{"entity": self._entity(a)} for a in assets]}})
        return e

    def _matches(self, urn: str, inp: dict) -> bool:
        types = inp.get("types")
        if types and TYPES.get(_kind(urn), "") not in types:
            return False
        q = (inp.get("query") or "*").strip("* ").lower()
        if q and q not in self._name(urn).lower() and q not in urn.lower():
            return False
        def holds(conjunction: dict) -> bool:
            for clause in conjunction.get("and") or []:
                have = {"domains": self._domains_of(urn), "glossaryTerms": self._terms_of(urn),
                        "fieldGlossaryTerms": self._field_terms_of(urn),
                        "platform": [f"urn:li:dataPlatform:{_platform(urn)}"]}.get(clause["field"], [])
                if not set(have) & set(clause["values"]):
                    return False
            return True
        return any(holds(c) for c in (inp.get("orFilters") or [{}]))

    def _search(self, inp: dict) -> dict:
        hits = [u for u in self._urns() if self._matches(u, inp)]
        facets: dict[str, dict[str, int]] = {"_entityType": {}, "platform": {}, "domains": {}}
        for u in hits:
            facets["_entityType"][TYPES.get(_kind(u), "")] = facets["_entityType"].get(TYPES.get(_kind(u), ""), 0) + 1
            if _platform(u):
                p = f"urn:li:dataPlatform:{_platform(u)}"
                facets["platform"][p] = facets["platform"].get(p, 0) + 1
            for d in self._domains_of(u):
                facets["domains"][d] = facets["domains"].get(d, 0) + 1
        start, count = int(inp.get("start") or 0), int(inp.get("count") if inp.get("count") is not None else 20)
        return {"total": len(hits), "searchResults": [{"entity": self._entity(u)} for u in hits[start:start + count]],
                "facets": [{"field": f, "displayName": f, "aggregations": [
                    {"value": v, "count": n, "entity": {"urn": v, "name": v.split(":")[-1],
                                                        "properties": {"name": self._name(v)} if f == "domains" else None}}
                    for v, n in sorted(vals.items())]} for f, vals in facets.items()]}

    # ---- the operations ---------------------------------------------------------------------------------------------
    def __call__(self, query: str, variables: dict) -> dict:
        op = (re.search(r"query\s+(\w+)", query) or [None, ""])[1]
        v = variables or {}
        mutation = (re.search(r"mutation\s+(\w+)", query) or [None, ""])[1]
        if mutation == "cairnDeleteDomain":             # Core's deleteDomain: refused with domains inside, else gone
            urn = v["urn"]
            if urn not in self._urns() or any(self._aspect(u, "domainProperties").get("parentDomain") == urn
                                               for u in self._urns()):
                return {"deleteDomain": False}
            for key in [k for k in self.graph.aspects if k[0] == urn]:
                del self.graph.aspects[key]
            return {"deleteDomain": True}
        if op in ("cairnConsumerSearch", "cairnProducts", "cairnCatalogAssets"):
            return {"searchAcrossEntities": self._search(v.get("input") or {})}
        if op == "cairnProduct":
            urn = v["urn"]
            return {"dataProduct": self._entity(urn) if urn in self._urns() else None}
        if op == "cairnConsumerAsset":
            urn = v["urn"]
            return {"entity": {**self._entity(urn), "ownership": self._owners(urn)} if urn in self._urns() else None}
        nodes = [u for u in self._urns() if _kind(u) == "glossaryNode"]
        terms = [u for u in self._urns() if _kind(u) == "glossaryTerm"]
        parent = lambda u: self._aspect(u, PROPS[_kind(u)]).get("parentNode")  # noqa: E731
        if op == "cairnGlossaryRoot":
            return {"getRootGlossaryNodes": {"nodes": [{**self._entity(n), "childrenCount": {
                        "termsCount": sum(1 for t in terms if parent(t) == n),
                        "nodesCount": sum(1 for x in nodes if parent(x) == n)}} for n in nodes if not parent(n)]},
                    "getRootGlossaryTerms": {"terms": [self._entity(t) for t in terms if not parent(t)]}}
        if op == "cairnGlossaryNode":
            n = v["urn"]
            if n not in nodes:
                return {"glossaryNode": None}
            kids = [u for u in nodes + terms if parent(u) == n]
            return {"glossaryNode": {**self._entity(n), "ownership": self._owners(n),
                                     "parentNodes": {"nodes": [self._entity(parent(n))] if parent(n) else []},
                                     "children": {"total": len(kids), "relationships": [{"entity": self._entity(k)} for k in kids]}}}
        if op == "cairnGlossaryTerm":
            t = v["urn"]
            if t not in terms:
                return {"glossaryTerm": None}
            rel = self._aspect(t, "glossaryRelatedTerms")
            as_rel = lambda key: {"relationships": [{"entity": self._entity(u)} for u in rel.get(key) or []]}  # noqa: E731
            return {"glossaryTerm": {**self._entity(t), "hierarchicalName": self._name(t), "ownership": self._owners(t),
                                     "parentNodes": {"nodes": [self._entity(parent(t))] if parent(t) else []},
                                     "isA": as_rel("isRelatedTerms"), "hasA": as_rel("hasRelatedTerms"),
                                     "related": as_rel("relatedTerms")}}
        domains = [u for u in self._urns() if _kind(u) == "domain"]
        in_domain = lambda d: [u for u in self._urns() if d in self._domains_of(u)]  # noqa: E731
        if op == "cairnDomains":
            return {"listDomains": {"total": len(domains), "domains": [
                {**self._entity(d), "ownership": self._owners(d), "entities": {"total": len(in_domain(d))}} for d in domains]}}
        if op == "cairnDomain":
            d = v["urn"]
            if d not in domains:
                return {"domain": None}
            up = self._aspect(d, "domainProperties").get("parentDomain")
            kids = [u for u in domains if self._aspect(u, "domainProperties").get("parentDomain") == d]
            return {"domain": {**self._entity(d), "ownership": self._owners(d), "entities": {"total": len(in_domain(d))},
                               "parentDomains": {"domains": [self._entity(up)] if up else []},
                               "children": {"relationships": [{"entity": self._entity(k)} for k in kids]}}}
        if op == "cairnColumnLineage":
            up = self._aspect(v["urn"], "upstreamLineage")
            return {"dataset": {"urn": v["urn"], "fineGrainedLineages": [
                {"upstreams": [{"urn": u, "path": u.rsplit(",", 1)[1].rstrip(")")} for u in f.get("upstreams") or []],
                 "downstreams": [{"urn": d, "path": d.rsplit(",", 1)[1].rstrip(")")} for d in f.get("downstreams") or []],
                 "transformOperation": f.get("transformOperation")} for f in up.get("fineGrainedLineages") or []]}}
        return {}
