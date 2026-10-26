"""``/context`` router: findings inbox, accept/dismiss, ontology, miner status."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from cairn.common.security import Principal
from cairn.context.miner import FINDING_TYPES, ContextStore


def build_router(store: ContextStore, current_principal, authorize, can_view) -> APIRouter:
    router = APIRouter(prefix="/context", tags=["context"])

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
    def act(fid: str, action: str, p: Principal = Depends(current_principal)):
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
                return store.accept(fid, p.urn)
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
