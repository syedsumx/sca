"""Policy Decision Point: RBAC + ABAC with deny-overrides and obligations."""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import Column, Integer, String, Table, select
from sqlalchemy.engine import Engine

from datahub_enterprise.common.audit import AuditLog
from datahub_enterprise.common.db import JSONText, metadata
from datahub_enterprise.governance.abac import Obligation, Policy, PolicyBundle, RuleResult, evaluate_policy
from datahub_enterprise.governance.attributes import AttributeResolver
from datahub_enterprise.governance.rbac import RbacModel, translate_native_policy

logger = logging.getLogger(__name__)

SYSTEM_ACTORS = {"urn:li:corpuser:__datahub_system", "urn:li:corpuser:__dhe_service"}

policy_bundles = Table(
    "dhe_policy_bundles",
    metadata,
    Column("version", Integer, primary_key=True, autoincrement=True),
    Column("created_at", String(40), nullable=False),
    Column("created_by", String(512), nullable=False),
    Column("bundle", JSONText, nullable=False),
)


class ResourceRef(BaseModel):
    type: str
    urn: str


class AuthorizeRequest(BaseModel):
    actor: str
    privilege: str
    resource: Optional[ResourceRef] = None
    sub_resources: list[ResourceRef] = Field(default_factory=list)
    resource_attributes: dict[str, Any] = Field(default_factory=dict)
    actor_groups: list[str] = Field(default_factory=list)
    subject_attributes: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)


class Decision(BaseModel):
    decision: Literal["ALLOW", "DENY", "NOT_APPLICABLE"]
    reason: str
    policy_ids: list[str] = Field(default_factory=list)
    obligations: list[Obligation] = Field(default_factory=list)
    ttl_seconds: int = 30
    trace: Optional[list[RuleResult]] = None


# Plugin-provided attribute names (from DataHub's EntitySpecResolver) → context keys.
_PLUGIN_ATTRS = ("owners", "domains", "tags", "glossary_terms", "containers", "platform_instances",
                 "structured_property_urns")


class PolicyDecisionPoint:
    def __init__(
        self,
        resolver: AttributeResolver,
        mode: Literal["additive", "authoritative"] = "additive",
        root_users: Optional[list[str]] = None,
        cache_ttl_seconds: int = 30,
        audit: Optional[AuditLog] = None,
        audit_denies: bool = True,
    ):
        self.resolver = resolver
        self.mode = mode
        self.root_users = set(root_users or [])
        self.cache_ttl = cache_ttl_seconds
        self.audit = audit
        self.audit_denies = audit_denies
        self._bundle = PolicyBundle()
        self._rbac = RbacModel()
        self._native: list[Policy] = []
        # Additional compiled layers (e.g. "personas", "purposes") managed by other modules.
        self._layers: dict[str, list[Policy]] = {}
        # Callables that add derived subject attributes (e.g. persona memberships).
        self.subject_enrichers: list[Callable[[dict, int], dict]] = []
        self._compiled: list[Policy] = []
        self._cache: dict[str, tuple[float, Decision]] = {}
        self._lock = threading.RLock()
        self._generation = 0

    # ---- policy management ---------------------------------------------------------
    @property
    def bundle(self) -> PolicyBundle:
        return self._bundle

    def set_bundle(self, bundle: PolicyBundle) -> None:
        with self._lock:
            self._bundle = bundle
            self._recompile()

    def set_rbac(self, rbac: RbacModel) -> None:
        with self._lock:
            self._rbac = rbac
            self._recompile()

    def set_native_policies(self, entities: list[dict]) -> int:
        translated = []
        for ent in entities:
            info = ent.get("dataHubPolicyInfo")
            if info:
                p = translate_native_policy(ent["urn"], info)
                if p:
                    translated.append(p)
        with self._lock:
            self._native = translated
            self._recompile()
        return len(translated)

    def set_layer(self, name: str, policies: list[Policy]) -> None:
        with self._lock:
            self._layers[name] = list(policies)
            self._recompile()

    def invalidate_decisions(self) -> None:
        with self._lock:
            self._cache.clear()

    def policies(self) -> list[Policy]:
        return list(self._compiled)

    def _recompile(self) -> None:
        self._compiled = sorted(
            [*self._bundle.policies, *self._rbac.to_policies(), *self._native,
             *(p for layer in self._layers.values() for p in layer)],
            key=lambda p: (-p.priority, p.id),
        )
        self._cache.clear()
        self._generation += 1

    # ---- decisions -----------------------------------------------------------------
    def build_context(self, req: AuthorizeRequest) -> dict:
        subject = dict(self.resolver.subject(req.actor))
        subject["groups"] = sorted(set(subject.get("groups", [])) | set(req.actor_groups))
        subject["attributes"] = {**subject.get("attributes", {}), **req.subject_attributes}
        now_ms = int(time.time() * 1000)
        for enrich in self.subject_enrichers:
            subject.update(enrich(subject, now_ms))
        resource: dict[str, Any] = {}
        if req.resource:
            resource = dict(self.resolver.resource(req.resource.urn))
            resource["type"] = req.resource.type or resource.get("type")
            for key in _PLUGIN_ATTRS:
                if req.resource_attributes.get(key):
                    resource[key] = sorted(set(resource.get(key) or []) | set(req.resource_attributes[key]))
            plugin_props = req.resource_attributes.get("structured_properties")
            if isinstance(plugin_props, list):
                # The GMS plugin sends ["urn:li:structuredProperty:<qn>=<value>", ...].
                merged = dict(resource.get("structured_properties") or {})
                for item in plugin_props:
                    urn, _, value = str(item).partition("=")
                    qn = urn.removeprefix("urn:li:structuredProperty:")
                    merged[qn] = sorted(set(merged.get(qn) or []) | ({value} if value else set()))
                resource["structured_properties"] = merged
            for key, value in req.resource_attributes.items():
                if key not in _PLUGIN_ATTRS and not (key == "structured_properties" and isinstance(value, list)):
                    resource[key] = value
        now = datetime.now(timezone.utc)
        environment = {"time": now.isoformat(), "hour": now.hour, "weekday": now.strftime("%a").lower(),
                       "now_ms": now_ms, "channel": "api", **req.environment}
        return {"subject": subject, "resource": resource, "action": req.privilege, "environment": environment}

    def _cache_key(self, req: AuthorizeRequest) -> str:
        payload = req.model_dump(mode="json", exclude={"environment"})
        payload["_env_channel"] = req.environment.get("channel")
        payload["_gen"] = self._generation
        payload["_v"] = [self.resolver.version(req.actor),
                         self.resolver.version(req.resource.urn if req.resource else None)]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def decide(self, req: AuthorizeRequest, explain: bool = False) -> Decision:
        if req.actor in SYSTEM_ACTORS or req.actor in self.root_users:
            return Decision(decision="ALLOW", reason="system or break-glass root user", policy_ids=["builtin:root"],
                            ttl_seconds=self.cache_ttl)
        # Time/IP-dependent environment attributes bypass the cache.
        cacheable = not explain and not ({"ip", "time", "mfa"} & set(req.environment))
        key = self._cache_key(req) if cacheable else ""
        if cacheable:
            with self._lock:
                hit = self._cache.get(key)
                if hit and hit[0] > time.monotonic():
                    return hit[1]

        decision = self._evaluate(req, explain)
        if cacheable:
            with self._lock:
                self._cache[key] = (time.monotonic() + self.cache_ttl, decision)
                if len(self._cache) > 50_000:
                    self._cache.clear()
        if self.audit and self.audit_denies and decision.decision == "DENY" and decision.policy_ids:
            self.audit.record("authz", "deny", req.actor, req.resource.urn if req.resource else None,
                              {"privilege": req.privilege, "policies": decision.policy_ids})
        return decision

    def _evaluate(self, req: AuthorizeRequest, explain: bool) -> Decision:
        ctx = self.build_context(req)
        results = [evaluate_policy(p, req.privilege, ctx, explain) for p in self._compiled]

        # Sub-resources (e.g. the tag being attached) are checked against deny rules only:
        # an allow on the parent is required, and no sub-resource may be explicitly denied.
        for sub in req.sub_resources:
            sub_ctx = dict(ctx)
            sub_ctx["resource"] = {**self.resolver.resource(sub.urn), "type": sub.type, "parent": ctx["resource"]}
            for p in self._compiled:
                if p.effect == "deny":
                    r = evaluate_policy(p, req.privilege, sub_ctx, explain)
                    if r.matched:
                        r.reason = f"sub-resource {sub.urn}: {r.reason}"
                        results.append(r)

        matched = [r for r in results if r.matched]
        trace = results if explain else None
        denies = [r for r in matched if r.effect == "deny"]
        if denies:
            top = denies[0]
            return Decision(decision="DENY", reason=f"denied by policy {top.policy_id}",
                            policy_ids=[r.policy_id for r in denies], ttl_seconds=self.cache_ttl, trace=trace)
        by_id = {p.id: p for p in self._compiled}
        # Obligations come from matched allow policies and from obligation-only policies; they
        # apply to every non-denied outcome (in additive mode the native authorizer may allow).
        obligations: list[Obligation] = []
        seen: set[str] = set()
        for r in matched:
            if r.effect in ("allow", "obligation"):
                for o in by_id[r.policy_id].obligations:
                    k = o.model_dump_json(exclude={"policy_id"})
                    if k not in seen:
                        seen.add(k)
                        obligations.append(o if o.policy_id else o.model_copy(update={"policy_id": r.policy_id}))
        obligation_ids = [r.policy_id for r in matched if r.effect == "obligation"]
        allows = [r for r in matched if r.effect == "allow"]
        if allows:
            return Decision(decision="ALLOW", reason=f"allowed by policy {allows[0].policy_id}",
                            policy_ids=[r.policy_id for r in allows] + obligation_ids, obligations=obligations,
                            ttl_seconds=self.cache_ttl, trace=trace)
        if self.mode == "additive":
            return Decision(decision="NOT_APPLICABLE", reason="no DHE policy applies; defer to DataHub policies",
                            policy_ids=obligation_ids, obligations=obligations, ttl_seconds=self.cache_ttl,
                            trace=trace)
        return Decision(decision="DENY", reason="no policy grants this privilege (default deny)",
                        ttl_seconds=self.cache_ttl, trace=trace)


# --------------------------------------------------------------------------- persistence
def load_bundle_from_dir(policy_dir: Optional[str]) -> tuple[PolicyBundle, RbacModel]:
    policies: list[dict] = []
    rbac: dict[str, Any] = {"roles": [], "bindings": []}
    if policy_dir and os.path.isdir(policy_dir):
        for path in sorted(glob.glob(os.path.join(policy_dir, "*.y*ml"))):
            with open(path) as fh:
                doc = yaml.safe_load(fh) or {}
            policies.extend(doc.get("policies", []))
            rbac["roles"].extend((doc.get("rbac") or {}).get("roles", []))
            rbac["bindings"].extend((doc.get("rbac") or {}).get("bindings", []))
    return PolicyBundle.model_validate({"policies": policies}), RbacModel.model_validate(rbac)


class PolicyRepository:
    """Versioned policy bundles in the DHE database; every change is audited."""

    def __init__(self, engine: Engine, audit: Optional[AuditLog] = None):
        self.engine = engine
        self.audit = audit

    def latest(self) -> Optional[PolicyBundle]:
        with self.engine.connect() as conn:
            row = conn.execute(select(policy_bundles).order_by(policy_bundles.c.version.desc()).limit(1)).first()
        return PolicyBundle.model_validate(row._mapping["bundle"]) if row else None

    def save(self, bundle: PolicyBundle, actor: str) -> int:
        with self.engine.begin() as conn:
            res = conn.execute(policy_bundles.insert().values(
                created_at=datetime.now(timezone.utc).isoformat(), created_by=actor,
                bundle=bundle.model_dump(mode="json")))
            version = int(res.inserted_primary_key[0])
        if self.audit:
            self.audit.record("governance", "policies.updated", actor, f"bundle:v{version}",
                              {"policy_ids": [p.id for p in bundle.policies]})
        return version


def sync_native_policies(pdp: PolicyDecisionPoint, graph) -> int:
    """Import active ``dataHubPolicy`` entities; call periodically (e.g. every 2 minutes)."""
    try:
        entities = graph.scroll_entities("dataHubPolicy", ["dataHubPolicyInfo"])
    except Exception as e:
        logger.warning("native policy import failed: %s", e)
        return -1
    return pdp.set_native_policies(entities)
