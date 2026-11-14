"""People & roles: one administrator's view of who is in Cairn, what they hold, and why.

People and groups live in Cairn Core (``corpuser``, ``corpGroup``), where they arrive three ways:

* **SCIM** (:mod:`cairn.identity.scim`): the identity provider pushes them. Cairn never changes them by hand: every
  change here to a SCIM person or group is refused (409, "managed by your identity provider").
* **Manual**: made here, for deployments without SCIM (``POST /v1/people/users``, ``/v1/people/groups``), and kept as
  configuration (resource types ``people``, ``people_groups``, ``people_group_members``). Their bookkeeping is the
  table ``cairn_people``; the people themselves are written to Cairn Core as the provisioning account, the same
  account SCIM writes with, never as the control plane's.
* **SSO**: anyone else, made in Cairn Core when they first signed in (just in time) or in Cairn Core's own pages.
  Cairn may adopt one (create it here with the same id: from then on it is manual) or deactivate one.

Manual groups are native groups (``origin`` NATIVE, membership in each member's ``nativeGroupMembership``), so a
person's groups from single sign-on (``groupMembership``, replaced at each sign-in) never overwrite them.

A person's attributes are what ABAC reads (:class:`cairn.governance.attributes.AttributeResolver`): title, department,
country and email from ``corpUserInfo``; employment type and any other attribute as a structured property
``io.cairn.<name>`` on the person (``subject.attributes.<name>``). Deactivating a person suspends them in Cairn Core and
ends every session and token Cairn issued them, the way SCIM deprovisioning does.

Reading needs ``MANAGE_POLICIES`` (who holds what is an administrator's question); so does every change. Every change
and every access check is audited (category ``people``). Cairn Core unreadable means a 503, never a guess.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import BigInteger, Boolean, Column, String, Table, and_, select

from cairn.common.db import JSONText, metadata
from cairn.common.proposals import make_mcp

USER, GROUP = "urn:li:corpuser:", "urn:li:corpGroup:"
PROP = "urn:li:structuredProperty:"
ATTR_NS = "io.cairn."
ACTOR = "urn:li:corpuser:__datahub_system"
MANAGED_BY_IDP = "managed by your identity provider (SCIM): change it there"
# Cairn's and Cairn Core's own accounts: listed as service accounts, never as people.
SERVICE_ACCOUNTS = {"cairn-service": "Cairn's control plane in Cairn Core",
                    "cairn-provisioner": "writes people and groups (SCIM, People & roles)",
                    "__datahub_system": "Cairn Core's own system account",
                    "__cairn_service": "Cairn's in-process service account",
                    "cairn-catalog": "Trino's catalog crawler", "cairn-catalogs": "Trino's catalog administrator"}
USER_ASPECTS = ("corpUserInfo", "corpUserStatus", "groupMembership", "nativeGroupMembership", "structuredProperties",
                "status")
GROUP_ASPECTS = ("corpGroupInfo", "origin", "status")
_UID = re.compile(r"^[A-Za-z0-9._@+-]{1,128}$")
_GID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_ATTR = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_PRIV = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
RESERVED_ATTRS = {"department", "title", "country", "email", "display_name", "employment_type"}
MAX_ATTRS = 20

people_table = Table(
    "cairn_people", metadata,
    Column("kind", String(8), primary_key=True),                 # user | group
    Column("id", String(255), primary_key=True),
    Column("doc", JSONText, nullable=False),                     # what Cairn manages: the profile, or name/description
    Column("active", Boolean, nullable=False),
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_by", String(512), nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)


class PeopleError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- requests
class PersonUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    email: Optional[str] = Field(default=None, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    first_name: Optional[str] = Field(default=None, max_length=100)
    last_name: Optional[str] = Field(default=None, max_length=100)
    title: Optional[str] = Field(default=None, max_length=200)
    department: Optional[str] = Field(default=None, max_length=200)
    country: Optional[str] = Field(default=None, pattern=r"^[A-Z]{2}$", description="ISO 3166-1 alpha-2, e.g. DE")
    employment_type: Optional[str] = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,31}$",
                                           description="e.g. employee, contractor, intern")
    manager: Optional[str] = Field(default=None, pattern=r"^urn:li:corpuser:[A-Za-z0-9._@+-]{1,128}$")
    attributes: dict[str, str] = Field(default_factory=dict, description="more attributes ABAC reads: name → value")

    @field_validator("attributes")
    @classmethod
    def _attributes(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > MAX_ATTRS:
            raise ValueError(f"at most {MAX_ATTRS} attributes")
        for k, val in v.items():
            if not _ATTR.match(k):
                raise ValueError(f"attribute {k!r}: lowercase letters, digits and _, starting with a letter")
            if k in RESERVED_ATTRS:
                raise ValueError(f"attribute {k!r} has its own field")
            if not isinstance(val, str) or not 0 < len(val) <= 200:
                raise ValueError(f"attribute {k!r}: a value of 1 to 200 characters")
        return dict(sorted(v.items()))


class PersonIn(PersonUpdate):
    id: str = Field(pattern=_UID.pattern, description="the user name: urn:li:corpuser:<id>")


class GroupUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class GroupIn(GroupUpdate):
    id: str = Field(pattern=_GID.pattern, description="lowercase letters, digits and . _ -: urn:li:corpGroup:<id>")


class MemberIn(BaseModel):
    person: str = Field(description="the person's id, or urn:li:corpuser:<id>")


class AccessCheck(BaseModel):
    person: str = Field(description="the person's id, or urn:li:corpuser:<id>")
    privilege: str = Field(default="VIEW_ENTITY_PAGE", pattern=_PRIV.pattern)
    resource: Optional[str] = Field(default=None, max_length=1024, pattern=r"^urn:li:[A-Za-z]+:.+$",
                                    description="the resource's urn; none for a platform privilege")


def uid_of(person: str) -> str:
    uid = person[len(USER):] if person.startswith(USER) else person
    if not _UID.match(uid):
        raise PeopleError(f"not a person: {person}", 404)
    return uid


def gid_of(group: str) -> str:
    return group[len(GROUP):] if group.startswith(GROUP) else group


def _short(urn: str) -> str:
    return urn.split(":", 3)[-1]


# --------------------------------------------------------------------------- the directory
class PeopleDirectory:
    CACHE_SECONDS = 30.0

    def __init__(self, engine, graph, writer=None, schema_graph=None, audit=None, attributes=None, pdp=None,
                 personas=None, workspaces=None, agents=None, access_reviews=None,
                 on_deactivate: Optional[Callable[[str], Any]] = None, clock: Callable[[], int] = None):
        """``graph`` reads Cairn Core; ``writer`` writes people and groups (the provisioning account); ``schema_graph``
        declares the attributes' structured properties (the control plane, which may manage them)."""
        self.engine, self.graph, self.audit = engine, graph, audit
        self.writer = writer or graph
        self.schema_graph = schema_graph or graph
        self.attributes, self.pdp, self.personas = attributes, pdp, personas
        self.workspaces, self.agents, self.access_reviews = workspaces, agents, access_reviews
        self.on_deactivate = on_deactivate
        self.clock = clock or (lambda: int(time.time() * 1000))
        self._lock = threading.Lock()
        self._snap: Optional[tuple[float, dict]] = None
        self._defined: set[str] = set()

    # ---- reading Cairn Core ------------------------------------------------------------------------
    def _require_graph(self):
        if self.graph is None:
            raise PeopleError("Cairn Core is not configured: people and groups live there", 503)

    def _snapshot(self) -> dict:
        """Every person and group in Cairn Core, read at most every CACHE_SECONDS (changes here drop it)."""
        self._require_graph()
        with self._lock:
            if self._snap and self._snap[0] > time.monotonic():
                return self._snap[1]
        try:
            users = self.graph.scroll_entities("corpuser", list(USER_ASPECTS))
            groups = self.graph.scroll_entities("corpGroup", list(GROUP_ASPECTS))
        except Exception as e:  # noqa: BLE001 - fail closed: an unreadable directory is an error, not an empty one
            raise PeopleError(f"Cairn Core cannot be read now: {e}", 503) from e
        snap = {"users": {r["urn"]: r for r in users if r.get("urn", "").startswith(USER)
                          and not (r.get("status") or {}).get("removed")},
                "groups": {r["urn"]: r for r in groups if r.get("urn", "").startswith(GROUP)
                           and not (r.get("status") or {}).get("removed")}}
        with self._lock:
            self._snap = (time.monotonic() + self.CACHE_SECONDS, snap)
        return snap

    def _changed(self, *urns: str) -> None:
        with self._lock:
            self._snap = None
        for urn in urns:
            if self.attributes is not None:
                self.attributes.invalidate(urn)
        if self.pdp is not None:
            self.pdp.invalidate_decisions()

    def _get(self, urn: str, aspect: str) -> Optional[dict]:
        self._require_graph()
        try:
            return self.graph.get_aspect(urn, aspect)
        except Exception as e:  # noqa: BLE001
            raise PeopleError(f"Cairn Core cannot be read now: {e}", 503) from e

    def _emit(self, graph, mcps: list[dict]) -> None:
        try:
            graph.emit(mcps)
        except Exception as e:  # noqa: BLE001 - nothing half-done is reported as done
            raise PeopleError(f"Cairn Core refused the change: {e}", 502) from e

    # ---- bookkeeping -------------------------------------------------------------------------------
    def _rows(self, kind: str) -> dict[str, dict]:
        with self.engine.connect() as c:
            return {r.id: dict(r._mapping) for r in c.execute(select(people_table).where(people_table.c.kind == kind))}

    def _row(self, kind: str, rid: str) -> Optional[dict]:
        with self.engine.connect() as c:
            r = c.execute(select(people_table).where(and_(people_table.c.kind == kind, people_table.c.id == rid))).first()
        return dict(r._mapping) if r else None

    def _scim(self, rtype: str) -> set[str]:
        from cairn.identity.scim import scim_resources

        with self.engine.connect() as c:
            return {r.id for r in c.execute(select(scim_resources.c.id).where(scim_resources.c.resource_type == rtype))}

    def _is_scim(self, rtype: str, rid: str) -> bool:
        from cairn.identity.scim import scim_resources

        with self.engine.connect() as c:
            return c.execute(select(scim_resources.c.id).where(and_(
                scim_resources.c.resource_type == rtype, scim_resources.c.id == rid))).first() is not None

    def _save(self, kind: str, rid: str, doc: dict, active: bool, actor: str, conn) -> bool:
        now = self.clock()
        cur = conn.execute(select(people_table.c.id).where(and_(people_table.c.kind == kind,
                                                                 people_table.c.id == rid))).first()
        if cur is None:
            conn.execute(people_table.insert().values(kind=kind, id=rid, doc=doc, active=active, created_by=actor,
                                                      created_ms=now, updated_by=actor, updated_ms=now))
            return True
        conn.execute(people_table.update().where(and_(people_table.c.kind == kind, people_table.c.id == rid))
                     .values(doc=doc, active=active, updated_by=actor, updated_ms=now))
        return False

    def _record(self, action: str, actor: str, subject: str, details: dict, conn=None) -> None:
        if self.audit is not None:
            self.audit.record("people", action, actor, subject, details, conn=conn)

    # ---- shaping ------------------------------------------------------------------------------------
    @staticmethod
    def _props(row: dict) -> dict[str, str]:
        out = {}
        for p in (row.get("structuredProperties") or {}).get("properties") or []:
            name = str(p.get("propertyUrn", "")).removeprefix(PROP)
            if not name.startswith(ATTR_NS):
                continue
            vals = [v.get("string", v.get("double")) if isinstance(v, dict) else v for v in p.get("values") or []]
            if vals and vals[0] is not None:
                out[name[len(ATTR_NS):]] = str(vals[0])
        return out

    def _service_ids(self) -> set[str]:
        from cairn.identity.workload import workload_bindings

        with self.engine.connect() as c:
            bound = {_short(r.user) for r in c.execute(select(workload_bindings.c.user))}
        return set(SERVICE_ACCOUNTS) | bound

    def _person(self, urn: str, row: dict, scim: set[str], manual: dict[str, dict], services: set[str]) -> dict:
        uid = _short(urn)
        info = row.get("corpUserInfo") or {}
        status = str((row.get("corpUserStatus") or {}).get("status") or "ACTIVE").upper()
        active = status != "SUSPENDED" and bool(info.get("active", True))
        props = self._props(row)
        groups = sorted(set((row.get("groupMembership") or {}).get("groups") or [])
                        | set((row.get("nativeGroupMembership") or {}).get("nativeGroups") or []))
        source = "scim" if uid in scim else "manual" if uid in manual else "sso"
        return {"id": uid, "urn": urn, "type": "service" if uid in services else "person",
                "display_name": info.get("displayName") or info.get("fullName") or uid,
                "email": info.get("email"), "first_name": info.get("firstName"), "last_name": info.get("lastName"),
                "title": info.get("title"), "department": info.get("departmentName"), "country": info.get("countryCode"),
                "employment_type": props.pop("employment_type", None), "manager": info.get("managerUrn"),
                "attributes": props, "status": "active" if active else "deactivated", "source": source,
                "groups": groups}

    def _group(self, urn: str, row: dict, scim: set[str], manual: dict[str, dict], counts: dict[str, int]) -> dict:
        gid = _short(urn)
        info = row.get("corpGroupInfo") or {}
        origin = str((row.get("origin") or {}).get("type") or "").upper()
        source = "scim" if gid in scim else "manual" if gid in manual else "sso" if origin == "EXTERNAL" else "core"
        return {"id": gid, "urn": urn, "name": info.get("displayName") or gid, "description": info.get("description") or "",
                "email": info.get("email"), "source": source, "members": counts.get(urn, 0)}

    # ---- people -------------------------------------------------------------------------------------
    def list_people(self, q: str = "", status: str = "all", source: str = "", group: str = "", type_: str = "all",
                    offset: int = 0, limit: int = 50) -> dict:
        if status not in ("all", "active", "deactivated") or source not in ("", "scim", "manual", "sso") \
                or type_ not in ("all", "person", "service"):
            raise PeopleError("status: all, active or deactivated; source: scim, manual or sso; type: all, person or "
                              "service")
        snap = self._snapshot()
        scim, manual, services = self._scim("User"), self._rows("user"), self._service_ids()
        want_group = (group if group.startswith(GROUP) else GROUP + group) if group else ""
        needle = q.strip().lower()
        items = []
        for urn, row in snap["users"].items():
            p = self._person(urn, row, scim, manual, services)
            if (status != "all" and p["status"] != status) or (source and p["source"] != source) \
                    or (type_ != "all" and p["type"] != type_) or (want_group and want_group not in p["groups"]):
                continue
            if needle and not any(needle in str(p.get(k) or "").lower() for k in ("id", "display_name", "email", "title",
                                                                                  "department")):
                continue
            items.append(p)
        items.sort(key=lambda p: (p["display_name"].lower(), p["id"]))
        offset, limit = max(offset, 0), min(max(limit, 1), 5000)
        return {"total": len(items), "offset": offset, "limit": limit, "items": items[offset:offset + limit]}

    def _user_row(self, uid: str) -> tuple[str, dict]:
        urn = USER + uid
        row = self._snapshot()["users"].get(urn)
        if row is None:                                   # made a moment ago, after the snapshot: read it directly
            info = self._get(urn, "corpUserInfo")
            if info is None:
                raise PeopleError(f"no person {uid}", 404)
            row = {"urn": urn, **{a: self._get(urn, a) for a in USER_ASPECTS}}
        return urn, row

    def person(self, person: str) -> dict:
        uid = uid_of(person)
        urn, row = self._user_row(uid)
        manual = self._rows("user")
        p = self._person(urn, row, self._scim("User"), manual, self._service_ids())
        snap = self._snapshot()
        scim_groups, manual_groups = self._scim("Group"), self._rows("group")
        p["groups"] = [self._group(g, snap["groups"].get(g) or {"urn": g}, scim_groups, manual_groups, {})
                       for g in p["groups"]]
        now = self.clock()
        subject = self._subject(urn)
        p["personas"] = self.personas.explain_memberships(subject, now) if self.personas is not None else []
        p["persona_grants"] = self.personas.grants_of(urn) if self.personas is not None else []
        p["workspaces"] = [{"id": w["id"], "name": w["name"], "role": w["role"], "domains": w["domains"]}
                           for w in self.workspaces.mine(urn)] if self.workspaces is not None else []
        p["owns"] = self._owned(urn)
        p["last_sign_in"] = self.audit.last_sign_in(urn) if self.audit is not None else None
        p["open_access_reviews"] = self.access_reviews.open_items_for(urn) if self.access_reviews is not None else []
        p["recent_audit"] = self.audit.about(urn, 20) if self.audit is not None else []
        p["managed"] = {"row": manual.get(uid) is not None,
                        "editable": p["source"] != "scim",
                        "why": MANAGED_BY_IDP if p["source"] == "scim" else None}
        return p

    def _subject(self, urn: str) -> dict:
        """The subject the policy engine decides with (groups, attributes), as it is now."""
        if self.attributes is None:
            return {"urn": urn, "groups": [], "attributes": {}}
        self.attributes.invalidate(urn)          # an administrator's question: the answer as Cairn Core has it now
        return dict(self.attributes.subject(urn))

    def _owned(self, urn: str) -> list[dict]:
        out: list[dict] = []
        if self.agents is not None:
            for a in self.agents.list():
                if a.get("owner") == urn or (a.get("kind") == "key" and a.get("created_by") == urn and not a.get("owner")):
                    out.append({"kind": "agent", "id": a["id"], "name": a["name"], "agent_kind": a.get("kind"),
                                "scopes": a.get("scopes", []), "enabled": a.get("enabled"),
                                "last_used_ms": a.get("last_used_ms")})
        from cairn.identity.workload import workload_bindings

        with self.engine.connect() as c:
            for r in c.execute(select(workload_bindings).where(workload_bindings.c.created_by == urn)):
                out.append({"kind": "workload", "id": r.id, "name": f"{r.issuer}: {r.subject}", "user": r.user})
        return out

    # ---- groups -------------------------------------------------------------------------------------
    def _member_counts(self, snap: dict) -> dict[str, list[str]]:
        members: dict[str, list[str]] = {}
        for urn, row in snap["users"].items():
            for g in set((row.get("groupMembership") or {}).get("groups") or []) \
                    | set((row.get("nativeGroupMembership") or {}).get("nativeGroups") or []):
                members.setdefault(g, []).append(urn)
        return members

    def list_groups(self, q: str = "", source: str = "", offset: int = 0, limit: int = 50) -> dict:
        if source not in ("", "scim", "manual", "sso", "core"):
            raise PeopleError("source: scim, manual, sso or core")
        snap = self._snapshot()
        members = self._member_counts(snap)
        counts = {g: len(m) for g, m in members.items()}
        scim, manual = self._scim("Group"), self._rows("group")
        needle = q.strip().lower()
        urns = set(snap["groups"]) | {GROUP + g for g in manual}
        items = []
        for urn in urns:
            g = self._group(urn, snap["groups"].get(urn) or {"urn": urn}, scim, manual, counts)
            if source and g["source"] != source:
                continue
            if needle and needle not in g["id"].lower() and needle not in g["name"].lower():
                continue
            items.append(g)
        items.sort(key=lambda g: (g["name"].lower(), g["id"]))
        offset, limit = max(offset, 0), min(max(limit, 1), 5000)
        return {"total": len(items), "offset": offset, "limit": limit, "items": items[offset:offset + limit]}

    def group(self, group: str) -> dict:
        gid = gid_of(group)
        urn = GROUP + gid
        snap = self._snapshot()
        row = snap["groups"].get(urn)
        if row is None:
            info = self._get(urn, "corpGroupInfo")
            if info is None:
                raise PeopleError(f"no group {gid}", 404)
            row = {"urn": urn, "corpGroupInfo": info, "origin": self._get(urn, "origin")}
        members = self._member_counts(snap).get(urn, [])
        g = self._group(urn, row, self._scim("Group"), self._rows("group"), {urn: len(members)})
        scim, manual, services = self._scim("User"), self._rows("user"), self._service_ids()
        g["member_list"] = sorted(({k: v for k, v in self._person(m, snap["users"][m], scim, manual, services).items()
                                    if k in ("id", "urn", "display_name", "email", "status", "source", "type")}
                                   for m in members), key=lambda x: x["display_name"].lower())
        g["personas"] = [{"id": p.id, "name": p.name} for p in (self.personas.list_personas() if self.personas else [])
                         if urn in p.members.groups]
        g["purposes"] = [{"id": p.id, "name": p.name} for p in (self.personas.list_purposes() if self.personas else [])
                         if urn in p.allowed.groups]
        g["workspaces"] = [{"id": w.id, "name": w.name, "role": "admin" if urn in w.admins else "member"}
                           for w in (self.workspaces.list() if self.workspaces else [])
                           if urn in w.admins or urn in w.members]
        g["managed"] = {"editable": g["source"] == "manual",
                        "why": MANAGED_BY_IDP if g["source"] == "scim" else None if g["source"] == "manual"
                        else "not managed in Cairn: create it here with the same id to manage it"}
        return g

    def members(self, group: str) -> list[dict]:
        """A manual group's members, as configuration (``people_group_members``): none for a group Cairn does not
        manage (or no longer has), whose members are not configuration here."""
        gid = gid_of(group)
        if self._row("group", gid) is None:
            return []
        urn = GROUP + gid
        users = self._snapshot()["users"]
        return [{"id": _short(m), "group": gid, "person": _short(m)} for m in sorted(users)
                if urn in ((users[m].get("nativeGroupMembership") or {}).get("nativeGroups") or [])]

    # ---- changes: people ----------------------------------------------------------------------------
    def _ensure_attribute(self, name: str) -> str:
        qn = ATTR_NS + name
        if qn in self._defined:
            return qn
        purn = PROP + qn
        if self._get_schema(purn) is None:
            self._emit(self.schema_graph, [make_mcp(purn, "propertyDefinition", {
                "qualifiedName": qn, "displayName": name.replace("_", " ").capitalize(),
                "valueType": "urn:li:dataType:datahub.string", "cardinality": "SINGLE",
                "entityTypes": ["urn:li:entityType:datahub.corpuser"],
                "description": "A person's attribute, read by Cairn's access policies (subject.attributes."
                               f"{name}). Set in People & roles."})])
        self._defined.add(qn)
        return qn

    def _get_schema(self, urn: str) -> Optional[dict]:
        try:
            return self.schema_graph.get_aspect(urn, "propertyDefinition")
        except Exception as e:  # noqa: BLE001
            raise PeopleError(f"Cairn Core cannot be read now: {e}", 503) from e

    def _write_person(self, urn: str, spec: PersonUpdate, before: dict, active: bool) -> None:
        info = dict(self._get(urn, "corpUserInfo") or {})
        fields = {"displayName": spec.display_name, "email": spec.email, "firstName": spec.first_name,
                  "lastName": spec.last_name, "title": spec.title, "departmentName": spec.department,
                  "countryCode": spec.country, "managerUrn": spec.manager}
        full = " ".join(x for x in (spec.first_name, spec.last_name) if x) or spec.display_name
        fields["fullName"] = full
        for k, v in fields.items():
            if v is None:
                info.pop(k, None)
            else:
                info[k] = v
        info["active"] = active
        wanted = {**spec.attributes, **({"employment_type": spec.employment_type} if spec.employment_type else {})}
        managed_before = set((before.get("attributes") or {})) | ({"employment_type"} if before.get("employment_type")
                                                                 else set())
        props = []
        for prop in (self._get(urn, "structuredProperties") or {}).get("properties") or []:
            name = str(prop.get("propertyUrn", "")).removeprefix(PROP)
            if name.startswith(ATTR_NS) and name[len(ATTR_NS):] in set(wanted) | managed_before:
                continue                                  # Cairn's: replaced below, or dropped when no longer wanted
            props.append(prop)
        for name, value in sorted(wanted.items()):
            props.append({"propertyUrn": PROP + self._ensure_attribute(name), "values": [{"string": value}]})
        stamp = {"time": self.clock(), "actor": ACTOR}
        self._emit(self.writer, [make_mcp(urn, "corpUserInfo", info),
                                 make_mcp(urn, "corpUserStatus", {"status": "ACTIVE" if active else "SUSPENDED",
                                                                  "lastModified": stamp}),
                                 make_mcp(urn, "structuredProperties", {"properties": props})])

    def _editable_person(self, uid: str) -> None:
        if self._is_scim("User", uid):
            raise PeopleError(f"{uid} is {MANAGED_BY_IDP}", 409)

    def create_person(self, spec: PersonIn, actor: str) -> dict:
        """A person made by hand; an SSO person with this id is adopted (managed here from now on); a deactivated
        manual person is activated again with this profile."""
        self._require_graph()
        self._editable_person(spec.id)
        urn = USER + spec.id
        row = self._row("user", spec.id)
        if row is not None and row["active"]:
            raise PeopleError(f"person {spec.id} already exists", 409)
        exists = self._get(urn, "corpUserInfo") is not None
        before = row["doc"] if row else {}
        self._write_person(urn, spec, before, True)
        doc = spec.model_dump(mode="json", exclude={"id"})
        with self.engine.begin() as conn:
            self._save("user", spec.id, doc, True, actor, conn)
            action = "person.activated" if row else "person.adopted" if exists else "person.created"
            self._record(action, actor, urn, doc, conn)
        self._changed(urn)
        return self.person(spec.id)

    def update_person(self, person: str, spec: PersonUpdate, actor: str) -> dict:
        uid = uid_of(person)
        self._editable_person(uid)
        row = self._row("user", uid)
        if row is None:
            raise PeopleError(f"{uid} is not managed in Cairn: create them here (with this id) to manage them", 409)
        urn = USER + uid
        self._write_person(urn, spec, row["doc"], row["active"])
        doc = spec.model_dump(mode="json")
        with self.engine.begin() as conn:
            self._save("user", uid, doc, row["active"], actor, conn)
            self._record("person.updated", actor, urn, {k: v for k, v in doc.items() if v != row["doc"].get(k)}, conn)
        self._changed(urn)
        return self.person(uid)

    def set_active(self, person: str, active: bool, actor: str) -> dict:
        """Deactivate (suspended in Cairn Core; every session and token Cairn issued them ends) or activate again.
        A SCIM person is refused; an SSO person can be deactivated (offboarding without SCIM)."""
        uid = uid_of(person)
        self._editable_person(uid)
        urn = USER + uid
        if self._get(urn, "corpUserInfo") is None:
            raise PeopleError(f"no person {uid}", 404)
        if uid in SERVICE_ACCOUNTS:
            raise PeopleError(f"{uid} is one of Cairn's own accounts", 409)
        if not active and urn == actor:
            raise PeopleError("you cannot deactivate yourself", 409)
        info = dict(self._get(urn, "corpUserInfo") or {})
        info["active"] = active
        stamp = {"time": self.clock(), "actor": ACTOR}
        self._emit(self.writer, [make_mcp(urn, "corpUserInfo", info),
                                 make_mcp(urn, "corpUserStatus", {"status": "ACTIVE" if active else "SUSPENDED",
                                                                  "lastModified": stamp})])
        ended = 0
        if not active and self.on_deactivate is not None:
            ended = self.on_deactivate(urn) or 0
        row = self._row("user", uid)
        with self.engine.begin() as conn:
            if row is not None:
                self._save("user", uid, row["doc"], active, actor, conn)
            self._record("person.activated" if active else "person.deactivated", actor, urn,
                         {"sessions_ended": ended} if not active else {}, conn)
        self._changed(urn)
        return self.person(uid)

    # ---- changes: groups ----------------------------------------------------------------------------
    def _editable_group(self, gid: str, must_be_manual: bool = True) -> dict:
        if self._is_scim("Group", gid):
            raise PeopleError(f"group {gid} is {MANAGED_BY_IDP}", 409)
        row = self._row("group", gid)
        if must_be_manual and row is None:
            raise PeopleError(f"group {gid} is not managed in Cairn: create it here (with this id) to manage it", 409)
        return row or {}

    def _write_group(self, gid: str, spec: GroupUpdate) -> None:
        urn = GROUP + gid
        info = dict(self._get(urn, "corpGroupInfo") or {})
        info.update({"displayName": spec.name, "admins": info.get("admins") or [], "members": info.get("members") or [],
                     "groups": info.get("groups") or []})
        if spec.description:
            info["description"] = spec.description
        else:
            info.pop("description", None)
        self._emit(self.writer, [make_mcp(urn, "corpGroupInfo", info), make_mcp(urn, "origin", {"type": "NATIVE"})])

    def create_group(self, spec: GroupIn, actor: str) -> dict:
        self._require_graph()
        self._editable_group(spec.id, must_be_manual=False)
        urn = GROUP + spec.id
        if self._row("group", spec.id) is not None:
            raise PeopleError(f"group {spec.id} already exists", 409)
        origin = str((self._get(urn, "origin") or {}).get("type") or "").upper()
        exists = self._get(urn, "corpGroupInfo") is not None
        if exists and origin == "EXTERNAL":
            raise PeopleError(f"group {spec.id} comes from your identity provider's sign-in: manage it there", 409)
        self._write_group(spec.id, spec)
        doc = spec.model_dump(mode="json", exclude={"id"})
        with self.engine.begin() as conn:
            self._save("group", spec.id, doc, True, actor, conn)
            self._record("group.adopted" if exists else "group.created", actor, urn, doc, conn)
        self._changed(urn)
        return self.group(spec.id)

    def update_group(self, group: str, spec: GroupUpdate, actor: str) -> dict:
        gid = gid_of(group)
        row = self._editable_group(gid)
        self._write_group(gid, spec)
        doc = spec.model_dump(mode="json")
        with self.engine.begin() as conn:
            self._save("group", gid, doc, True, actor, conn)
            self._record("group.updated", actor, GROUP + gid, {k: v for k, v in doc.items() if v != row["doc"].get(k)},
                         conn)
        self._changed(GROUP + gid)
        return self.group(gid)

    def delete_group(self, group: str, actor: str) -> None:
        gid = gid_of(group)
        self._editable_group(gid)
        urn = GROUP + gid
        members = [m for m in self._member_counts(self._snapshot()).get(urn, [])]
        for m in members:
            self._set_member(urn, m, False)
        self._emit(self.writer, [make_mcp(urn, "status", {"removed": True})])
        with self.engine.begin() as conn:
            conn.execute(people_table.delete().where(and_(people_table.c.kind == "group", people_table.c.id == gid)))
            self._record("group.deleted", actor, urn, {"members_removed": len(members)}, conn)
        self._changed(urn, *members)

    def _set_member(self, group_urn: str, user_urn: str, member: bool) -> bool:
        native = list((self._get(user_urn, "nativeGroupMembership") or {}).get("nativeGroups") or [])
        if member == (group_urn in native):
            return False
        native = [*native, group_urn] if member else [g for g in native if g != group_urn]
        self._emit(self.writer, [make_mcp(user_urn, "nativeGroupMembership", {"nativeGroups": native})])
        return True

    def add_member(self, group: str, person: str, actor: str) -> dict:
        gid, uid = gid_of(group), uid_of(person)
        self._editable_group(gid)
        urn, user = GROUP + gid, USER + uid
        if self._get(user, "corpUserInfo") is None:
            raise PeopleError(f"no person {uid}", 404)
        if self._set_member(urn, user, True):
            self._record("group.member_added", actor, user, {"group": urn})
            self._changed(urn, user)
        return {"id": uid, "group": gid, "person": uid}

    def remove_member(self, group: str, person: str, actor: str) -> None:
        gid, uid = gid_of(group), uid_of(person)
        self._editable_group(gid)
        urn, user = GROUP + gid, USER + uid
        if not self._set_member(urn, user, False):
            raise PeopleError(f"{uid} is not a member of {gid}", 404)
        self._record("group.member_removed", actor, user, {"group": urn})
        self._changed(urn, user)

    # ---- service accounts and agents ----------------------------------------------------------------
    def principals(self, type_: str = "all") -> list[dict]:
        if type_ not in ("all", "agent", "service", "workload"):
            raise PeopleError("type: all, agent, service or workload")
        out: list[dict] = []
        if type_ in ("all", "agent") and self.agents is not None:
            for a in self.agents.list():
                out.append({"type": "agent", "id": a["id"], "name": a["name"], "agent_kind": a.get("kind"),
                            "owner": a.get("owner"), "scopes": a.get("scopes", []), "enabled": a.get("enabled"),
                            "created_by": a.get("created_by"), "last_used_ms": a.get("last_used_ms"),
                            "ai_system": a.get("ai_system")})
        if type_ in ("all", "workload"):
            from cairn.identity.workload import workload_bindings

            with self.engine.connect() as c:
                for r in c.execute(select(workload_bindings).order_by(workload_bindings.c.issuer,
                                                                      workload_bindings.c.subject)):
                    out.append({"type": "workload", "id": r.id, "name": f"{r.issuer}: {r.subject}", "owner": r.user,
                                "scopes": [], "enabled": True, "created_by": r.created_by, "acts_as": r.user})
        if type_ in ("all", "service"):
            for p in self.list_people(type_="service", limit=5000)["items"]:
                out.append({"type": "service", "id": p["id"], "name": p["display_name"], "owner": None,
                            "scopes": [], "enabled": p["status"] == "active",
                            "purpose": SERVICE_ACCOUNTS.get(p["id"], "a pipeline's account (workload identity)")})
        return out

    # ---- effective access ---------------------------------------------------------------------------
    def _policy_label(self, policy_id: str, by_id: dict) -> dict:
        pol = by_id.get(policy_id)
        out = {"policy_id": policy_id, "source": getattr(pol, "source", "cairn"),
               "description": getattr(pol, "description", ""), "effect": getattr(pol, "effect", None)}
        if policy_id.startswith("persona:") and self.personas is not None:
            pid = policy_id.split(":", 2)[1]
            p = self.personas.get_persona(pid)
            out.update({"kind": "persona", "id": pid, "name": p.name if p else pid})
        elif policy_id.startswith("purpose:") and self.personas is not None:
            pid = policy_id.split(":", 2)[1]
            p = self.personas.get_purpose(pid)
            out.update({"kind": "purpose", "id": pid, "name": p.name if p else pid})
        elif policy_id == "builtin:root":
            out.update({"kind": "root", "name": "break-glass root user"})
        else:
            out.update({"kind": out["source"], "name": policy_id})
        return out

    def check(self, req: AccessCheck, actor: str) -> dict:
        """Can this person do this (to this resource)? The policy engine's decision, with every policy that decided it
        and the persona or purpose behind each. Nothing is recorded as the person's own; the check itself is audited."""
        from cairn.governance.abac import action_matches
        from cairn.governance.pdp import AuthorizeRequest, ResourceRef

        if self.pdp is None:
            raise PeopleError("the policy engine is not running here", 503)
        uid = uid_of(req.person)
        urn = USER + uid
        _, row = self._user_row(uid)
        p = self._person(urn, row, self._scim("User"), self._rows("user"), self._service_ids())
        if self.attributes is not None:
            self.attributes.invalidate(urn)
        resource = ResourceRef(type=req.resource.split(":", 3)[2], urn=req.resource) if req.resource else None
        d = self.pdp.decide(AuthorizeRequest(actor=urn, privilege=req.privilege, resource=resource,
                                             environment={"channel": "cairn-api"}), explain=True, record=False)
        by_id = {x.id: x for x in self.pdp.policies()}
        decided = [self._policy_label(i, by_id) for i in d.policy_ids]
        # The personas and purposes that could have applied to this privilege but did not, and why not.
        considered = []
        for r in d.trace or []:
            pol = by_id.get(r.policy_id)
            if r.matched or pol is None or pol.source not in ("persona", "purpose", "workspace") \
                    or not action_matches(pol.actions, req.privilege):
                continue
            considered.append({**self._policy_label(r.policy_id, by_id), "why_not": r.reason})
        allowed = d.decision == "ALLOW"
        what = f"{req.privilege} on {req.resource}" if req.resource else req.privilege
        if allowed:
            first = decided[0] if decided else {"kind": "policy", "name": "a policy"}
            summary = f"Yes: {p['display_name']} may {what}, allowed by {first['kind']} {first['name']}."
        elif d.decision == "DENY" and d.policy_ids:
            first = decided[0]
            summary = f"No: {p['display_name']} may not {what}: denied by {first['kind']} {first['name']}."
        elif d.decision == "DENY":
            summary = f"No: no policy grants {p['display_name']} {what}."
        else:
            summary = (f"No, in Cairn: no Cairn policy grants {p['display_name']} {what}. In the catalog's own pages, "
                       "Cairn Core's policies decide it (the policy engine runs additively).")
        if p["status"] != "active":
            summary += " They are deactivated: they cannot sign in."
        subject = self.pdp.build_context(AuthorizeRequest(actor=urn, privilege=req.privilege))["subject"]
        out = {"person": {k: p[k] for k in ("id", "urn", "display_name", "status", "source")},
               "privilege": req.privilege, "resource": req.resource, "allowed": allowed, "decision": d.decision,
               "reason": d.reason, "summary": summary, "because": decided, "considered": considered,
               "obligations": [o.model_dump(mode="json") for o in d.obligations],
               "subject": {"groups": subject.get("groups", []), "personas": subject.get("personas", []),
                           "workspaces": subject.get("workspaces", []), "attributes": subject.get("attributes", {})}}
        self._record("access.checked", actor, urn, {"privilege": req.privilege, "resource": req.resource,
                                                    "decision": d.decision})
        return out

    def reach(self, person: str) -> dict:
        """What this person can see and do, by persona scope: each persona they hold with its permissions and the part
        of the catalog it reaches; the purposes that restrict them; their workspaces."""
        from cairn.governance.personas import privileges_for

        uid = uid_of(person)
        urn, _row = self._user_row(uid)
        subject = self._subject(urn)
        now = self.clock()
        held = self.personas.explain_memberships(subject, now) if self.personas is not None else []
        personas = []
        for h in held:
            p = self.personas.get_persona(h["id"])
            scope = p.scope.model_dump(mode="json")
            parts = [f"{k.replace('_', ' ')}: {', '.join(_short(x) for x in v)}" for k, v in scope.items() if v]
            personas.append({**h, "privileges": privileges_for(p.permissions), "scope": scope,
                             "scope_text": "; ".join(parts) or "the whole catalog"})
        mine = {h["id"] for h in held}
        groups = set(subject.get("groups") or [])
        purposes = []
        for pu in (self.personas.list_purposes() if self.personas is not None else []):
            if not pu.enabled:
                continue
            inside = urn in pu.allowed.users or bool(groups & set(pu.allowed.groups)) \
                or bool(mine & set(pu.allowed.personas))
            effects = []
            if not inside and pu.deny:
                effects.append(f"denied {', '.join(pu.deny)}")
            if not inside and pu.mask.enabled:
                effects.append(f"values masked ({pu.mask.strategy})")
            purposes.append({"id": pu.id, "name": pu.name, "labels": pu.tags, "allowed": inside,
                             "effect": "; ".join(effects) or "none"})
        workspaces = []
        if self.workspaces is not None:
            roles = self.workspaces.roles(urn, groups)
            for w in self.workspaces.list():
                if w.id in roles:
                    workspaces.append({"id": w.id, "name": w.name, "role": roles[w.id], "domains": w.domains,
                                       "permissions": w.admin_permissions if roles[w.id] == "admin"
                                       else w.member_permissions})
        return {"person": urn, "personas": personas, "purposes": purposes, "workspaces": workspaces,
                "attributes": subject.get("attributes", {}), "groups": sorted(groups)}


# --------------------------------------------------------------------------- HTTP
def build_router(people: PeopleDirectory, current_principal, authorize):
    from fastapi import APIRouter, Body, Depends, HTTPException, Query
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/people", tags=["people"])

    def admin(p=Depends(current_principal)):
        authorize(p, "MANAGE_POLICIES", None)       # fail closed: raises 403 unless the policy engine says yes
        return p

    async def call(fn, *a, **kw):
        try:
            return await run_in_threadpool(lambda: fn(*a, **kw))
        except PeopleError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/users")
    async def list_users(q: str = "", status: str = "all", source: str = "", group: str = "",
                         type: str = Query("all", description="all, person or service"),  # noqa: A002
                         offset: int = 0, limit: int = 50, _p=Depends(admin)):
        """People in Cairn Core, with where each comes from (scim, manual, sso), searched and filtered, a page at a
        time."""
        return await call(people.list_people, q, status, source, group, type, offset, limit)

    @router.post("/users", status_code=201)
    async def create_user(body: PersonIn = Body(...), p=Depends(admin)):
        return await call(people.create_person, body, p.urn)

    @router.get("/users/{uid}")
    async def get_user(uid: str, _p=Depends(admin)):
        return await call(people.person, uid)

    @router.put("/users/{uid}")
    async def put_user(uid: str, body: PersonUpdate = Body(...), p=Depends(admin)):
        return await call(people.update_person, uid, body, p.urn)

    @router.delete("/users/{uid}", status_code=204)
    async def deactivate_user(uid: str, p=Depends(admin)):
        """Deactivates (people keep their history and ownership): suspended in Cairn Core, every session ended."""
        await call(people.set_active, uid, False, p.urn)

    @router.post("/users/{uid}/activate")
    async def activate_user(uid: str, p=Depends(admin)):
        return await call(people.set_active, uid, True, p.urn)

    @router.get("/users/{uid}/access")
    async def user_access(uid: str, _p=Depends(admin)):
        return await call(people.reach, uid)

    @router.get("/groups")
    async def list_groups(q: str = "", source: str = "", offset: int = 0, limit: int = 50, _p=Depends(admin)):
        return await call(people.list_groups, q, source, offset, limit)

    @router.post("/groups", status_code=201)
    async def create_group(body: GroupIn = Body(...), p=Depends(admin)):
        return await call(people.create_group, body, p.urn)

    @router.get("/groups/{gid}")
    async def get_group(gid: str, _p=Depends(admin)):
        return await call(people.group, gid)

    @router.put("/groups/{gid}")
    async def put_group(gid: str, body: GroupUpdate = Body(...), p=Depends(admin)):
        return await call(people.update_group, gid, body, p.urn)

    @router.delete("/groups/{gid}", status_code=204)
    async def delete_group(gid: str, p=Depends(admin)):
        await call(people.delete_group, gid, p.urn)

    @router.get("/groups/{gid}/members")
    async def list_members(gid: str, _p=Depends(admin)):
        return await call(people.members, gid)

    @router.post("/groups/{gid}/members", status_code=201)
    async def add_member(gid: str, body: MemberIn = Body(...), p=Depends(admin)):
        return await call(people.add_member, gid, body.person, p.urn)

    @router.delete("/groups/{gid}/members/{uid}", status_code=204)
    async def remove_member(gid: str, uid: str, p=Depends(admin)):
        await call(people.remove_member, gid, uid, p.urn)

    @router.get("/principals")
    async def principals(type: str = "all", _p=Depends(admin)):  # noqa: A002
        """Service accounts, pipelines' workload identities and agent identities, with owner and scopes."""
        return await call(people.principals, type)

    @router.post("/access-check")
    async def access_check(body: AccessCheck = Body(...), p=Depends(admin)):
        """Can <person> <privilege> <resource>? Yes or no, and the policies, personas and purposes behind it."""
        return await call(people.check, body, p.urn)

    return router
