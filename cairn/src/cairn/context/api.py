"""``/context`` router: findings inbox, accept/dismiss, ontology, miner status."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from cairn.common.security import Principal
from cairn.context.miner import FINDING_TYPES, ContextStore


class DefinitionEdits(BaseModel):
    """Refine a metric or join before accepting it as an approved definition."""

    name: Optional[str] = Field(default=None, max_length=200)
    expression: Optional[str] = Field(default=None, max_length=2000)
    description: Optional[str] = Field(default=None, max_length=2000)


def build_router(store: ContextStore, current_principal, authorize, can_view) -> APIRouter:
    router = APIRouter(prefix="/context", tags=["context"])
    layer = store.layer

    def _defs_visible(p: Principal) -> list[dict]:
        check = can_view(p)
        return [d for d in (layer.definitions() if layer else []) if all(check(u) for u in d["doc"].get("datasets") or [])]

    @router.get("/answers")
    def answers(question: str = Query(..., min_length=3, max_length=500), limit: int = Query(5, ge=1, le=20),
                p: Principal = Depends(current_principal)):
        """How similar questions were answered before: mined queries over tables the caller may see."""
        return layer.answers(question, can_view(p), limit) if layer else []

    @router.get("/definitions")
    def definitions(p: Principal = Depends(current_principal)):
        return _defs_visible(p)

    @router.get("/definitions/{did}/versions")
    def definition_versions(did: str, p: Principal = Depends(current_principal)):
        if not any(d["id"] == did for d in _defs_visible(p)):
            raise HTTPException(404, "definition not found")
        return layer.versions(did)

    @router.delete("/definitions/{did}")
    def retire(did: str, p: Principal = Depends(current_principal)):
        authorize(p, "MANAGE_GLOBAL_SETTINGS", None)
        try:
            return layer.retire(did, p.urn)
        except KeyError:
            raise HTTPException(404, "definition not found")

    @router.get("/definitions/export", response_class=PlainTextResponse)
    def export(format: str = Query(..., pattern="^(snowflake|databricks)$"),  # noqa: A002
               name: str = Query("cairn_semantic_view", pattern=r"^[A-Za-z0-9_.]{1,200}$"),
               source: Optional[str] = None, datasets: Optional[list[str]] = Query(None),
               p: Principal = Depends(current_principal)):
        """Approved definitions as a Snowflake semantic view or a Databricks metric view (over ``source``)."""
        from cairn.context.layer import export_databricks, export_snowflake

        defs = _defs_visible(p)
        if format == "snowflake":
            return export_snowflake(defs, name, datasets)
        if not source:
            raise HTTPException(422, "a Databricks metric view needs source= (the fact table's dataset urn)")
        return export_databricks(defs, name, source, datasets)

    def _visible(p: Principal, items: list[dict]) -> list[dict]:
        check = can_view(p)
        # A finding is visible when the caller can view every dataset/term it references.
        return [i for i in items if all(check(e) for e in i["entities"] if e.startswith("urn:li:"))]

    @router.get("/findings")
    def findings(type: Optional[str] = Query(None), status: Optional[str] = Query("OPEN"),
                 entity_urn: Optional[str] = None, limit: int = Query(100, le=500),
                 p: Principal = Depends(current_principal)):
        if type and type not in FINDING_TYPES:
            raise HTTPException(422, f"type must be one of {FINDING_TYPES}")
        return _visible(p, store.list(type, status or None, entity_urn, limit))

    @router.post("/findings/{fid}/{action}")
    def act(fid: str, action: str, body: Optional[DefinitionEdits] = None, p: Principal = Depends(current_principal)):
        try:
            f = store.get(fid)
        except KeyError:
            raise HTTPException(404, "finding not found")
        if not _visible(p, [f]):
            raise HTTPException(403, "you cannot view the assets this finding refers to")
        if action == "dismiss":
            return store.set_status(fid, "DISMISSED", p.urn)
        if action == "accept":
            # Accepting writes metadata: require edit rights on every referenced dataset.
            for e in f["entities"]:
                if e.startswith("urn:li:dataset:"):
                    authorize(p, "EDIT_ENTITY_GLOSSARY_TERMS", e)
            try:
                return store.accept(fid, p.urn, body.model_dump(exclude_none=True) if body else None)
            except ValueError as e:
                raise HTTPException(409, str(e))
        raise HTTPException(404, "unknown action")

    @router.get("/ontology")
    def ontology(limit: int = Query(300, le=2000), p: Principal = Depends(current_principal)):
        check = can_view(p)
        return store.ontology(limit, lambda urn: not urn.startswith("urn:li:dataset:") or check(urn))

    @router.get("/status")
    def status(p: Principal = Depends(current_principal)):
        return store.cursor("last_run") or {"status": "never run"}

    return router
