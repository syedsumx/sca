"""``/ai/steward``: run the AI steward and review its proposals in batches."""

from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from cairn.ai.steward import StewardError, StewardService
from cairn.common.security import Principal


class RunIn(BaseModel):
    limit: int = Field(default=100, ge=1, le=2000)
    max_drafts: int = Field(default=10, ge=0, le=200)
    urns: Optional[list[str]] = Field(default=None, max_length=2000)    # only these datasets


class DecideIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=1000)
    action: Literal["accept", "reject"]
    edits: dict[str, Any] = Field(default_factory=dict)     # proposal id → edited value


def build_router(steward: StewardService, current_principal, authorize, can_view, summaries) -> APIRouter:
    router = APIRouter(prefix="/ai/steward", tags=["ai"])

    @router.post("/run")
    def run(body: RunIn, p: Principal = Depends(current_principal)):
        authorize(p, "MANAGE_GLOBAL_SETTINGS", None)
        return steward.run(p.urn, body.limit, body.max_drafts, body.urns)

    @router.get("/proposals")
    def proposals(state: str = Query("PROPOSED", pattern="^(PROPOSED|ACCEPTED|AUTO_ACCEPTED|REJECTED)$"),
                  kind: Optional[str] = Query(None, pattern="^(owner|term|description)$"),
                  urn: Optional[str] = None, limit: int = Query(200, ge=1, le=1000),
                  p: Principal = Depends(current_principal)):
        """Proposals on datasets the caller may see, most used datasets first."""
        check = can_view(p)
        rows = [r for r in steward.proposals(state, kind, urn, limit * 2) if check(r["urn"])][:limit]
        names = summaries(sorted({r["urn"] for r in rows}))
        return [{**r, "name": (names.get(r["urn"]) or {}).get("name") or r["urn"]} for r in rows]

    @router.post("/decide")
    def decide(body: DecideIn, p: Principal = Depends(current_principal)):
        """Accept or reject many at once; each needs the right to make that edit on its dataset."""
        try:
            return steward.decide(body.ids, body.action, p.urn,
                                  lambda priv, urn: authorize(p, priv, urn, raise_on_deny=False), body.edits)
        except StewardError as e:
            raise HTTPException(e.status, str(e))

    return router
