"""Workspaces: business units sharing one Cairn, each run by its own delegated admins.

A workspace is a part of the catalog (its domains) with the people who work in it:

* **admins** (users or groups) govern everything in the workspace's domains: documentation, tags,
  terms, owners, quality, certification and access approvals (``admin_permissions``), and manage the
  workspace's members. They are not global admins: they cannot change policies, other workspaces, or
  which domains are theirs; only a global admin creates workspaces and assigns domains and admins;
* **members** get ``member_permissions`` (view, comment and request access by default) in those
  domains, and the workspace is their home: search, reports and the marketplace can be narrowed to it.

Both compile into ABAC policies (a PDP layer, like personas); the caller's workspaces are exposed to
policies as ``subject.workspaces`` and ``subject.workspace_admin``.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import BigInteger, Column, String, Table, select

from cairn.common.db import JSONText, metadata
from cairn.governance.abac import Policy
from cairn.governance.personas import PERMISSION_BUNDLES, privileges_for

workspaces_table = Table(
    "cairn_workspaces", metadata,
    Column("id", String(64), primary_key=True),
    Column("doc", JSONText, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
    Column("updated_by", String(256), nullable=False),
)

WORKSPACE_PRIORITY = 20
ADMIN_DEFAULT = ["view", "view_samples", "view_queries", "view_usage", "comment", "edit_docs", "edit_tags",
                 "edit_terms", "edit_owners", "manage_quality", "certify", "approve_access"]
MEMBER_DEFAULT = ["view", "comment", "request_access"]


class WorkspaceIn(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    domains: list[str] = Field(min_length=1, max_length=200)
    admins: list[str] = Field(default_factory=list, max_length=200)
    members: list[str] = Field(default_factory=list, max_length=2000)
    admin_permissions: list[str] = Field(default_factory=lambda: sorted(ADMIN_DEFAULT))
    member_permissions: list[str] = Field(default_factory=lambda: sorted(MEMBER_DEFAULT))

    @field_validator("domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        if any(not d.startswith("urn:li:domain:") for d in v):
            raise ValueError("domains are domain urns")
        return sorted(set(v))

    @field_validator("admins", "members")
    @classmethod
    def _people(cls, v: list[str]) -> list[str]:
        if any(not (p.startswith("urn:li:corpuser:") or p.startswith("urn:li:corpGroup:")) for p in v):
            raise ValueError("people are user or group urns")
        return sorted(set(v))

    @field_validator("admin_permissions", "member_permissions")
    @classmethod
    def _bundles(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - set(PERMISSION_BUNDLES))
        if unknown:
            raise ValueError(f"unknown permissions: {', '.join(unknown)}")
        return sorted(set(v))


class WorkspaceError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def compile_workspace(w: WorkspaceIn) -> list[Policy]:
    where = {"attr": "resource.domains", "op": "contains_any", "value": w.domains}
    out = []
    if w.admin_permissions:
        out.append(Policy(id=f"workspace:{w.id}:admins", description=f"Workspace '{w.name}': admins",
                          effect="allow", priority=WORKSPACE_PRIORITY, actions=privileges_for(w.admin_permissions),
                          condition={"all": [{"attr": "subject.workspace_admin", "op": "contains", "value": w.id}, where]},
                          source="workspace"))
    if w.member_permissions:
        out.append(Policy(id=f"workspace:{w.id}:members", description=f"Workspace '{w.name}': members",
                          effect="allow", priority=WORKSPACE_PRIORITY, actions=privileges_for(w.member_permissions),
                          condition={"all": [{"attr": "subject.workspaces", "op": "contains", "value": w.id}, where]},
                          source="workspace"))
    return out


class WorkspaceService:
    ASSETS_TTL_SECONDS = 60

    def __init__(self, engine, pdp=None, graph=None, audit=None, attributes=None):
        self.engine = engine
        self.pdp = pdp
        self.graph = graph
        self.audit = audit
        self.attributes = attributes
        self._lock = threading.RLock()
        self._all: dict[str, WorkspaceIn] = {}
        self._assets: dict[str, tuple[float, list[dict]]] = {}
        self.reload()
        if pdp is not None:
            pdp.subject_enrichers.append(self.enrich_subject)

    # ---- definitions ---------------------------------------------------------------------------
    def reload(self) -> None:
        with self.engine.connect() as conn:
            found = {r.id: WorkspaceIn.model_validate(r.doc) for r in conn.execute(select(workspaces_table))}
        with self._lock:
            self._all = found
        if self.pdp is not None:
            self.pdp.set_layer("workspaces", [p for w in found.values() for p in compile_workspace(w)])
            self.pdp.invalidate_decisions()

    def list(self) -> list[WorkspaceIn]:
        with self._lock:
            return sorted(self._all.values(), key=lambda w: w.name.lower())

    def get(self, wid: str) -> WorkspaceIn:
        with self._lock:
            w = self._all.get(wid)
        if w is None:
            raise WorkspaceError("no such workspace", 404)
        return w

    def put(self, w: WorkspaceIn, actor: str, is_admin: bool) -> WorkspaceIn:
        """Global admins set anything; a workspace's own admins change its name, description and members."""
        with self._lock:
            before = self._all.get(w.id)
        if not is_admin:
            if before is None or not self.is_workspace_admin(actor, before):
                raise WorkspaceError("only an admin of this workspace may change it", 403)
            fixed = ("domains", "admins", "admin_permissions", "member_permissions")
            if any(getattr(w, k) != getattr(before, k) for k in fixed):
                raise WorkspaceError("only a global admin changes a workspace's domains, admins or permissions", 403)
        else:
            taken = {d: o.id for o in self.list() if o.id != w.id for d in o.domains}
            clash = sorted(d for d in w.domains if d in taken)
            if clash:
                raise WorkspaceError(f"{clash[0]} already belongs to the workspace {taken[clash[0]]}", 409)
        with self.engine.begin() as conn:
            conn.execute(workspaces_table.delete().where(workspaces_table.c.id == w.id))
            conn.execute(workspaces_table.insert().values(id=w.id, doc=w.model_dump(), updated_ms=int(time.time() * 1000),
                                                          updated_by=actor))
        if self.audit is not None:
            self.audit.record("governance", "workspace.set" if before else "workspace.created", actor,
                              f"workspace:{w.id}", w.model_dump())
        self._assets.pop(w.id, None)
        self.reload()
        return w

    def delete(self, wid: str, actor: str) -> None:
        self.get(wid)
        with self.engine.begin() as conn:
            conn.execute(workspaces_table.delete().where(workspaces_table.c.id == wid))
        if self.audit is not None:
            self.audit.record("governance", "workspace.removed", actor, f"workspace:{wid}", {})
        self.reload()

    # ---- people --------------------------------------------------------------------------------
    def _groups(self, actor: str) -> set[str]:
        if self.attributes is None:
            return set()
        try:
            return set(self.attributes.subject(actor).get("groups") or [])
        except Exception:  # noqa: BLE001 - without groups, direct membership still counts
            return set()

    @staticmethod
    def _in(actor: str, groups: set[str], people: list[str]) -> bool:
        return actor in people or bool(groups & set(people))

    def is_workspace_admin(self, actor: str, w: WorkspaceIn) -> bool:
        return self._in(actor, self._groups(actor), w.admins)

    def roles(self, actor: str, groups: Optional[set[str]] = None) -> dict[str, str]:
        """``{workspace id: "admin" | "member"}`` for the caller."""
        groups = self._groups(actor) if groups is None else groups
        out = {}
        for w in self.list():
            if self._in(actor, groups, w.admins):
                out[w.id] = "admin"
            elif self._in(actor, groups, w.members):
                out[w.id] = "member"
        return out

    def enrich_subject(self, subject: dict, now_ms: int) -> dict:
        roles = self.roles(subject.get("urn") or "", set(subject.get("groups") or []))
        return {"workspaces": sorted(roles), "workspace_admin": sorted(k for k, v in roles.items() if v == "admin")}

    def mine(self, actor: str) -> list[dict]:
        roles = self.roles(actor)
        return [{"id": w.id, "name": w.name, "domains": w.domains, "role": roles[w.id]} for w in self.list() if w.id in roles]

    def workspace_of(self, urn: str) -> Optional[str]:
        """The workspace an asset belongs to, by its domain."""
        domains = set(((self.graph.get_aspect(urn, "domains") or {}).get("domains") or []) if self.graph else [])
        return next((w.id for w in self.list() if domains & set(w.domains)), None)

    # ---- contents ------------------------------------------------------------------------------
    def assets(self, wid: str) -> list[dict]:
        w = self.get(wid)
        hit = self._assets.get(wid)
        if hit and time.time() - hit[0] < self.ASSETS_TTL_SECONDS:
            return hit[1]
        rows = []
        wanted = set(w.domains)
        for kind in ("dataset", "dataProduct", "dashboard", "chart", "glossaryTerm"):
            for r in self.graph.scroll_entities(kind, ["domains", "ownership"]) if self.graph else []:
                ds = set((r.get("domains") or {}).get("domains") or [])
                if ds & wanted:
                    owners = [o.get("owner") for o in (r.get("ownership") or {}).get("owners") or [] if o.get("owner")]
                    rows.append({"urn": r["urn"], "type": kind, "domain": sorted(ds & wanted)[0], "owners": owners})
        rows.sort(key=lambda r: (r["type"], r["urn"]))
        self._assets[wid] = (time.time(), rows)
        return rows

    def summary(self, wid: str, actor: Optional[str] = None) -> dict:
        w = self.get(wid)
        rows = self.assets(wid)
        by_type: dict[str, int] = {}
        for r in rows:
            by_type[r["type"]] = by_type.get(r["type"], 0) + 1
        unowned = sum(1 for r in rows if not r["owners"])
        out = {**w.model_dump(), "assets": by_type, "unowned": unowned,
               "owners": len({o for r in rows for o in r["owners"]})}
        if actor is not None:
            out["role"] = self.roles(actor).get(wid)
        return out
