"""Personas and Purposes: a business-friendly access model compiled into ABAC.

* A **Persona** groups people by organisational role (*who*), the part of the catalog they work
  in (*where*), and what they may do there (*can*, as named permission bundles). Time-boxed
  **grants** (e.g. from marketplace access requests) add members with an expiry.
* A **Purpose** protects data carrying certain tags/terms/classifications. Everyone outside the
  purpose's allow list gets the configured restrictions: strict ``deny`` rules (priority 1000,
  so deny-overrides beats any persona grant) and/or masking obligations.

Both compile into :class:`~datahub_enterprise.governance.abac.Policy` objects loaded as PDP
layers. Membership is computed by :meth:`PersonaEngine.enrich_subject` and exposed to policies
as ``subject.personas``, so a policy never needs to repeat membership logic.
"""

from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, and_, delete, select, update
from sqlalchemy.engine import Engine

from datahub_enterprise.common.audit import AuditLog
from datahub_enterprise.common.db import JSONText, metadata
from datahub_enterprise.governance.abac import Obligation, Policy, evaluate_leaf

# Business-language permission bundles → DataHub (and DHE) privileges.
PERMISSION_BUNDLES: dict[str, list[str]] = {
    "view": ["VIEW_ENTITY_PAGE", "SEARCH_PRIVILEGE", "GET_ENTITY_PRIVILEGE", "GET_TIMELINE_PRIVILEGE"],
    "view_samples": ["VIEW_DATASET_PROFILE"],
    "view_queries": ["VIEW_ENTITY_QUERIES"],
    "view_usage": ["VIEW_DATASET_USAGE", "VIEW_DATASET_OPERATIONS"],
    "comment": ["DHE_COMMENT"],
    "request_access": ["DHE_REQUEST_ACCESS"],
    "edit_docs": ["EDIT_ENTITY_DOCS", "EDIT_DATASET_COL_DESCRIPTION", "EDIT_ENTITY_DOC_LINKS"],
    "edit_tags": ["EDIT_ENTITY_TAGS", "EDIT_DATASET_COL_TAGS"],
    "edit_terms": ["EDIT_ENTITY_GLOSSARY_TERMS", "EDIT_DATASET_COL_GLOSSARY_TERMS"],
    "edit_owners": ["EDIT_ENTITY_OWNERS"],
    "manage_quality": ["EDIT_ENTITY_ASSERTIONS", "EDIT_ENTITY_INCIDENTS"],
    "certify": ["DHE_APPROVE_CERTIFICATION", "DHE_APPROVE_DOCUMENTATION"],
    "approve_access": ["DHE_APPROVE_ACCESS"],
    "data_access": ["DHE_DATA_ACCESS"],
}

# Privileges whose responses can carry data values; masking obligations attach to these.
DATA_BEARING_PRIVILEGES = ["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE", "VIEW_ENTITY_QUERIES", "VIEW_DATASET_USAGE"]

PERSONA_PRIORITY = 10
PURPOSE_DENY_PRIORITY = 1000
_ID = re.compile(r"^[a-z0-9][a-z0-9\-]{1,62}$")

personas_table = Table(
    "dhe_personas", metadata,
    Column("id", String(64), primary_key=True),
    Column("doc", JSONText, nullable=False),
    Column("version", Integer, nullable=False, default=1),
    Column("updated_by", String(512), nullable=False),
    Column("updated_at", String(40), nullable=False),
)
purposes_table = Table(
    "dhe_purposes", metadata,
    Column("id", String(64), primary_key=True),
    Column("doc", JSONText, nullable=False),
    Column("version", Integer, nullable=False, default=1),
    Column("updated_by", String(512), nullable=False),
    Column("updated_at", String(40), nullable=False),
)
grants_table = Table(
    "dhe_persona_grants", metadata,
    Column("id", String(64), primary_key=True),
    Column("persona_id", String(64), nullable=False, index=True),
    Column("actor", String(512), nullable=False, index=True),
    Column("source", String(128), nullable=False),
    Column("expires_at_ms", BigInteger, nullable=True),
    Column("revoked", Boolean, nullable=False, default=False),
    Column("created_at_ms", BigInteger, nullable=False),
)


class AttributeRule(BaseModel):
    attr: str = Field(description="subject attribute name, e.g. department, title, country")
    op: Literal["eq", "neq", "in", "contains", "starts_with", "matches"] = "eq"
    value: Any


class Members(BaseModel):
    users: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    attribute_rules: list[AttributeRule] = Field(default_factory=list)


class Scope(BaseModel):
    """Union semantics: an asset is in scope if it matches ANY listed criterion. Empty = everything."""

    domains: list[str] = Field(default_factory=list)
    data_products: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    glossary_terms: list[str] = Field(default_factory=list)
    asset_urns: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not any((self.domains, self.data_products, self.platforms, self.tags, self.glossary_terms,
                        self.asset_urns))


class PersonaSpec(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    color: str = "#3b82f6"
    members: Members = Field(default_factory=Members)
    scope: Scope = Field(default_factory=Scope)
    permissions: list[str] = Field(default_factory=lambda: ["view"])
    enabled: bool = True

    @field_validator("permissions")
    @classmethod
    def _known_bundles(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - set(PERMISSION_BUNDLES))
        if unknown:
            raise ValueError(f"unknown permission bundles {unknown}; known: {sorted(PERMISSION_BUNDLES)}")
        return v


class Persona(PersonaSpec):
    id: str
    updated_at: str
    updated_by: Optional[str] = None


class Allowed(BaseModel):
    users: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    personas: list[str] = Field(default_factory=list)


class Mask(BaseModel):
    enabled: bool = True
    strategy: Literal["redact", "partial", "hash", "null"] = "partial"


class PurposeSpec(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    tags: list[str] = Field(min_length=1, description="tag/term urns (glob '*' allowed) or classification labels "
                                                        "such as 'PII' or 'PII.*'")
    allowed: Allowed = Field(default_factory=Allowed)
    deny: list[str] = Field(default_factory=lambda: ["view_samples", "view_queries"])
    mask: Mask = Field(default_factory=Mask)
    enabled: bool = True

    @field_validator("deny")
    @classmethod
    def _known_bundles(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - set(PERMISSION_BUNDLES))
        if unknown:
            raise ValueError(f"unknown permission bundles {unknown}")
        return v


class Purpose(PurposeSpec):
    id: str
    updated_at: str
    updated_by: Optional[str] = None


def privileges_for(bundles: list[str]) -> list[str]:
    return sorted({p for b in bundles for p in PERMISSION_BUNDLES[b]})


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:56] or "item"
    return slug if len(slug) >= 2 else f"{slug}-x"


# --------------------------------------------------------------------------- compilation
def _platform_values(platforms: list[str]) -> list[str]:
    return [p.removeprefix("urn:li:dataPlatform:") for p in platforms]


def scope_condition(scope: Scope) -> Optional[dict]:
    if scope.is_empty():
        return None
    clauses: list[dict] = []
    if scope.domains:
        clauses.append({"attr": "resource.domains", "op": "contains_any", "value": scope.domains})
    if scope.data_products:
        clauses.append({"attr": "resource.data_products", "op": "contains_any", "value": scope.data_products})
        clauses.append({"attr": "resource.urn", "op": "in", "value": scope.data_products})
    if scope.platforms:
        clauses.append({"attr": "resource.platform", "op": "in", "value": _platform_values(scope.platforms)})
    if scope.tags:
        clauses.append({"attr": "resource.tags", "op": "contains_any", "value": scope.tags})
    if scope.glossary_terms:
        clauses.append({"attr": "resource.glossary_terms", "op": "contains_any", "value": scope.glossary_terms})
    if scope.asset_urns:
        clauses.append({"attr": "resource.urn", "op": "in", "value": scope.asset_urns})
    return {"any": clauses} if len(clauses) > 1 else clauses[0]


def label_match_condition(labels: list[str]) -> dict:
    """Match resources carrying any of ``labels`` (tags, terms, classifications; '*' = prefix)."""
    clauses: list[dict] = []
    for label in labels:
        prefix = label.endswith("*")
        base = label.rstrip("*").rstrip(".") if prefix else label
        if label.startswith("urn:li:tag:"):
            clauses.append({"attr": "resource.all_labels", "op": "starts_with" if prefix else "contains", "value": base})
        elif label.startswith("urn:li:glossaryTerm:"):
            clauses.append({"attr": "resource.all_labels", "op": "starts_with" if prefix else "contains", "value": base})
        else:
            # Classification labels already include every ancestor ("PII.Email" ⇒ "PII").
            clauses.append({"attr": "resource.classification", "op": "contains", "value": base})
    return {"any": clauses} if len(clauses) > 1 else clauses[0]


def compile_persona(p: Persona) -> list[Policy]:
    if not p.enabled or not p.permissions:
        return []
    conds: list[dict] = [{"attr": "subject.personas", "op": "contains", "value": p.id}]
    sc = scope_condition(p.scope)
    if sc:
        conds.append(sc)
    return [Policy(
        id=f"persona:{p.id}", description=f"Persona '{p.name}': {', '.join(p.permissions)}",
        effect="allow", priority=PERSONA_PRIORITY, actions=privileges_for(p.permissions),
        condition={"all": conds} if len(conds) > 1 else conds[0], source="persona",
    )]


def compile_purpose(pu: Purpose) -> list[Policy]:
    if not pu.enabled:
        return []
    allowed: list[dict] = [{"attr": "subject.urn", "op": "in", "value": pu.allowed.users or ["__none__"]}]
    if pu.allowed.groups:
        allowed.append({"attr": "subject.groups", "op": "contains_any", "value": pu.allowed.groups})
    if pu.allowed.personas:
        allowed.append({"attr": "subject.personas", "op": "contains_any", "value": pu.allowed.personas})
    restricted = {"all": [label_match_condition(pu.tags), {"not": {"any": allowed}}]}
    out: list[Policy] = []
    if pu.deny:
        out.append(Policy(
            id=f"purpose:{pu.id}:deny", description=f"Purpose '{pu.name}': deny {', '.join(pu.deny)} outside purpose",
            effect="deny", priority=PURPOSE_DENY_PRIORITY, actions=privileges_for(pu.deny), condition=restricted,
            source="purpose",
        ))
    if pu.mask.enabled:
        out.append(Policy(
            id=f"purpose:{pu.id}:mask", description=f"Purpose '{pu.name}': mask {pu.mask.strategy} outside purpose",
            effect="obligation", priority=PURPOSE_DENY_PRIORITY, actions=DATA_BEARING_PRIVILEGES,
            condition=restricted, source="purpose",
            obligations=[Obligation(type="mask_columns", params={
                "purpose": pu.id, "labels": pu.tags, "strategy": pu.mask.strategy})],
        ))
    return out


# --------------------------------------------------------------------------- engine
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PersonaEngine:
    """CRUD + compilation + membership. Recompiles the PDP layers on every change."""

    def __init__(self, engine: Engine, pdp=None, audit: Optional[AuditLog] = None, attributes=None):
        self.engine = engine
        self.pdp = pdp
        self.audit = audit
        self.attributes = attributes
        self._lock = threading.RLock()
        self._personas: dict[str, Persona] = {}
        self._purposes: dict[str, Purpose] = {}
        self._grants: dict[str, list[tuple[str, Optional[int]]]] = {}  # actor → [(persona_id, expires)]
        self.reload()
        if pdp is not None:
            pdp.subject_enrichers.append(self.enrich_subject)

    # ---- loading / compiling -------------------------------------------------------
    def reload(self) -> None:
        with self.engine.connect() as conn:
            personas = {r.id: Persona(id=r.id, updated_at=r.updated_at, updated_by=r.updated_by, **r.doc)
                        for r in conn.execute(select(personas_table))}
            purposes = {r.id: Purpose(id=r.id, updated_at=r.updated_at, updated_by=r.updated_by, **r.doc)
                        for r in conn.execute(select(purposes_table))}
            grants: dict[str, list[tuple[str, Optional[int]]]] = {}
            for g in conn.execute(select(grants_table).where(grants_table.c.revoked.is_(False))):
                grants.setdefault(g.actor, []).append((g.persona_id, g.expires_at_ms))
        with self._lock:
            self._personas, self._purposes, self._grants = personas, purposes, grants
        self._publish()

    def _publish(self) -> None:
        if self.pdp is None:
            return
        self.pdp.set_layer("personas", [x for p in self._personas.values() for x in compile_persona(p)])
        self.pdp.set_layer("purposes", [x for p in self._purposes.values() for x in compile_purpose(p)])

    # ---- membership ----------------------------------------------------------------
    def memberships(self, subject: dict, now_ms: int) -> list[str]:
        urn = subject.get("urn")
        groups = set(subject.get("groups") or [])
        ctx = {"subject": subject}
        out = []
        with self._lock:
            personas = list(self._personas.values())
            grants = list(self._grants.get(urn or "", []))
        for p in personas:
            if not p.enabled:
                continue
            m = p.members
            hit = urn in m.users or bool(groups & set(m.groups))
            if not hit and m.attribute_rules:
                hit = all(evaluate_leaf({"attr": f"subject.attributes.{r.attr}", "op": r.op, "value": r.value}, ctx)
                          for r in m.attribute_rules)
            if hit:
                out.append(p.id)
        for persona_id, expires in grants:
            if (expires is None or expires > now_ms) and persona_id not in out and persona_id in self._personas:
                out.append(persona_id)
        return sorted(out)

    def enrich_subject(self, subject: dict, now_ms: int) -> dict:
        return {"personas": self.memberships(subject, now_ms)}

    # ---- CRUD ----------------------------------------------------------------------
    def list_personas(self) -> list[Persona]:
        return sorted(self._personas.values(), key=lambda p: p.name.lower())

    def list_purposes(self) -> list[Purpose]:
        return sorted(self._purposes.values(), key=lambda p: p.name.lower())

    def get_persona(self, pid: str) -> Optional[Persona]:
        return self._personas.get(pid)

    def get_purpose(self, pid: str) -> Optional[Purpose]:
        return self._purposes.get(pid)

    def _unique_id(self, name: str, existing: dict) -> str:
        base = slugify(name)
        pid = base
        while pid in existing:
            pid = f"{base}-{uuid.uuid4().hex[:4]}"
        return pid

    def upsert_persona(self, spec: PersonaSpec, actor: str, pid: Optional[str] = None) -> Persona:
        return self._upsert(personas_table, self._personas, Persona, spec, actor, pid, "persona")

    def upsert_purpose(self, spec: PurposeSpec, actor: str, pid: Optional[str] = None) -> Purpose:
        return self._upsert(purposes_table, self._purposes, Purpose, spec, actor, pid, "purpose")

    def _upsert(self, table, cache, model, spec, actor, pid, kind):
        if pid is not None and not _ID.match(pid):
            raise ValueError("invalid id")
        now = _now_iso()
        with self._lock:
            new = pid is None or pid not in cache
            pid = pid or self._unique_id(spec.name, cache)
            doc = spec.model_dump(mode="json")
            with self.engine.begin() as conn:
                if new:
                    conn.execute(table.insert().values(id=pid, doc=doc, version=1, updated_by=actor, updated_at=now))
                else:
                    conn.execute(update(table).where(table.c.id == pid).values(
                        doc=doc, version=table.c.version + 1, updated_by=actor, updated_at=now))
            obj = model(id=pid, updated_at=now, updated_by=actor, **doc)
            cache[pid] = obj
        if self.audit:
            self.audit.record("governance", f"{kind}.{'created' if new else 'updated'}", actor, f"{kind}:{pid}", doc)
        self._publish()
        return obj

    def delete_persona(self, pid: str, actor: str) -> bool:
        return self._delete(personas_table, self._personas, pid, actor, "persona")

    def delete_purpose(self, pid: str, actor: str) -> bool:
        return self._delete(purposes_table, self._purposes, pid, actor, "purpose")

    def _delete(self, table, cache, pid, actor, kind) -> bool:
        with self._lock, self.engine.begin() as conn:
            res = conn.execute(delete(table).where(table.c.id == pid))
            cache.pop(pid, None)
        if res.rowcount and self.audit:
            self.audit.record("governance", f"{kind}.deleted", actor, f"{kind}:{pid}", {})
        self._publish()
        return bool(res.rowcount)

    # ---- grants (time-boxed membership) ------------------------------------------------
    def grant(self, persona_id: str, actor: str, source: str, expires_at_ms: Optional[int], granted_by: str) -> str:
        if persona_id not in self._personas:
            raise KeyError(f"persona {persona_id} not found")
        gid = f"grant_{uuid.uuid4().hex[:16]}"
        with self.engine.begin() as conn:
            conn.execute(grants_table.insert().values(
                id=gid, persona_id=persona_id, actor=actor, source=source, expires_at_ms=expires_at_ms,
                revoked=False, created_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000)))
        with self._lock:
            self._grants.setdefault(actor, []).append((persona_id, expires_at_ms))
        if self.audit:
            self.audit.record("governance", "persona.granted", granted_by, actor,
                              {"persona": persona_id, "grant_id": gid, "expires_at_ms": expires_at_ms, "source": source})
        self._invalidate(actor)
        return gid

    def revoke_grant(self, grant_id: str, revoked_by: str) -> bool:
        with self.engine.begin() as conn:
            row = conn.execute(select(grants_table).where(grants_table.c.id == grant_id)).first()
            if not row or row.revoked:
                return False
            conn.execute(update(grants_table).where(grants_table.c.id == grant_id).values(revoked=True))
        self.reload()
        if self.audit:
            self.audit.record("governance", "persona.grant_revoked", revoked_by, row.actor, {"grant_id": grant_id})
        self._invalidate(row.actor)
        return True

    def expired_grants(self, now_ms: int) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(grants_table).where(and_(
                grants_table.c.revoked.is_(False), grants_table.c.expires_at_ms.is_not(None),
                grants_table.c.expires_at_ms <= now_ms))).fetchall()
        return [dict(r._mapping) for r in rows]

    def _invalidate(self, actor: str) -> None:
        if self.attributes is not None:
            self.attributes.invalidate(actor)
        if self.pdp is not None:
            self.pdp.invalidate_decisions()

    # ---- preview -------------------------------------------------------------------
    def preview(self, pid: str, graph=None, sample_size: int = 10) -> dict:
        p = self._personas.get(pid)
        if p is None:
            raise KeyError(pid)
        members = set(p.members.users)
        with self.engine.connect() as conn:
            members |= {r.actor for r in conn.execute(select(grants_table.c.actor).where(and_(
                grants_table.c.persona_id == pid, grants_table.c.revoked.is_(False))))}
        group_members = 0
        assets_total, sample_assets = 0, []
        if graph is not None:
            for g in p.members.groups:
                try:
                    data = graph.graphql(_GROUP_MEMBERS_Q, {"urn": g})
                    rel = ((data.get("corpGroup") or {}).get("relationships") or {})
                    group_members += int(rel.get("total") or 0)
                    members |= {r["entity"]["urn"] for r in rel.get("relationships") or [] if r.get("entity")}
                except Exception:
                    pass
            try:
                data = graph.graphql(_SCOPE_SEARCH_Q, {"input": _scope_search_input(p.scope, sample_size)})
                res = data.get("searchAcrossEntities") or {}
                assets_total = int(res.get("total") or 0)
                for r in res.get("searchResults") or []:
                    e = r.get("entity") or {}
                    sample_assets.append({"urn": e.get("urn"), "name": (e.get("properties") or {}).get("name")
                                          or e.get("name") or e.get("urn")})
            except Exception:
                pass
        return {"members_count": max(len(members), group_members), "sample_members": sorted(members)[:sample_size],
                "assets_in_scope": assets_total, "sample_assets": sample_assets,
                "attribute_rules": len(p.members.attribute_rules)}


_GROUP_MEMBERS_Q = """query($urn: String!) { corpGroup(urn: $urn) {
  relationships(input: {types: ["IsMemberOfGroup", "IsMemberOfNativeGroup"], direction: INCOMING, start: 0, count: 50}) {
    total relationships { entity { urn } } } } }"""

_SCOPE_SEARCH_Q = """query($input: SearchAcrossEntitiesInput!) { searchAcrossEntities(input: $input) { total
  searchResults { entity { urn ... on Dataset { name properties { name } } ... on Dashboard { properties { name } }
  ... on Chart { properties { name } } ... on DataProduct { properties { name } } } } } }"""


def _scope_search_input(scope: Scope, count: int) -> dict:
    ors = []
    for field, values in (("domains", scope.domains), ("dataProduct", scope.data_products),
                          ("platform", [p if p.startswith("urn:") else f"urn:li:dataPlatform:{p}" for p in scope.platforms]),
                          ("tags", scope.tags), ("glossaryTerms", scope.glossary_terms), ("urn", scope.asset_urns)):
        if values:
            ors.append({"and": [{"field": field, "values": values}]})
    inp: dict = {"query": "*", "start": 0, "count": count}
    if ors:
        inp["orFilters"] = ors
    return inp
