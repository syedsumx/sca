"""Access reviews (recertification): owners confirm, or take away, the access people hold.

A **campaign** (``POST /governance/access-reviews``) takes every active, time-boxed grant (persona grants, which
approved marketplace requests create) in its scope and asks a reviewer about each one:

* **Who reviews.** A grant from an access request is reviewed by the data product's owners; any other grant by the
  campaign's ``reviewers``. Nobody reviews their own access (separation of duties): such an item goes to the other
  reviewers only. A campaign in which some grant would have no reviewer at all is refused (422): name reviewers.
* **Deciding.** A reviewer keeps or revokes each item (``POST .../items/{id}``), with a comment. A revoke takes the
  access away at once.
* **The due date.** Items still undecided when the campaign is due are revoked (``on_no_response: "revoke"``, the
  default: access nobody vouches for does not stay) or kept (``"keep"``). The scheduler closes due campaigns and
  reminds reviewers of open items once a day.
* **The record.** Every decision is audited with who decided and why; ``GET .../{id}`` reports the progress and
  each item's outcome, the evidence an auditor asks for.

Starting and closing a campaign needs ``MANAGE_POLICIES``; deciding needs only to be the item's reviewer.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Column, String, Table, Text, and_, select, update

from cairn.common.db import JSONText, metadata

DAY_MS = 86_400_000

reviews = Table(
    "cairn_access_reviews", metadata,
    Column("id", String(40), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("state", String(16), nullable=False, index=True),        # open | closed
    Column("doc", JSONText, nullable=False),                         # scope, reviewers, on_no_response
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("due_ms", BigInteger, nullable=False, index=True),
    Column("closed_ms", BigInteger, nullable=True),
    Column("reminded_ms", BigInteger, nullable=True),
)
review_items = Table(
    "cairn_access_review_items", metadata,
    Column("id", String(40), primary_key=True),
    Column("review_id", String(40), nullable=False, index=True),
    Column("grant_id", String(64), nullable=False),
    Column("persona_id", String(64), nullable=False),
    Column("actor", String(512), nullable=False),                    # whose access
    Column("source", String(128), nullable=False),
    Column("reviewers", JSONText, nullable=False),
    Column("decision", String(16), nullable=True),                   # keep | revoke | expired (gone by itself)
    Column("decided_by", String(512), nullable=True),
    Column("decided_ms", BigInteger, nullable=True),
    Column("comment", Text, nullable=True),
)


class ReviewError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class ReviewIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    personas: list[str] = Field(default_factory=list, description="the personas whose grants are reviewed; empty = all")
    reviewers: list[str] = Field(default_factory=list,
                                 description="users who review grants that do not come from an access request")
    due_days: int = Field(default=14, ge=1, le=90)
    on_no_response: Literal["revoke", "keep"] = "revoke"


class DecisionIn(BaseModel):
    decision: Literal["keep", "revoke"]
    comment: str = Field(default="", max_length=2000)


def _now() -> int:
    return int(time.time() * 1000)


class AccessReviews:
    def __init__(self, engine, personas, owners_of_request: Callable[[str], list[str]], audit=None, notify=None,
                 clock: Callable[[], int] = _now):
        """``personas``: the PersonaEngine (its grants and their revocation). ``owners_of_request(request_id)``: the
        owners of the data product an access request was for."""
        self.engine, self.personas, self.owners_of_request = engine, personas, owners_of_request
        self.audit, self.notify, self.clock = audit, notify, clock

    # ---- campaigns ---------------------------------------------------------------------------------
    def _active_grants(self, persona_ids: list[str]) -> list[dict]:
        from cairn.governance.personas import grants_table

        now = self.clock()
        q = select(grants_table).where(and_(grants_table.c.revoked.is_(False)))
        if persona_ids:
            q = q.where(grants_table.c.persona_id.in_(persona_ids))
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(q)]
        return [r for r in rows if r["expires_at_ms"] is None or r["expires_at_ms"] > now]

    def _reviewers(self, grant: dict, campaign_reviewers: list[str]) -> list[str]:
        m = re.fullmatch(r"access_request:(.+)", grant["source"] or "")
        found: list[str] = []
        if m:
            try:
                found = list(self.owners_of_request(m.group(1)) or [])
            except Exception:  # noqa: BLE001 - the request or its product is gone: the campaign's reviewers
                found = []
        found = found or list(campaign_reviewers)
        return sorted({r for r in found if r != grant["actor"]})          # nobody reviews their own access

    def start(self, actor: str, body: ReviewIn) -> dict:
        unknown = [p for p in body.personas if self.personas.get_persona(p) is None]
        if unknown:
            raise ReviewError(f"no persona {', '.join(unknown)}", 404)
        grants = self._active_grants(body.personas)
        rid, now = f"rev_{uuid.uuid4().hex[:16]}", self.clock()
        doc = {"personas": body.personas, "reviewers": body.reviewers, "on_no_response": body.on_no_response}
        items = []
        for g in grants:
            revs = self._reviewers(g, body.reviewers)
            items.append({"id": f"item_{uuid.uuid4().hex[:16]}", "review_id": rid, "grant_id": g["id"],
                          "persona_id": g["persona_id"], "actor": g["actor"], "source": g["source"],
                          "reviewers": revs})
        unassigned = [i for i in items if not i["reviewers"]]
        if unassigned:
            raise ReviewError(f"{len(unassigned)} grant(s) would have no reviewer (their owners are the people "
                              f"holding them, or none is known): name reviewers for the campaign", 422)
        with self.engine.begin() as conn:
            conn.execute(reviews.insert().values(id=rid, name=body.name, state="open", doc=doc, created_by=actor,
                                                 created_ms=now, due_ms=now + body.due_days * DAY_MS))
            if items:
                conn.execute(review_items.insert(), items)
            if self.audit is not None:
                self.audit.record("governance", "access_review.started", actor, f"urn:li:accessReview:{rid}",
                                  {"name": body.name, "items": len(items), **doc}, conn=conn)
        self._tell(sorted({r for i in items for r in i["reviewers"]}), f"Access review: {body.name}",
                   f"You have access to review in '{body.name}', due in {body.due_days} days.", rid)
        return self.get(actor, rid, admin=True)

    def _row(self, rid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(reviews).where(reviews.c.id == rid)).first()
            if r is None:
                raise ReviewError("no such access review", 404)
            items = [dict(i._mapping) for i in conn.execute(select(review_items).where(review_items.c.review_id == rid)
                                                            .order_by(review_items.c.actor))]
        return {**dict(r._mapping), "items": items}

    def get(self, actor: str, rid: str, admin: bool = False) -> dict:
        r = self._row(rid)
        items = r["items"] if admin else [i for i in r["items"] if actor in i["reviewers"]]
        if not admin and not items:
            raise ReviewError("no such access review", 404)                # not one of its reviewers
        decided = sum(1 for i in r["items"] if i["decision"])
        return {"id": r["id"], "name": r["name"], "state": r["state"], "created_by": r["created_by"],
                "created_ms": r["created_ms"], "due_ms": r["due_ms"], "closed_ms": r["closed_ms"], **r["doc"],
                "progress": {"items": len(r["items"]), "decided": decided,
                             "kept": sum(1 for i in r["items"] if i["decision"] == "keep"),
                             "revoked": sum(1 for i in r["items"] if i["decision"] == "revoke")},
                "items": items}

    def list(self, actor: str, admin: bool) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(reviews).order_by(reviews.c.created_ms.desc())).fetchall()
            mine = {r.review_id for r in conn.execute(select(review_items.c.review_id, review_items.c.reviewers))
                    if actor in (r.reviewers or [])}
        return [{"id": r.id, "name": r.name, "state": r.state, "due_ms": r.due_ms} for r in rows
                if admin or r.id in mine]

    # ---- decisions ---------------------------------------------------------------------------------
    def decide(self, actor: str, rid: str, item_id: str, body: DecisionIn) -> dict:
        r = self._row(rid)
        if r["state"] != "open":
            raise ReviewError("the review is closed", 409)
        item = next((i for i in r["items"] if i["id"] == item_id), None)
        if item is None or actor not in item["reviewers"]:
            raise ReviewError("no such item for you to review", 404)
        if item["actor"] == actor:                                         # belt and braces: never in reviewers
            raise ReviewError("nobody reviews their own access", 403)
        if item["decision"]:
            raise ReviewError(f"already decided ({item['decision']} by {item['decided_by']})", 409)
        self._settle(item, body.decision, actor, body.comment)
        return next(i for i in self._row(rid)["items"] if i["id"] == item_id)

    def _settle(self, item: dict, decision: str, actor: str, comment: str) -> None:
        if decision == "revoke":
            if not self.personas.revoke_grant(item["grant_id"], actor):
                decision = "expired"                                       # revoked or lapsed meanwhile
        with self.engine.begin() as conn:
            done = conn.execute(update(review_items).where(and_(review_items.c.id == item["id"],
                                                                review_items.c.decision.is_(None)))
                                .values(decision=decision, decided_by=actor, decided_ms=self.clock(),
                                        comment=comment or None)).rowcount
            if done and self.audit is not None:
                self.audit.record("governance", f"access_review.{decision}", actor, item["actor"],
                                  {"review": item["review_id"], "item": item["id"], "grant_id": item["grant_id"],
                                   "persona": item["persona_id"], "comment": comment}, conn=conn)

    # ---- closing and reminders ---------------------------------------------------------------------
    def close(self, actor: str, rid: str) -> dict:
        r = self._row(rid)
        if r["state"] != "open":
            raise ReviewError("the review is closed", 409)
        fallback = r["doc"].get("on_no_response", "revoke")
        for item in r["items"]:
            if not item["decision"]:
                self._settle(item, fallback, actor, f"no decision by the due date: {fallback}")
        with self.engine.begin() as conn:
            conn.execute(update(reviews).where(reviews.c.id == rid).values(state="closed", closed_ms=self.clock()))
            if self.audit is not None:
                self.audit.record("governance", "access_review.closed", actor, f"urn:li:accessReview:{rid}",
                                  {"on_no_response": fallback}, conn=conn)
        return self.get(actor, rid, admin=True)

    def run_due(self) -> int:
        """The scheduler's: close the campaigns that are due; remind reviewers of open items once a day."""
        now, closed = self.clock(), 0
        with self.engine.connect() as conn:
            open_ = [dict(r._mapping) for r in conn.execute(select(reviews).where(reviews.c.state == "open"))]
        for r in open_:
            if r["due_ms"] <= now:
                self.close("urn:li:corpuser:cairn-access-reviews", r["id"])
                closed += 1
            elif not r["reminded_ms"] or now - r["reminded_ms"] >= DAY_MS:
                pending = {rv for i in self._row(r["id"])["items"] if not i["decision"] for rv in i["reviewers"]}
                if pending:
                    days = max(1, (r["due_ms"] - now) // DAY_MS)
                    self._tell(sorted(pending), f"Access review: {r['name']}",
                               f"You have access left to review, due in {days} day(s).", r["id"])
                with self.engine.begin() as conn:
                    conn.execute(update(reviews).where(reviews.c.id == r["id"]).values(reminded_ms=now))
        return closed

    def _tell(self, who: list[str], title: str, text: str, rid: str) -> None:
        if self.notify is None or not who:
            return
        try:
            self.notify("access_review.pending", title, text, who, {"review": rid})
        except Exception:  # noqa: BLE001 - the review stands either way
            pass


def build_access_reviews_router(ar: AccessReviews, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/governance/access-reviews", tags=["governance"])

    def is_admin(p) -> bool:
        try:
            authorize(p, "MANAGE_POLICIES", None)
            return True
        except HTTPException:
            return False

    async def call(fn, *a):
        try:
            return await run_in_threadpool(fn, *a)
        except ReviewError as e:
            raise HTTPException(e.status, str(e))

    @router.post("", status_code=201)
    async def start(body: ReviewIn = Body(...), p=Depends(current_principal)):
        authorize(p, "MANAGE_POLICIES", None)
        return await call(ar.start, p.urn, body)

    @router.get("")
    async def listing(p=Depends(current_principal)):
        return await call(ar.list, p.urn, is_admin(p))

    @router.get("/{rid}")
    async def get(rid: str, p=Depends(current_principal)):
        return await call(ar.get, p.urn, rid, is_admin(p))

    @router.post("/{rid}/items/{item_id}")
    async def decide(rid: str, item_id: str, body: DecisionIn = Body(...), p=Depends(current_principal)):
        return await call(ar.decide, p.urn, rid, item_id, body)

    @router.post("/{rid}/close")
    async def close(rid: str, p=Depends(current_principal)):
        authorize(p, "MANAGE_POLICIES", None)
        return await call(ar.close, p.urn, rid)

    return router
