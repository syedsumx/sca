"""Ticket-free Data Marketplace: product cards, request policies, access requests, grants.

Lifecycle of an access request::

    PENDING ─approve→ APPROVED (grant active until expires_at) ─time→ EXPIRED
       │                     └─revoke→ REVOKED
       ├─reject→ REJECTED
       └─cancel→ CANCELLED

Approval grants a **time-boxed membership** in the product's access persona (auto-created,
scoped to the product and its assets). The PDP evaluates the expiry on every decision, so access
ends exactly on time even if no sweeper runs; the sweeper only flips request state and notifies.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import BigInteger, Column, Integer, String, Table, and_, delete, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from cairn.common.audit import AuditLog
from cairn.common.db import JSONText, metadata
from cairn.governance.personas import PersonaEngine, PersonaSpec, Scope

DAY_MS = 86_400_000

product_policies = Table(
    "cairn_product_policies", metadata,
    Column("product_urn", String(1024), primary_key=True),
    Column("doc", JSONText, nullable=False),
    Column("updated_by", String(512), nullable=False),
)
access_requests = Table(
    "cairn_access_requests", metadata,
    Column("id", String(40), primary_key=True),
    Column("product_urn", String(1024), nullable=False, index=True),
    Column("requester", String(512), nullable=False, index=True),
    Column("state", String(16), nullable=False, index=True),
    Column("doc", JSONText, nullable=False),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("updated_at_ms", BigInteger, nullable=False),
    Column("expires_at_ms", BigInteger, nullable=True, index=True),
    Column("version", Integer, nullable=False, default=1),
)
subscriptions = Table(
    "cairn_subscriptions", metadata,
    Column("actor", String(512), primary_key=True),
    Column("product_urn", String(1024), primary_key=True),
    Column("created_at_ms", BigInteger, nullable=False),
)


class AutoApprove(BaseModel):
    enabled: bool = False
    personas: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    purposes: list[str] = Field(default_factory=list, description="empty = any allowed purpose")


class RequestPolicy(BaseModel):
    auto_approve: AutoApprove = Field(default_factory=AutoApprove)
    approvers: list[str] = Field(default_factory=list, description="users/groups; empty = product owners")
    max_duration_days: int = Field(default=90, ge=1, le=365)
    purposes: list[str] = Field(default_factory=lambda: ["Analytics", "Reporting", "Data science", "Operations"])
    grant_permissions: list[str] = Field(default_factory=lambda: ["view", "view_samples", "view_queries",
                                                                  "data_access"])
    requires_justification: bool = True
    provision: list[dict[str, Any]] = Field(default_factory=list,
                                            description="what approval grants for real: {target, role|group|grant}")

    @field_validator("provision")
    @classmethod
    def _steps(cls, v: list[dict]) -> list[dict]:
        from cairn.marketplace.provisioning import ProvisionStep

        return [ProvisionStep.model_validate(s).model_dump(exclude_none=True) for s in v]


class AccessRequestIn(BaseModel):
    product_urn: str = Field(pattern=r"^urn:li:dataProduct:")
    purpose: str = Field(min_length=1, max_length=120)
    justification: str = Field(default="", max_length=2000)
    duration_days: int = Field(default=30, ge=1, le=365)


class MarketplaceError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


PRODUCTS_QUERY = """
query cairnProducts($input: SearchAcrossEntitiesInput!) {
  searchAcrossEntities(input: $input) {
    total
    searchResults { entity { urn ... on DataProduct {
      properties { name description numAssets externalUrl customProperties { key value } }
      domain { domain { urn properties { name } } }
      ownership { owners { owner { ... on CorpUser { urn } ... on CorpGroup { urn } } } }
      tags { tags { tag { urn name } } }
    } } }
  }
}
"""

PRODUCT_QUERY = """
query cairnProduct($urn: String!) {
  dataProduct(urn: $urn) {
    urn
    properties { name description numAssets externalUrl customProperties { key value } }
    domain { domain { urn properties { name } } }
    ownership { owners { owner { ... on CorpUser { urn } ... on CorpGroup { urn } } } }
    tags { tags { tag { urn name } } }
    institutionalMemory { elements { url label } }
    entities(input: {query: "*", start: 0, count: 100}) { total searchResults { entity { urn type
      ... on Dataset { name platform { name } properties { name } }
      ... on Dashboard { platform { name } properties { name } }
      ... on Chart { platform { name } properties { name } } } } }
  }
}
"""


def _now() -> int:
    return int(time.time() * 1000)


def _iso(ms: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat() if ms else None


def _grant_label(step: dict) -> str:
    from cairn.marketplace.provisioning import ProvisionStep

    return f"{ProvisionStep.model_validate(step).label()} on {step['target']}"


def _custom(props: dict) -> dict[str, str]:
    return {c["key"]: c["value"] for c in (props.get("customProperties") or [])}


class MarketplaceService:
    def __init__(self, engine: Engine, graph, personas: PersonaEngine, notify=None, audit: Optional[AuditLog] = None,
                 attributes=None, trust: Optional[Callable[[list[str]], dict[str, dict]]] = None,
                 is_admin: Optional[Callable[[str], bool]] = None):
        self.engine = engine
        self.graph = graph
        self.personas = personas
        self.notify = notify
        self.audit = audit
        self.attributes = attributes
        self.trust = trust or (lambda urns: {})
        self.is_admin = is_admin or (lambda actor: False)
        self.provisioner = None            # marketplace.provisioning.Provisioner, when targets are configured

    # ---- policies ----------------------------------------------------------------------
    def get_policy(self, product_urn: str) -> RequestPolicy:
        with self.engine.connect() as conn:
            row = conn.execute(select(product_policies).where(product_policies.c.product_urn == product_urn)).first()
        return RequestPolicy.model_validate(row.doc) if row else RequestPolicy()

    def get_policies(self, product_urns: list[str]) -> dict[str, RequestPolicy]:
        """:meth:`get_policy` for many products in one query."""
        if not product_urns:
            return {}
        with self.engine.connect() as conn:
            rows = conn.execute(select(product_policies.c.product_urn, product_policies.c.doc)
                                .where(product_policies.c.product_urn.in_(product_urns))).all()
        found = {u: RequestPolicy.model_validate(doc) for u, doc in rows}
        return {u: found.get(u) or RequestPolicy() for u in product_urns}

    def put_policy(self, product_urn: str, policy: RequestPolicy, actor: str) -> RequestPolicy:
        self._require_owner_or_admin(actor, product_urn)
        if policy.provision:
            from cairn.marketplace.provisioning import ProvisionError, ProvisionStep

            if self.provisioner is None:
                raise MarketplaceError("no provisioning targets are configured (config/provisioning)", 422)
            try:
                self.provisioner.check([ProvisionStep.model_validate(s) for s in policy.provision])
            except ProvisionError as e:
                raise MarketplaceError(str(e), 422)
        doc = policy.model_dump(mode="json")
        with self.engine.begin() as conn:
            conn.execute(delete(product_policies).where(product_policies.c.product_urn == product_urn))
            conn.execute(product_policies.insert().values(product_urn=product_urn, doc=doc, updated_by=actor))
        if self.audit:
            self.audit.record("marketplace", "policy.updated", actor, product_urn, doc)
        return policy

    # ---- catalog -------------------------------------------------------------------------
    def _owners(self, product: dict) -> list[str]:
        return [o["owner"]["urn"] for o in ((product.get("ownership") or {}).get("owners") or []) if o.get("owner")]

    def _card(self, p: dict, actor: str, access: dict[str, dict], badges: dict, consumers: dict[str, int],
              policy: Optional[RequestPolicy] = None) -> dict:
        props = p.get("properties") or {}
        custom = _custom(props)
        dom = ((p.get("domain") or {}).get("domain") or {})
        policy = policy or self.get_policy(p["urn"])
        owners = self._owners(p)
        return {
            "urn": p["urn"], "name": props.get("name") or p["urn"], "description": props.get("description") or "",
            "domain": {"urn": dom["urn"], "name": (dom.get("properties") or {}).get("name")} if dom else None,
            "owners": owners,
            "tags": [t["tag"]["urn"] for t in ((p.get("tags") or {}).get("tags") or []) if t.get("tag")],
            "certification": (badges.get(p["urn"]) or {}).get("certification"),
            "quality_status": (badges.get(p["urn"]) or {}).get("quality_status"),
            "contract": (badges.get(p["urn"]) or {}).get("contract"),
            "assets_count": props.get("numAssets") or 0,
            "consumers": consumers.get(p["urn"], 0),
            "sla": {"freshness": custom.get("sla.freshness"), "availability": custom.get("sla.availability")},
            "access": access.get(p["urn"], {"status": "NONE", "request_id": None, "expires_at": None}),
            "request_policy": {"auto_approve": policy.auto_approve.enabled,
                               "max_duration_days": policy.max_duration_days, "purposes": policy.purposes,
                               "approvers": policy.approvers or owners,
                               "grants": [_grant_label(st) for st in policy.provision]},
        }

    def _access_states(self, actor: str) -> dict[str, dict]:
        now = _now()
        out: dict[str, dict] = {}
        with self.engine.connect() as conn:
            rows = conn.execute(select(access_requests).where(access_requests.c.requester == actor)
                                .order_by(access_requests.c.created_at_ms)).fetchall()
        for r in rows:  # later requests override earlier ones
            state = r.state
            if state == "APPROVED":
                status = "GRANTED" if (r.expires_at_ms or now + 1) > now else "EXPIRED"
            else:
                status = {"PENDING": "PENDING", "REJECTED": "REJECTED", "EXPIRED": "EXPIRED"}.get(state, "NONE")
            if status == "NONE" and r.product_urn in out:
                continue
            out[r.product_urn] = {"status": status, "request_id": r.id, "expires_at": _iso(r.expires_at_ms)}
        return out

    def _consumers(self) -> dict[str, int]:
        now = _now()
        with self.engine.connect() as conn:
            rows = conn.execute(select(access_requests.c.product_urn, func.count(func.distinct(access_requests.c.requester)))
                                .where(and_(access_requests.c.state == "APPROVED",
                                            access_requests.c.expires_at_ms > now))
                                .group_by(access_requests.c.product_urn)).fetchall()
        return {u: int(n) for u, n in rows}

    def list_products(self, actor: str, q: str = "*", domain: Optional[str] = None, certified: bool = False,
                      access_filter: Optional[str] = None, sort: str = "relevance", limit: int = 60,
                      can_view: Optional[Callable[[str], bool]] = None) -> list[dict]:
        inp: dict[str, Any] = {"types": ["DATA_PRODUCT"], "query": q or "*", "start": 0, "count": limit}
        if domain:
            inp["orFilters"] = [{"and": [{"field": "domains", "values": [domain]}]}]
        data = self.graph.graphql(PRODUCTS_QUERY, {"input": inp})
        products = [r["entity"] for r in ((data.get("searchAcrossEntities") or {}).get("searchResults") or [])
                    if r.get("entity")]
        if can_view:
            products = [p for p in products if can_view(p["urn"])]
        access, consumers = self._access_states(actor), self._consumers()
        badges = self.trust([p["urn"] for p in products])
        policies = self.get_policies([p["urn"] for p in products])
        cards = [self._card(p, actor, access, badges, consumers, policies[p["urn"]]) for p in products]
        if certified:
            cards = [c for c in cards if c["certification"] == "CERTIFIED"]
        if access_filter == "mine":
            cards = [c for c in cards if c["access"]["status"] in ("GRANTED", "PENDING")]
        elif access_filter == "requestable":
            cards = [c for c in cards if c["access"]["status"] in ("NONE", "EXPIRED", "REJECTED")]
        if sort == "consumers":
            cards.sort(key=lambda c: -c["consumers"])
        elif sort == "name":
            cards.sort(key=lambda c: c["name"].lower())
        return cards

    def get_product(self, actor: str, urn: str) -> dict:
        p = (self.graph.graphql(PRODUCT_QUERY, {"urn": urn}) or {}).get("dataProduct")
        if not p:
            raise MarketplaceError(f"data product {urn} not found", 404)
        card = self._card(p, actor, self._access_states(actor), self.trust([urn]), self._consumers())
        assets = []
        for r in (((p.get("entities") or {}).get("searchResults")) or []):
            e = r.get("entity") or {}
            assets.append({"urn": e.get("urn"), "type": e.get("type"),
                           "name": (e.get("properties") or {}).get("name") or e.get("name") or e.get("urn"),
                           "platform": (e.get("platform") or {}).get("name")})
        props = p.get("properties") or {}
        readme = _custom(props).get("readme") or props.get("description") or ""
        links = (((p.get("institutionalMemory") or {}).get("elements")) or [])
        if links:
            readme += "\n\n**Links**\n" + "\n".join(f"- [{ln.get('label') or ln['url']}]({ln['url']})" for ln in links)
        with self.engine.connect() as conn:
            subscribed = bool(conn.execute(select(subscriptions).where(and_(
                subscriptions.c.actor == actor, subscriptions.c.product_urn == urn))).first())
        return {**card, "assets": assets, "readme_md": readme, "subscribed": subscribed}

    # ---- access requests ------------------------------------------------------------------
    def _row_to_request(self, r) -> dict:
        d = dict(r.doc)
        return {"id": r.id, "product_urn": r.product_urn, "requester": r.requester, "state": r.state,
                "created_at": _iso(r.created_at_ms), "expires_at": _iso(r.expires_at_ms), **d}

    def get_request(self, rid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(access_requests).where(access_requests.c.id == rid)).first()
        if not r:
            raise MarketplaceError("access request not found", 404)
        return self._row_to_request(r)

    def list_requests(self, actor: str, scope: str = "mine", state: Optional[str] = None,
                      actor_groups: Optional[list[str]] = None) -> list[dict]:
        stmt = select(access_requests).order_by(access_requests.c.created_at_ms.desc()).limit(500)
        if state:
            stmt = stmt.where(access_requests.c.state == state)
        if scope == "mine":
            stmt = stmt.where(access_requests.c.requester == actor)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        items = [self._row_to_request(r) for r in rows]
        if scope == "inbox":
            groups = set(actor_groups or [])
            items = [i for i in items if i["requester"] != actor and
                     (actor in i["approvers"] or groups & set(i["approvers"]) or self.is_admin(actor))]
        return items

    def _approvers(self, product_urn: str, policy: RequestPolicy) -> list[str]:
        if policy.approvers:
            return policy.approvers
        owners = (self.attributes.resource(product_urn).get("owners") if self.attributes else None) or []
        return owners

    def _product_persona(self, product_urn: str, name: str, policy: RequestPolicy) -> str:
        pid = "access-" + uuid.uuid5(uuid.NAMESPACE_URL, product_urn).hex[:12]
        if self.personas.get_persona(pid) is None:
            self.personas.upsert_persona(PersonaSpec(
                name=f"Access: {name}", description=f"Time-boxed consumers of data product {name} (marketplace)",
                color="#10b981", scope=Scope(data_products=[product_urn]), permissions=policy.grant_permissions),
                "urn:li:corpuser:cairn-marketplace", pid)
        return pid

    def create_request(self, actor: str, req: AccessRequestIn, subject: Optional[dict] = None) -> dict:
        policy = self.get_policy(req.product_urn)
        if req.duration_days > policy.max_duration_days:
            raise MarketplaceError(f"maximum duration for this product is {policy.max_duration_days} days", 422)
        if policy.purposes and req.purpose not in policy.purposes and "Other" not in policy.purposes:
            raise MarketplaceError(f"purpose must be one of {policy.purposes}", 422)
        if policy.requires_justification and len(req.justification.strip()) < 10:
            raise MarketplaceError("please add a short justification (at least 10 characters)", 422)
        product = self.get_product(actor, req.product_urn)
        if product["access"]["status"] in ("GRANTED", "PENDING"):
            raise MarketplaceError(f"you already have a {product['access']['status'].lower()} request for this product")
        rid = f"ar_{uuid.uuid4().hex[:16]}"
        now = _now()
        approvers = self._approvers(req.product_urn, policy)
        doc = {"product_name": product["name"], "purpose": req.purpose, "justification": req.justification,
               "duration_days": req.duration_days, "auto_approved": False, "approvers": approvers,
               "decided_by": None, "decided_at": None, "grant_id": None,
               "history": [{"at": _iso(now), "actor": actor, "from_state": None, "to_state": "PENDING",
                            "comment": req.justification[:500] or None}]}
        with self.engine.begin() as conn:
            conn.execute(access_requests.insert().values(id=rid, product_urn=req.product_urn, requester=actor,
                                                         state="PENDING", doc=doc, created_at_ms=now,
                                                         updated_at_ms=now, version=1))
        if self.audit:
            self.audit.record("marketplace", "access.requested", actor, req.product_urn,
                              {"request_id": rid, "purpose": req.purpose, "duration_days": req.duration_days})
        if self._auto_approvable(policy, req, subject or {}):
            return self._decide(rid, "approve", "urn:li:corpuser:cairn-marketplace", "auto-approved by product policy",
                                auto=True)
        self._notify_requested(rid)
        return self.get_request(rid)

    def _auto_approvable(self, policy: RequestPolicy, req: AccessRequestIn, subject: dict) -> bool:
        aa = policy.auto_approve
        if not aa.enabled:
            return False
        if aa.purposes and req.purpose not in aa.purposes:
            return False
        if not aa.personas and not aa.groups:
            return True
        personas = set(subject.get("personas") or [])
        groups = set(subject.get("groups") or [])
        return bool(personas & set(aa.personas) or groups & set(aa.groups))

    def preview(self, actor: str, req: AccessRequestIn, subject: Optional[dict] = None) -> dict:
        policy = self.get_policy(req.product_urn)
        auto = self._auto_approvable(policy, req, subject or {})
        return {"auto_approve": auto, "approvers": [] if auto else self._approvers(req.product_urn, policy),
                "max_duration_days": policy.max_duration_days}

    def act(self, rid: str, action: str, actor: str, comment: Optional[str] = None,
            actor_groups: Optional[list[str]] = None) -> dict:
        r = self.get_request(rid)
        if action == "cancel":
            if actor != r["requester"]:
                raise MarketplaceError("only the requester can cancel", 403)
            return self._transition(rid, "PENDING", "CANCELLED", actor, comment)
        if action in ("approve", "reject"):
            self._require_approver(actor, r, actor_groups)
            return self._decide(rid, action, actor, comment)
        if action == "revoke":
            if not (self._is_approver(actor, r, actor_groups) or actor == r["requester"]):
                raise MarketplaceError("only approvers or the requester can revoke", 403)
            out = self._transition(rid, "APPROVED", "REVOKED", actor, comment)
            if r.get("grant_id"):
                self.personas.revoke_grant(r["grant_id"], actor)
            out = self._provision(out, r.get("provision_steps") or [], grant=False)
            self._publish("access.revoked", out, f"Access to {r['product_name']} was revoked",
                          comment or "Your access has ended.", [r["requester"]], actor)
            return out
        raise MarketplaceError(f"unknown action {action}", 404)

    def _is_approver(self, actor: str, r: dict, actor_groups: Optional[list[str]]) -> bool:
        return actor in r["approvers"] or bool(set(actor_groups or []) & set(r["approvers"])) or self.is_admin(actor)

    def _require_approver(self, actor: str, r: dict, actor_groups: Optional[list[str]]) -> None:
        if actor == r["requester"]:
            raise MarketplaceError("separation of duties: you cannot approve your own request", 403)
        if not self._is_approver(actor, r, actor_groups):
            raise MarketplaceError("you are not an approver for this data product", 403)

    def _require_owner_or_admin(self, actor: str, product_urn: str) -> None:
        owners = (self.attributes.resource(product_urn).get("owners") if self.attributes else None) or []
        if actor not in owners and not self.is_admin(actor):
            raise MarketplaceError("only product owners or admins can change the request policy", 403)

    def _transition(self, rid: str, expected: str, to: str, actor: str, comment: Optional[str], **extra) -> dict:
        now = _now()
        with self.engine.begin() as conn:
            row = conn.execute(select(access_requests).where(access_requests.c.id == rid)).first()
            if not row or row.state != expected:
                raise MarketplaceError(f"request is {row.state if row else 'missing'}, expected {expected}")
            doc = dict(row.doc)
            doc["history"] = doc.get("history", []) + [{"at": _iso(now), "actor": actor, "from_state": row.state,
                                                         "to_state": to, "comment": comment}]
            values: dict[str, Any] = {}
            for k in list(extra):
                if k == "expires_at_ms":
                    values["expires_at_ms"] = extra.pop(k)
            doc.update(extra)
            res = conn.execute(update(access_requests).where(and_(
                access_requests.c.id == rid, access_requests.c.version == row.version)).values(
                state=to, doc=doc, updated_at_ms=now, version=row.version + 1, **values))
            if res.rowcount != 1:
                raise MarketplaceError("request changed concurrently; retry")
        if self.audit:
            self.audit.record("marketplace", f"access.{to.lower()}", actor, row.product_urn,
                              {"request_id": rid, "comment": comment})
        return self.get_request(rid)

    def _decide(self, rid: str, action: str, actor: str, comment: Optional[str], auto: bool = False) -> dict:
        r = self.get_request(rid)
        if action == "reject":
            out = self._transition(rid, "PENDING", "REJECTED", actor, comment, decided_by=actor, decided_at=_iso(_now()))
            self._publish("access.rejected", out, f"Access to {r['product_name']} was declined",
                          comment or "No reason given.", [r["requester"]], actor)
            return out
        policy = self.get_policy(r["product_urn"])
        persona_id = self._product_persona(r["product_urn"], r["product_name"], policy)
        expires = _now() + r["duration_days"] * DAY_MS
        grant_id = self.personas.grant(persona_id, r["requester"], f"access_request:{rid}", expires, actor)
        out = self._transition(rid, "PENDING", "APPROVED", actor, comment, decided_by=actor, decided_at=_iso(_now()),
                               grant_id=grant_id, persona_id=persona_id, auto_approved=auto, expires_at_ms=expires)
        if policy.provision:
            out = self._provision(out, policy.provision, grant=True)
        self._publish("access.granted", out, f"Access to {r['product_name']} granted",
                      f"{'Auto-approved' if auto else 'Approved'} for {r['duration_days']} days "
                      f"(until {out['expires_at'][:10]}). Purpose: {r['purpose']}.", [r["requester"]], actor)
        return out

    # ---- provisioning ------------------------------------------------------------------------
    def _provision(self, r: dict, steps: list[dict], grant: bool) -> dict:
        """Grant (or take away) the request's real access; outcomes are kept on the request."""
        if self.provisioner is None or not steps:
            return r
        before = r.get("provisioning") or []
        results = self.provisioner.run(steps, r["requester"], grant, before)
        with self.engine.begin() as conn:
            row = conn.execute(select(access_requests).where(access_requests.c.id == r["id"])).first()
            doc = {**dict(row.doc), "provisioning": results, "provision_steps": steps}
            conn.execute(update(access_requests).where(access_requests.c.id == r["id"]).values(doc=doc))
        failed = [x for x in results if x.get("status") == "failed"]
        newly = [x for x in failed if not any(b.get("status") == "failed" and b.get("action") == x["action"]
                                              and b.get("what") == x["what"] for b in before)]
        if newly:
            self._publish("access.provisioning_failed", r,
                          f"Could not {'grant' if grant else 'take away'} access to {r['product_name']}",
                          "; ".join(f"{x['what']} on {x['target']}: {x.get('error')}" for x in newly)
                          + ". Cairn retries a few times; after that, a person has to do it.",
                          r.get("approvers") or [], "urn:li:corpuser:cairn-marketplace")
        return self.get_request(r["id"])

    def retry_provisioning(self) -> dict[str, int]:
        """Retry failed grants on approved requests, and failed revocations on ended ones."""
        from cairn.marketplace.provisioning import MAX_ATTEMPTS

        stats = {"granted": 0, "revoked": 0}
        if self.provisioner is None:
            return stats
        with self.engine.connect() as conn:
            rows = conn.execute(select(access_requests).where(
                access_requests.c.state.in_(["APPROVED", "EXPIRED", "REVOKED"]))).fetchall()
        for row in rows:
            r = self._row_to_request(row)
            grant = r["state"] == "APPROVED"
            todo = [x for x in r.get("provisioning") or [] if x.get("status") == "failed"
                    and x.get("action") == ("grant" if grant else "revoke") and int(x.get("attempts") or 0) < MAX_ATTEMPTS]
            if todo:
                after = self._provision(r, r.get("provision_steps") or [], grant)
                stats["granted" if grant else "revoked"] += sum(
                    1 for x in after.get("provisioning") or [] if x.get("status") == ("granted" if grant else "revoked"))
        return stats

    def _notify_requested(self, rid: str) -> None:
        if self.notify is None:
            return
        from cairn.notify.hub import Action, Event

        r = self.get_request(rid)
        recipients = list(r["approvers"])
        self.notify.publish(Event(
            type="access.requested", title=f"Access request: {r['product_name']}",
            body=f"{r['requester'].split(':')[-1]} requests {r['duration_days']} days of access for "
                 f"“{r['purpose']}”. {r['justification'][:300]}",
            actor=r["requester"], entity_urn=r["product_urn"], recipients=recipients,
            url=f"{self.notify.public_url}/cairn/requests?request={rid}", payload={"request_id": rid},
            actions=[Action("approve", "access_request", rid, "Approve", "primary"),
                     Action("reject", "access_request", rid, "Reject", "danger")]))

    def _publish(self, etype: str, r: dict, title: str, body: str, recipients: list[str], actor: str) -> None:
        if self.notify is None:
            return
        from cairn.notify.hub import Event

        self.notify.publish(Event(type=etype, title=title, body=body, actor=actor, entity_urn=r["product_urn"],
                                  recipients=recipients, payload={"request_id": r["id"]}))

    def action_handler(self, action: str, object_id: str, actor: str, comment: Optional[str]) -> str:
        """Entry point for Slack buttons / signed links (``kind = access_request``)."""
        groups = (self.attributes.subject(actor).get("groups") if self.attributes else None) or []
        try:
            out = self.act(object_id, action, actor, comment, groups)
        except MarketplaceError as e:
            if e.status == 403:
                raise PermissionError(str(e))
            raise ValueError(str(e))
        return f"Access request for {out['product_name']} is now {out['state'].lower()}"

    # ---- expiry ----------------------------------------------------------------------------
    def sweep(self, now_ms: Optional[int] = None, warn_days: int = 3) -> dict[str, int]:
        now = now_ms or _now()
        stats = {"expired": 0, "warned": 0}
        with self.engine.connect() as conn:
            rows = conn.execute(select(access_requests).where(and_(
                access_requests.c.state == "APPROVED", access_requests.c.expires_at_ms.is_not(None),
                access_requests.c.expires_at_ms <= now + warn_days * DAY_MS))).fetchall()
        for row in rows:
            r = self._row_to_request(row)
            if row.expires_at_ms <= now:
                ended = self._transition(r["id"], "APPROVED", "EXPIRED", "urn:li:corpuser:cairn-marketplace",
                                         "access period ended")
                self._provision(ended, r.get("provision_steps") or [], grant=False)
                self._publish("access.revoked", r, f"Access to {r['product_name']} expired",
                              "Request again from the marketplace if you still need it.", [r["requester"]],
                              "urn:li:corpuser:cairn-marketplace")
                stats["expired"] += 1
            elif not r.get("expiry_warned"):
                with self.engine.begin() as conn:
                    doc = dict(row.doc)
                    doc["expiry_warned"] = True
                    conn.execute(update(access_requests).where(access_requests.c.id == r["id"]).values(doc=doc))
                self._publish("access.expiring", r, f"Access to {r['product_name']} expires soon",
                              f"Your access ends on {r['expires_at'][:10]}.", [r["requester"]],
                              "urn:li:corpuser:cairn-marketplace")
                stats["warned"] += 1
        return stats

    # ---- subscriptions -----------------------------------------------------------------------
    def subscribe(self, actor: str, product_urn: str) -> dict:
        try:
            with self.engine.begin() as conn:
                conn.execute(subscriptions.insert().values(actor=actor, product_urn=product_urn, created_at_ms=_now()))
        except IntegrityError:
            pass
        return {"product_urn": product_urn, "subscribed": True}

    def unsubscribe(self, actor: str, product_urn: str) -> dict:
        with self.engine.begin() as conn:
            conn.execute(delete(subscriptions).where(and_(subscriptions.c.actor == actor,
                                                          subscriptions.c.product_urn == product_urn)))
        return {"product_urn": product_urn, "subscribed": False}

    def subscribers(self, product_urn: str) -> list[str]:
        with self.engine.connect() as conn:
            return [r.actor for r in conn.execute(select(subscriptions.c.actor)
                                                  .where(subscriptions.c.product_urn == product_urn))]
