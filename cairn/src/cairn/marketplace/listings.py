"""Agents and skills in the marketplace: listings found and requested like data products.

A listing (``POST /marketplace/listings``) offers one of the publisher's own

* **agents** (``kind: agent``): a deployed agent app (``/v1/agent-apps``) or a custom agent
  (``/v1/custom-agents``), both named ``urn:li:cairnAgent:<name>``; or
* **skills** (``kind: skill``): an installed extension, a reusable tool (``/v1/extensions``), named
  ``urn:li:cairnSkill:<name>``.

Listings are searched with data products (``GET /marketplace/search?q=&kind=``) and on their own
(``GET /marketplace/listings``). A person requests access with a purpose, a justification and a duration
(``POST /marketplace/listings/<id>/access-requests``); the request is an ordinary marketplace access request
(``/marketplace/access-requests``: the inbox, approve, reject, cancel, revoke, the expiry sweep and
notifications). Approval grants a time-boxed membership in the listing's persona, which allows ``USE_AGENT``
(or ``USE_SKILL``) on the agent's (or skill's) URN and nothing else; the decision point stops allowing it at
the expiry, and the sweep marks the request expired and takes the membership away.

Only the agent's or skill's owner (or an operator) lists it; delisting ends every grant made through it.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, UniqueConstraint, and_, select, update

from cairn.common.db import JSONText, metadata

logger = logging.getLogger(__name__)

marketplace_listings = Table(
    "cairn_marketplace_listings", metadata,
    Column("id", String(40), primary_key=True),
    Column("kind", String(16), nullable=False),            # agent, skill
    Column("target", String(128), nullable=False),         # the agent's or skill's name
    Column("source", String(16), nullable=False),          # agent_app, custom_agent, extension
    Column("title", String(200), nullable=False),
    Column("description", Text, nullable=False),
    Column("tags", JSONText, nullable=False),
    Column("owner", String(512), nullable=False),
    Column("approvers", JSONText, nullable=False),
    Column("purposes", JSONText, nullable=False),
    Column("max_duration_days", Integer, nullable=False),
    Column("requires_justification", Boolean, nullable=False),
    Column("persona_id", String(64), nullable=False),
    Column("published", Boolean, nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
    UniqueConstraint("kind", "target", name="uq_cairn_marketplace_listing_target"),
)

KINDS = {"agent": ("urn:li:cairnAgent:", "use_agent", "USE_AGENT"),
         "skill": ("urn:li:cairnSkill:", "use_skill", "USE_SKILL")}
TARGET = re.compile(r"^[a-z][a-z0-9_-]{0,127}$")
PRINCIPAL = re.compile(r"^urn:li:(corpuser|corpGroup):\S{1,250}$")
LISTING_PREFIX = "urn:li:cairnListing:"
DEFAULT_PURPOSES = ["Analytics", "Reporting", "Data science", "Operations"]


class ListingError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def target_urn(kind: str, target: str) -> str:
    return KINDS[kind][0] + target


class Listings:
    def __init__(self, engine, marketplace, owner_of: Callable[[str, str], Optional[tuple[str, str]]],
                 is_operator: Callable[[str], bool], audit=None):
        # owner_of(kind, target) -> (source, owner) of an existing agent or skill; None: there is none.
        self.engine, self.marketplace, self.owner_of, self.is_operator, self.audit = (
            engine, marketplace, owner_of, is_operator, audit)

    # ---- the listing -------------------------------------------------------------------------
    def _row(self, lid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(marketplace_listings).where(marketplace_listings.c.id == lid)).first()
        if r is None:
            raise ListingError("no such listing", 404)
        return dict(r._mapping)

    def _visible(self, actor: str, row: dict) -> bool:
        return row["published"] or row["owner"] == actor or self.is_operator(actor)

    def _card(self, row: dict, access: dict[str, dict], consumers: dict[str, int]) -> dict:
        urn = LISTING_PREFIX + row["id"]
        privilege = KINDS[row["kind"]][2]
        return {"urn": urn, "id": row["id"], "kind": row["kind"], "name": row["title"], "target": row["target"],
                "target_urn": target_urn(row["kind"], row["target"]), "source": row["source"],
                "description": row["description"], "tags": list(row["tags"] or []), "owners": [row["owner"]],
                "published": bool(row["published"]), "consumers": consumers.get(urn, 0),
                "access": access.get(urn, {"status": "NONE", "request_id": None, "expires_at": None}),
                "request_policy": {"auto_approve": False, "max_duration_days": row["max_duration_days"],
                                   "purposes": list(row["purposes"] or []), "approvers": list(row["approvers"] or []),
                                   "requires_justification": bool(row["requires_justification"]),
                                   "grants": [f"{privilege} on {target_urn(row['kind'], row['target'])}"]},
                "created_ms": row["created_ms"], "updated_ms": row["updated_ms"]}

    def publish(self, actor: str, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ListingError("expected an object")
        extra = set(body) - {"kind", "target", "title", "description", "tags", "approvers", "purposes",
                             "max_duration_days", "requires_justification"}
        if extra:
            raise ListingError(f"unknown fields: {sorted(extra)}")
        kind, target = str(body.get("kind") or ""), str(body.get("target") or "")
        if kind not in KINDS:
            raise ListingError("kind is agent or skill")
        if not TARGET.match(target):
            raise ListingError("target is the agent's or skill's name")
        found = self.owner_of(kind, target)
        if found is None:
            raise ListingError(f"no {kind} {target!r}", 404)
        source, owner = found
        if owner != actor and not self.is_operator(actor):
            raise ListingError(f"only the {kind}'s owner lists it", 403)
        approvers = body.get("approvers") or [owner]
        if not isinstance(approvers, list) or not all(isinstance(a, str) and PRINCIPAL.match(a) for a in approvers):
            raise ListingError("approvers are urn:li:corpuser:… or urn:li:corpGroup:…")
        purposes = body.get("purposes") or DEFAULT_PURPOSES
        tags = body.get("tags") or []
        if not isinstance(purposes, list) or not isinstance(tags, list) or len(tags) > 20:
            raise ListingError("purposes and tags are lists (at most 20 tags)")
        try:
            days = int(body.get("max_duration_days") or 90)
        except (TypeError, ValueError):
            raise ListingError("max_duration_days is a number")
        if not 1 <= days <= 365:
            raise ListingError("max_duration_days: 1 to 365")
        title = str(body.get("title") or target)[:200]
        lid = f"lst_{uuid.uuid4().hex[:16]}"
        persona_id = self._persona(kind, target, title)
        now = _now()
        from sqlalchemy.exc import IntegrityError

        try:
            with self.engine.begin() as conn:
                conn.execute(marketplace_listings.insert().values(
                    id=lid, kind=kind, target=target, source=source, title=title,
                    description=str(body.get("description") or "")[:4000], tags=[str(t)[:64] for t in tags],
                    owner=owner, approvers=approvers, purposes=[str(p)[:120] for p in purposes],
                    max_duration_days=days, requires_justification=bool(body.get("requires_justification", True)),
                    persona_id=persona_id, published=True, created_ms=now, updated_ms=now))
        except IntegrityError:
            raise ListingError(f"{kind} {target} is listed already", 409)
        self._audit("marketplace.listing_published", actor, lid, {"kind": kind, "target": target, "source": source})
        return self.get(actor, lid)

    def _persona(self, kind: str, target: str, title: str) -> str:
        """The listing's persona: USE_AGENT (USE_SKILL) on the target's URN only; members are time-boxed grants."""
        from cairn.governance.personas import PersonaSpec, Scope

        personas = self.marketplace.personas
        pid = "listing-" + uuid.uuid5(uuid.NAMESPACE_URL, target_urn(kind, target)).hex[:12]
        if personas.get_persona(pid) is None:
            personas.upsert_persona(PersonaSpec(
                name=f"Access: {title}"[:120], description=f"Time-boxed users of {kind} {target} (marketplace)",
                color="#8b5cf6", scope=Scope(asset_urns=[target_urn(kind, target)]), permissions=[KINDS[kind][1]]),
                "urn:li:corpuser:cairn-marketplace", pid)
        return pid

    def get(self, actor: str, lid: str) -> dict:
        row = self._row(lid)
        if not self._visible(actor, row):
            raise ListingError("no such listing", 404)
        return self._card(row, self.marketplace._access_states(actor), self.marketplace._consumers())

    def search(self, actor: str, q: str = "*", kind: Optional[str] = None) -> list[dict]:
        if kind and kind not in KINDS:
            raise ListingError("kind is agent or skill")
        stmt = select(marketplace_listings).order_by(marketplace_listings.c.title)
        if kind:
            stmt = stmt.where(marketplace_listings.c.kind == kind)
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(stmt)]
        words = [w for w in (q or "").lower().split() if w and w != "*"]

        def hit(r: dict) -> bool:
            text = " ".join([r["title"], r["description"], r["target"], r["kind"], *[str(t) for t in r["tags"] or []]]).lower()
            return all(w in text for w in words)
        access, consumers = self.marketplace._access_states(actor), self.marketplace._consumers()
        return [self._card(r, access, consumers) for r in rows if self._visible(actor, r) and hit(r)]

    def set_published(self, actor: str, lid: str, published: bool) -> dict:
        row = self._row(lid)
        self._owner_only(actor, row)
        with self.engine.begin() as conn:
            conn.execute(update(marketplace_listings).where(marketplace_listings.c.id == lid)
                         .values(published=bool(published), updated_ms=_now()))
        self._audit("marketplace.listing_changed", actor, lid, {"published": bool(published)})
        return self.get(actor, lid)

    def delist(self, actor: str, lid: str) -> dict:
        """Removes the listing and ends every grant made through it."""
        from cairn.marketplace.service import access_requests

        row = self._row(lid)
        self._owner_only(actor, row)
        urn = LISTING_PREFIX + lid
        with self.engine.connect() as conn:
            ids = [r.id for r in conn.execute(select(access_requests.c.id).where(and_(
                access_requests.c.product_urn == urn, access_requests.c.state.in_(["APPROVED", "PENDING"]))))]
        ended = 0
        for rid in ids:
            r = self.marketplace.get_request(rid)
            try:
                if r["state"] == "APPROVED":
                    self.marketplace.act(rid, "revoke", actor, "the listing was removed")
                else:
                    self.marketplace._transition(rid, "PENDING", "CANCELLED", actor, "the listing was removed")
                ended += 1
            except Exception as e:  # noqa: BLE001 - the others still end
                logger.warning("listing %s: request %s not ended: %s", lid, rid, e)
        from sqlalchemy import delete

        with self.engine.begin() as conn:
            conn.execute(delete(marketplace_listings).where(marketplace_listings.c.id == lid))
        self._audit("marketplace.listing_removed", actor, lid, {"requests_ended": ended})
        return {"id": lid, "removed": True, "requests_ended": ended}

    def _owner_only(self, actor: str, row: dict) -> None:
        if row["owner"] != actor and not self.is_operator(actor):
            raise ListingError("only the listing's owner changes it", 403)

    # ---- requests ----------------------------------------------------------------------------
    def request(self, actor: str, lid: str, purpose: str, justification: str, duration_days: int) -> dict:
        """An access request for the listing, in the marketplace's queue (approved by the listing's approvers)."""
        from cairn.marketplace.service import _iso, access_requests

        row = self._row(lid)
        if not row["published"]:
            raise ListingError("no such listing", 404)
        if actor == row["owner"]:
            raise ListingError(f"you own this {row['kind']}", 409)
        try:
            duration_days = int(duration_days)
        except (TypeError, ValueError):
            raise ListingError("duration_days is a number")
        if not 1 <= duration_days <= row["max_duration_days"]:
            raise ListingError(f"duration_days: 1 to {row['max_duration_days']} for this listing")
        purposes = list(row["purposes"] or [])
        if purposes and purpose not in purposes and "Other" not in purposes:
            raise ListingError(f"purpose must be one of {purposes}")
        justification = str(justification or "")[:2000]
        if row["requires_justification"] and len(justification.strip()) < 10:
            raise ListingError("please add a short justification (at least 10 characters)")
        urn = LISTING_PREFIX + lid
        if self.marketplace._access_states(actor).get(urn, {}).get("status") in ("GRANTED", "PENDING"):
            raise ListingError("you already have access or a pending request for this listing", 409)
        rid = f"ar_{uuid.uuid4().hex[:16]}"
        now = _now()
        doc: dict[str, Any] = {
            "product_name": row["title"], "purpose": purpose, "justification": justification,
            "duration_days": duration_days, "auto_approved": False, "approvers": list(row["approvers"]),
            "decided_by": None, "decided_at": None, "grant_id": None,
            "listing_id": lid, "listing_kind": row["kind"], "listing_persona": row["persona_id"],
            "target_urn": target_urn(row["kind"], row["target"]),
            "history": [{"at": _iso(now), "actor": actor, "from_state": None, "to_state": "PENDING",
                         "comment": justification[:500] or None}]}
        with self.engine.begin() as conn:
            conn.execute(access_requests.insert().values(id=rid, product_urn=urn, requester=actor, state="PENDING",
                                                         doc=doc, created_at_ms=now, updated_at_ms=now, version=1))
        if self.audit is not None:
            self.audit.record("marketplace", "access.requested", actor, urn,
                              {"request_id": rid, "purpose": purpose, "duration_days": duration_days,
                               "target": doc["target_urn"]})
        self.marketplace._notify_requested(rid)
        return self.marketplace.get_request(rid)

    def _audit(self, action: str, actor: str, lid: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("marketplace", action, actor, LISTING_PREFIX + lid, details)


def build_listings_router(listings: Listings, current_principal, can_view=None):
    from fastapi import APIRouter, Body, Depends, HTTPException, Query

    router = APIRouter(prefix="/marketplace", tags=["marketplace"])

    def call(fn, *a):
        try:
            return fn(*a)
        except ListingError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/search")
    def search(q: str = "*", kind: str = Query("all", pattern="^(all|data_product|agent|skill)$"),
               p=Depends(current_principal)):
        """Data products, agents and skills in one list, each with ``kind``."""
        out: list[dict] = []
        if kind in ("all", "data_product"):
            try:
                cards = listings.marketplace.list_products(p.urn, q or "*", can_view=can_view(p) if can_view else None)
                out += [{**c, "kind": "data_product"} for c in cards]
            except Exception as e:  # noqa: BLE001 - the catalog's search is down: agents and skills still answer
                logger.warning("marketplace search of data products failed: %s", e)
        if kind in ("all", "agent", "skill"):
            out += call(listings.search, p.urn, q, None if kind == "all" else kind)
        return {"results": out}

    @router.get("/listings")
    def list_(q: str = "*", kind: Optional[str] = Query(None, pattern="^(agent|skill)$"), p=Depends(current_principal)):
        return {"listings": call(listings.search, p.urn, q, kind)}

    @router.post("/listings", status_code=201)
    def publish(body: dict = Body(...), p=Depends(current_principal)):
        return call(listings.publish, p.urn, body)

    @router.get("/listings/{lid}")
    def get(lid: str, p=Depends(current_principal)):
        return call(listings.get, p.urn, lid)

    @router.patch("/listings/{lid}")
    def change(lid: str, published: bool = Body(..., embed=True), p=Depends(current_principal)):
        return call(listings.set_published, p.urn, lid, published)

    @router.delete("/listings/{lid}")
    def delist(lid: str, p=Depends(current_principal)):
        return call(listings.delist, p.urn, lid)

    @router.post("/listings/{lid}/access-requests", status_code=201)
    def request(lid: str, purpose: str = Body(...), justification: str = Body(""), duration_days: int = Body(30),
                p=Depends(current_principal)):
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "access is requested by people")
        return call(listings.request, p.urn, lid, purpose, justification, duration_days)

    return router
