"""``/collab`` router: bookmarks, follows, views, trending, enrichment, discussions, activity."""

from __future__ import annotations

from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from aegis.collab.service import CollabService, CommentIn, ThreadIn
from aegis.common.security import Principal


class BookmarkIn(BaseModel):
    note: Optional[str] = Field(default=None, max_length=500)


class ViewIn(BaseModel):
    urn: str = Field(pattern=r"^urn:li:")


class EnrichIn(BaseModel):
    urns: list[str] = Field(max_length=200)


class ReactionIn(BaseModel):
    emoji: str


def build_router(collab: CollabService, current_principal, authorize, can_view,
                 summaries: Callable[[list[str]], dict[str, dict]],
                 trust: Callable[[list[str]], dict[str, dict]],
                 explicitly_denied: Optional[Callable[[Principal, str, str], bool]] = None) -> APIRouter:
    """``can_view(principal) → (urn → bool)``; ``summaries``/``trust`` batch-resolve display data.

    Commenting is allowed for anyone who can view an asset unless a policy explicitly denies
    ``AEGIS_COMMENT`` (``explicitly_denied(principal, privilege, urn)``)."""
    router = APIRouter(prefix="/collab", tags=["collab"])

    def _view(p: Principal, urn: str) -> None:
        if not can_view(p)(urn):
            raise HTTPException(403, f"you cannot view {urn}")

    def _visible(p: Principal, items: list[dict], key: str = "urn") -> list[dict]:
        check = can_view(p)
        return [i for i in items if check(i[key])]

    @router.get("/bookmarks")
    def list_bookmarks(p: Principal = Depends(current_principal)):
        return _visible(p, collab.list_bookmarks(p.urn))

    @router.put("/bookmarks/{urn:path}")
    def put_bookmark(urn: str, body: Optional[BookmarkIn] = None, p: Principal = Depends(current_principal)):
        _view(p, urn)
        return collab.bookmark(p.urn, urn, body.note if body else None)

    @router.delete("/bookmarks/{urn:path}", status_code=204)
    def delete_bookmark(urn: str, p: Principal = Depends(current_principal)):
        collab.unbookmark(p.urn, urn)

    @router.get("/follows")
    def list_follows(p: Principal = Depends(current_principal)):
        return _visible(p, collab.list_follows(p.urn))

    @router.put("/follows/{urn:path}")
    def put_follow(urn: str, p: Principal = Depends(current_principal)):
        _view(p, urn)
        return collab.follow(p.urn, urn)

    @router.delete("/follows/{urn:path}", status_code=204)
    def delete_follow(urn: str, p: Principal = Depends(current_principal)):
        collab.unfollow(p.urn, urn)

    @router.post("/views", status_code=204)
    def view(body: ViewIn, p: Principal = Depends(current_principal)):
        collab.record_view(p.urn, body.urn)

    @router.get("/recent")
    def recent(limit: int = Query(20, le=100), p: Principal = Depends(current_principal)):
        return _visible(p, collab.recent(p.urn, limit))

    @router.get("/trending")
    def trending(entity_type: Optional[str] = None, domain: Optional[str] = None, limit: int = Query(12, le=50),
                 p: Principal = Depends(current_principal)):
        check = can_view(p)
        raw = collab.trending(limit * 4, candidate_filter=check)
        names = summaries([r["urn"] for r in raw]) if raw else {}
        badges = trust([r["urn"] for r in raw]) if raw else {}
        out = []
        for r in raw:
            s = names.get(r["urn"])
            if s is None:  # deleted or unresolvable
                continue
            if entity_type and (s.get("type") or "").lower() != entity_type.lower():
                continue
            if domain and ((s.get("domain") or {}).get("urn") != domain):
                continue
            out.append({**r, "name": s.get("name"), "type": s.get("type"), "platform": s.get("platform"),
                        "description": s.get("description"), **badges.get(r["urn"], {})})
            if len(out) >= limit:
                break
        return out

    @router.post("/enrich")
    def enrich(body: EnrichIn, p: Principal = Depends(current_principal)):
        check = can_view(p)
        urns = [u for u in dict.fromkeys(body.urns) if check(u)]
        engagement = collab.engagement(p.urn, urns)
        badges = trust(urns)
        return {u: {**engagement.get(u, {}), **badges.get(u, {"certification": None, "quality_status": None})}
                for u in urns}

    # ---- discussions ------------------------------------------------------------------
    @router.get("/threads")
    def list_threads(entity_urn: str, p: Principal = Depends(current_principal)):
        _view(p, entity_urn)
        return collab.list_threads(entity_urn)

    @router.post("/threads", status_code=201)
    def create_thread(body: ThreadIn, p: Principal = Depends(current_principal)):
        _can_comment(p, body.entity_urn)
        return collab.create_thread(p.urn, body)

    def _can_comment(p: Principal, urn: str) -> None:
        _view(p, urn)
        if explicitly_denied and explicitly_denied(p, "AEGIS_COMMENT", urn):
            raise HTTPException(403, "commenting on this asset is not permitted for you")

    @router.post("/threads/{thread_id}/comments")
    def add_comment(thread_id: str, body: CommentIn, p: Principal = Depends(current_principal)):
        t = collab.get_thread(thread_id)
        if t is None:
            raise HTTPException(404, "thread not found")
        _can_comment(p, t["entity_urn"])
        return collab.add_comment(p.urn, thread_id, body.body_md)

    @router.post("/threads/{thread_id}/{action}")
    def thread_action(thread_id: str, action: str, p: Principal = Depends(current_principal)):
        if action not in ("resolve", "reopen"):
            raise HTTPException(404, "unknown action")
        t = collab.get_thread(thread_id)
        if t is None:
            raise HTTPException(404, "thread not found")
        _view(p, t["entity_urn"])
        return collab.set_status(p.urn, thread_id, "RESOLVED" if action == "resolve" else "OPEN")

    @router.api_route("/comments/{comment_id}/reactions", methods=["POST", "DELETE"])
    def react(comment_id: str, body: ReactionIn, request: Request, p: Principal = Depends(current_principal)):
        entity = collab.thread_author_of_comment(comment_id)
        if entity is None:
            raise HTTPException(404, "comment not found")
        _view(p, entity)
        try:
            return collab.toggle_reaction(p.urn, comment_id, body.emoji, active=request.method == "POST")
        except ValueError as e:
            raise HTTPException(422, str(e))

    @router.get("/activity")
    def activity(limit: int = Query(30, le=100), p: Principal = Depends(current_principal)):
        return _visible(p, collab.activity(p.urn, limit), "entity_urn")

    return router
