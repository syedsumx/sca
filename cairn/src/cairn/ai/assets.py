"""Catalog search and asset details over Cairn's API, as the caller (``cairn search``, ``cairn show``).

* ``GET /v1/assets/search?q=&type=&limit=``: the catalog's search, keeping only what the caller may see
  (``VIEW_ENTITY_PAGE``).
* ``GET /v1/assets/describe?urn=`` (or ``?name=sales.orders``): one asset's description, schema (with each column's
  glossary terms), owners, glossary terms, tags, domain and a lineage summary (what it is built from; what is built
  from it when the catalog's lineage search is there). A name that matches several assets answers 409 with them.
"""

from __future__ import annotations

from typing import Any, Optional

from cairn.ai.catalog import ENTITY_TYPES, Catalog

MAX_SEARCH = 100


class AssetError(Exception):
    def __init__(self, message: str, status: int = 400, candidates: Optional[list] = None):
        super().__init__(message)
        self.status = status
        self.candidates = candidates or []


def _urns(aspect: Optional[dict], key: str, inner: str) -> list[str]:
    return [x.get(inner) for x in ((aspect or {}).get(key) or []) if isinstance(x, dict) and x.get(inner)]


def _name_of(urn: str) -> str:
    if urn.startswith("urn:li:dataset:(") and "," in urn:
        return urn.split(",", 2)[1]
    return urn.rsplit(":", 1)[-1]


class Assets:
    def __init__(self, graph, catalog: Optional[Catalog] = None):
        self.graph = graph
        self.catalog = catalog if catalog is not None else (Catalog(graph) if graph is not None else None)

    def search(self, query: str, types: list[str], limit: int, can_view) -> list[dict]:
        if self.catalog is None:
            raise AssetError("the catalog is not configured (CAIRN_CORE_URL)", 503)
        bad = [t for t in types if t not in ENTITY_TYPES]
        if bad:
            raise AssetError(f"unknown type {bad[0]}; types are {sorted(ENTITY_TYPES)}", 422)
        hits = self.catalog.search(query or "*", types or None, None, min(MAX_SEARCH, limit * 3))
        return [h for h in hits if h.get("urn") and can_view(h["urn"])][:limit]

    def resolve(self, name: str, can_view) -> str:
        if name.startswith("urn:li:"):
            return name
        wanted = name.strip().lower()
        hits = self.search(wanted.rsplit(".", 1)[-1], [], MAX_SEARCH // 3, can_view) if self.catalog else []
        exact = [h for h in hits if (h.get("name") or "").lower() == wanted or _name_of(h["urn"]).lower() == wanted]
        if not exact:
            exact = [h for h in hits if _name_of(h["urn"]).lower().endswith("." + wanted)]
        if not exact:
            raise AssetError(f"no asset named {name} that you may see; try `cairn search {name}`", 404)
        if len(exact) > 1:
            raise AssetError(f"{name} names {len(exact)} assets; give the urn", 409,
                             [{"urn": h["urn"], "type": h.get("type"), "platform": h.get("platform")} for h in exact])
        return exact[0]["urn"]

    def describe(self, urn: str) -> dict:
        if self.graph is None:
            raise AssetError("the catalog is not configured (CAIRN_CORE_URL)", 503)
        get = self.graph.get_aspect
        props = get(urn, "datasetProperties") or {}
        editable = get(urn, "editableDatasetProperties") or {}
        schema = get(urn, "schemaMetadata") or {}
        edit_schema = {f.get("fieldPath"): f for f in ((get(urn, "editableSchemaMetadata") or {})
                                                       .get("editableSchemaFieldInfo") or [])}
        known = any((props, schema, get(urn, "ownership"), get(urn, "status")))
        if not known:
            raise AssetError(f"no asset {urn}", 404)
        columns = []
        for f in schema.get("fields") or []:
            path = f.get("fieldPath")
            extra = edit_schema.get(path) or {}
            terms = _urns(f.get("glossaryTerms"), "terms", "urn") + _urns(extra.get("glossaryTerms"), "terms", "urn")
            tags = _urns(f.get("globalTags"), "tags", "tag") + _urns(extra.get("globalTags"), "tags", "tag")
            columns.append({"name": path, "type": f.get("nativeDataType") or (f.get("type") or {}).get("type"),
                            "description": extra.get("description") or f.get("description") or "",
                            "terms": sorted(set(terms)), "tags": sorted(set(tags))})
        owners = [{"owner": o.get("owner"), "type": o.get("type")} for o in ((get(urn, "ownership") or {}).get("owners") or [])
                  if o.get("owner")]
        upstream = [u.get("dataset") for u in ((get(urn, "upstreamLineage") or {}).get("upstreams") or []) if u.get("dataset")]
        downstream: Optional[list[str]] = None
        if self.catalog is not None:
            try:
                downstream = [d["urn"] for d in self.catalog.lineage(urn, "DOWNSTREAM", 1, 50) if d.get("urn")]
            except Exception:  # noqa: BLE001 - lineage search is optional; the summary says it is unknown
                downstream = None
        return {"urn": urn, "name": props.get("name") or _name_of(urn),
                "platform": urn.split("urn:li:dataPlatform:", 1)[1].split(",", 1)[0] if "urn:li:dataPlatform:" in urn else None,
                "description": editable.get("description") or props.get("description") or "",
                "owners": owners, "terms": sorted(_urns(get(urn, "glossaryTerms"), "terms", "urn")),
                "tags": sorted(_urns(get(urn, "globalTags"), "tags", "tag")),
                "domains": list((get(urn, "domains") or {}).get("domains") or []), "columns": columns,
                "lineage": {"upstream": upstream, "downstream": downstream}}


def build_assets_router(services, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Depends, HTTPException, Query
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/v1/assets", tags=["catalog"])

    def assets() -> Assets:
        return Assets(services.graph, services.catalog)

    def fail(e: AssetError):
        if e.candidates:
            return JSONResponse({"detail": str(e), "candidates": e.candidates}, status_code=e.status)
        raise HTTPException(e.status, str(e))

    @router.get("/search")
    def search(q: str = Query("", max_length=500), type: list[str] = Query(default=[]),  # noqa: A002 - the API's name
               limit: int = Query(20, ge=1, le=MAX_SEARCH), p=Depends(current_principal)):
        try:
            return {"results": assets().search(q, type, limit, services.can_view(p))}
        except AssetError as e:
            return fail(e)

    @router.get("/describe")
    def describe(urn: Optional[str] = Query(None, max_length=2000), name: Optional[str] = Query(None, max_length=500),
                 p=Depends(current_principal)):
        if bool(urn) == bool(name):
            raise HTTPException(422, "give urn or name")
        try:
            a = assets()
            target = urn or a.resolve(name or "", services.can_view(p))
            authorize(p, "VIEW_ENTITY_PAGE", target)
            out = a.describe(target)
        except AssetError as e:
            return fail(e)
        view = services.can_view(p)
        out["lineage"]["upstream"] = [u for u in out["lineage"]["upstream"] if view(u)]
        if out["lineage"]["downstream"] is not None:
            out["lineage"]["downstream"] = [d for d in out["lineage"]["downstream"] if view(d)]
        return out

    return router
