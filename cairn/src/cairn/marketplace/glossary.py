"""The business glossary edited through Cairn (``/catalog/glossary``), written to Cairn Core.

Term groups are Cairn Core ``glossaryNode`` entities and terms ``glossaryTerm`` entities: their properties, owners,
stewards, related terms and deprecation are aspects in the metadata graph, so search, the asset pages, lineage and
every other reader of the catalog see a change at once. Cairn keeps no copy; it reads Core and writes Core.

* **Who may change the glossary:** ``MANAGE_GLOSSARIES`` (Cairn Core's privilege name) on the term or group, or on any
  group above it: a policy whose resources are a term group scopes it to everything under that group; one without
  resources covers the whole glossary. Moving an entry needs the privilege where it is and where it goes.
* **Applying a term to an asset** needs ``EDIT_ENTITY_GLOSSARY_TERMS`` on the asset (on a column:
  ``EDIT_DATASET_COL_GLOSSARY_TERMS``), and the caller must be able to see the asset. A deprecated term is not
  applied again; it can still be removed.
* **Deleting** a term or group marks it removed (Core's ``status`` aspect): it leaves search and the glossary, and
  making it again with the same id brings it back. A group with terms or groups in it is not deleted; a term still on
  assets is deleted only with ``force``, which removes it from them first (each removal is checked as above).
* **Proposals:** someone who may not change a term can propose the change; it becomes a change request
  (``/v1/iac/changes``, kind ``glossary_terms``) that a person with ``APPROVE_IAC_CHANGE`` on that kind, who may
  make the change themselves, approves and applies.

Every write is audited after Cairn Core confirmed it; a write Core refused is audited as failed and answered 502.
What Cairn does not manage on an entry made in Core's own UI (other ownership types' details, a term's source, related
term values, other fields of its properties) is kept as it is.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Iterable, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from cairn.common.proposals import field_label_op, make_mcp, patch_mcp, pointer

MANAGE = "MANAGE_GLOSSARIES"
EDIT_TERMS = "EDIT_ENTITY_GLOSSARY_TERMS"
EDIT_COLUMN_TERMS = "EDIT_DATASET_COL_GLOSSARY_TERMS"
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,199}$")
OWNER = re.compile(r"^urn:li:(corpuser|corpGroup):[^\s]+$")
ASSET = re.compile(r"^urn:li:[A-Za-z]+:.+$")
STEWARD = "DATA_STEWARD"
OWNER_TYPE = "TECHNICAL_OWNER"
MAX_CONTENTS = 10_000            # Core's search window: more assets than this are refused, not half-handled
NODE_ASPECTS = ["glossaryNodeInfo", "ownership", "status"]
TERM_ASPECTS = ["glossaryTermInfo", "glossaryRelatedTerms", "ownership", "deprecation", "status"]
MAX_DEPTH = 32

# Cairn Core's search, for the assets that carry a term or sit in a domain (names for the answer).
SEARCH = """query cairnCatalogAssets($input: SearchAcrossEntitiesInput!) {
  searchAcrossEntities(input: $input) {
    total
    searchResults { entity { urn type
      ... on Dataset { name properties { name } }
      ... on Dashboard { properties { name } }
      ... on Chart { properties { name } }
      ... on DataFlow { properties { name } }
      ... on DataJob { properties { name } }
      ... on DataProduct { properties { name } }
      ... on MLModel { name }
      ... on Container { properties { name } } } }
  }
}"""


class EditError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---- values -------------------------------------------------------------------------------------------------------
def entity_id(value: str, kind: str) -> str:
    """An entry's id from its id or its URN (``urn:li:<kind>:<id>``)."""
    v = str(value or "").strip()
    prefix = f"urn:li:{kind}:"
    if v.startswith(prefix):
        v = v[len(prefix):]
    if not ID.match(v):
        raise ValueError(f"{value!r}: an id is letters, digits, '.', '_' and '-', starting with a letter or digit, "
                         f"or a {prefix}… URN")
    return v


def principals(values: Iterable[str]) -> list[str]:
    out = []
    for o in values:
        o = o if o.startswith("urn:li:") else f"urn:li:corpuser:{o}"
        if not OWNER.match(o):
            raise ValueError(f"{o}: a person (urn:li:corpuser:…) or a group (urn:li:corpGroup:…)")
        out.append(o)
    return sorted(set(out))


def node_urn(i: str) -> str:
    return f"urn:li:glossaryNode:{i}"


def term_urn(i: str) -> str:
    return f"urn:li:glossaryTerm:{i}"


def tail(urn: str) -> str:
    return urn.split(":", 3)[3]


def custom(props: dict) -> dict[str, str]:
    raw = props.get("customProperties") or {}
    if isinstance(raw, list):                                  # GraphQL's shape: [{key, value}]
        return {str(e.get("key")): str(e.get("value")) for e in raw if isinstance(e, dict)}
    return {str(k): str(v) for k, v in raw.items()}


def is_steward(entry: dict) -> bool:
    return entry.get("type") == STEWARD or str(entry.get("typeUrn") or "").endswith("data_steward")


def people(ownership: Optional[dict]) -> tuple[list[str], list[str]]:
    """(owners, stewards) from an ownership aspect: stewards are owners of the data steward type."""
    owners, stewards = set(), set()
    for o in (ownership or {}).get("owners") or []:
        if o.get("owner"):
            (stewards if is_steward(o) else owners).add(o["owner"])
    return sorted(owners), sorted(stewards)


def ownership(before: Optional[dict], owners: list[str], stewards: list[str], stamp: dict) -> dict:
    """The ownership aspect for these owners and stewards; entries that stay keep what Core had on them."""
    old = before or {}
    kept_owner = {o["owner"]: o for o in old.get("owners") or [] if o.get("owner") and not is_steward(o)}
    kept_steward = {o["owner"]: o for o in old.get("owners") or [] if o.get("owner") and is_steward(o)}
    entries = [kept_owner.get(o) or {"owner": o, "type": OWNER_TYPE} for o in owners]
    entries += [kept_steward.get(s) or {"owner": s, "type": STEWARD} for s in stewards]
    return {"owners": entries, "lastModified": stamp}


def removed(aspects: dict) -> bool:
    return bool((aspects.get("status") or {}).get("removed"))


# ---- what is written ----------------------------------------------------------------------------------------------
class NodeIn(BaseModel):
    """A term group as written: everything but ``id`` can change."""

    id: str = Field(description="the group's id: urn:li:glossaryNode:<id>")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20_000)
    parent: Optional[str] = Field(default=None, description="the group it is in (its id), none at the top")
    owners: list[str] = Field(default_factory=list, description="people and groups")
    custom_properties: dict[str, str] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return entity_id(v, "glossaryNode")

    @field_validator("parent")
    @classmethod
    def _parent(cls, v: Optional[str]) -> Optional[str]:
        return entity_id(v, "glossaryNode") if v not in (None, "") else None

    @field_validator("owners")
    @classmethod
    def _owners(cls, v: list[str]) -> list[str]:
        return principals(v)

    @model_validator(mode="after")
    def _not_own_parent(self) -> "NodeIn":
        if self.parent == self.id:
            raise ValueError("a group cannot be in itself")
        return self


class TermIn(BaseModel):
    """A glossary term as written: everything but ``id`` can change."""

    id: str = Field(description="the term's id: urn:li:glossaryTerm:<id>")
    name: str = Field(min_length=1, max_length=200)
    definition: str = Field(default="", max_length=20_000)
    parent: Optional[str] = Field(default=None, description="the term group it is in (its id)")
    is_a: list[str] = Field(default_factory=list, description="terms this one is a kind of (their ids)")
    has_a: list[str] = Field(default_factory=list, description="terms this one contains (their ids)")
    related: list[str] = Field(default_factory=list, description="terms related to it (their ids)")
    owners: list[str] = Field(default_factory=list, description="people and groups")
    stewards: list[str] = Field(default_factory=list, description="the term's data stewards")
    custom_properties: dict[str, str] = Field(default_factory=dict)
    source_url: Optional[str] = Field(default=None, max_length=2000)
    deprecated: bool = False
    deprecation_note: str = Field(default="", max_length=2000)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return entity_id(v, "glossaryTerm")

    @field_validator("parent")
    @classmethod
    def _parent(cls, v: Optional[str]) -> Optional[str]:
        return entity_id(v, "glossaryNode") if v not in (None, "") else None

    @field_validator("is_a", "has_a", "related")
    @classmethod
    def _terms(cls, v: list[str]) -> list[str]:
        return sorted({entity_id(t, "glossaryTerm") for t in v})

    @field_validator("owners", "stewards")
    @classmethod
    def _people(cls, v: list[str]) -> list[str]:
        return principals(v)

    @field_validator("source_url")
    @classmethod
    def _url(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None
        if not re.match(r"^https?://\S+$", v):
            raise ValueError("a link (http:// or https://)")
        return v

    @model_validator(mode="after")
    def _not_related_to_itself(self) -> "TermIn":
        if self.id in set(self.is_a) | set(self.has_a) | set(self.related):
            raise ValueError("a term is not related to itself")
        return self


def node_record(urn: str, a: dict) -> dict:
    info = a.get("glossaryNodeInfo") or {}
    owners = people(a.get("ownership"))[0]
    parent = info.get("parentNode")
    return {"id": tail(urn), "urn": urn, "name": info.get("name") or tail(urn), "description": info.get("definition") or "",
            "parent": tail(parent) if parent else None, "owners": owners, "custom_properties": custom(info)}


def term_record(urn: str, a: dict) -> dict:
    info = a.get("glossaryTermInfo") or {}
    rel = a.get("glossaryRelatedTerms") or {}
    dep = a.get("deprecation") or {}
    owners, stewards = people(a.get("ownership"))
    parent = info.get("parentNode")
    ids = lambda key: sorted({tail(u) for u in rel.get(key) or [] if str(u).startswith("urn:li:glossaryTerm:")})  # noqa: E731
    return {"id": tail(urn), "urn": urn, "name": info.get("name") or tail(urn), "definition": info.get("definition") or "",
            "parent": tail(parent) if parent else None, "is_a": ids("isRelatedTerms"), "has_a": ids("hasRelatedTerms"),
            "related": ids("relatedTerms"), "owners": owners, "stewards": stewards, "custom_properties": custom(info),
            "source_url": info.get("sourceUrl") or None, "deprecated": bool(dep.get("deprecated")),
            "deprecation_note": (dep.get("note") or "") if dep.get("deprecated") else ""}


# ---- the shared machinery -----------------------------------------------------------------------------------------
class CoreEditor:
    """Reads and writes Cairn Core for one part of the catalog. ``authorize(principal, privilege, urn) -> bool`` (False
    when the decision cannot be made); ``can_view(principal) -> (urn -> bool)``."""

    category = "catalog"

    def __init__(self, graph, audit=None, authorize: Optional[Callable[..., bool]] = None,
                 can_view: Optional[Callable[..., Callable[[str], bool]]] = None, clock: Callable[[], float] = time.time):
        self.graph, self.audit, self.clock = graph, audit, clock
        self.authorize = authorize or (lambda p, privilege, urn: False)
        self.can_view = can_view or (lambda p: (lambda urn: False))

    def _stamp(self, actor: str) -> dict:
        return {"time": int(self.clock() * 1000), "actor": actor}

    def _aspects(self, urn: str, names: list[str]) -> dict:
        try:
            return {a: self.graph.get_aspect(urn, a) for a in names}
        except Exception as e:                        # Core unreachable: nothing is decided on a guess
            raise EditError(f"Cairn Core did not answer: {e}"[:500], 503)

    def _scroll(self, entity_type: str, names: list[str]) -> list[dict]:
        try:
            return self.graph.scroll_entities(entity_type, names)
        except Exception as e:
            raise EditError(f"Cairn Core did not answer: {e}"[:500], 503)

    def _audit(self, actor: str, action: str, subject: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record(self.category, action, actor, subject, details)

    def _write(self, actor: str, mcps: list[dict], action: str, subject: str, details: dict) -> None:
        """Writes to Core, then audits; a refused write is audited as failed and answered 502."""
        try:
            self.graph.emit(mcps)
        except Exception as e:
            self._audit(actor, f"{action}_failed", subject, {**details, "error": str(e)[:300]})
            raise EditError(f"Cairn Core did not take the change (nothing was changed by Cairn): {e}"[:600], 502)
        self._audit(actor, action, subject, details)

    def _mutate(self, actor: str, field: str, query: str, variables: dict, action: str, subject: str,
                details: dict) -> None:
        """A Core GraphQL mutation that answers true when done; anything else is a failure (audited, 502)."""
        try:
            ok = (self.graph.graphql(query, variables) or {}).get(field) is True
            why = "" if ok else "Cairn Core did not confirm it"
        except Exception as e:
            ok, why = False, str(e)
        if not ok:
            self._audit(actor, f"{action}_failed", subject, {**details, "error": why[:300]})
            raise EditError(f"Cairn Core did not take the change: {why}"[:600], 502)
        self._audit(actor, action, subject, details)

    def _may(self, p, privilege: str, urns: Iterable[Optional[str]]) -> bool:
        return any(self.authorize(p, privilege, u) for u in urns)

    def _visible(self, p, urn: str) -> None:
        if not ASSET.match(urn or ""):
            raise EditError(f"{urn!r}: an asset's URN", 422)
        if not self.can_view(p)(urn):
            raise EditError(f"you cannot see {urn}", 403)

    def search(self, or_filters: list[dict]) -> list[dict]:
        """Everything Core's search finds for these filters: [{urn, type, name}] (refused beyond MAX_CONTENTS)."""
        out: list[dict] = []
        start = 0
        while True:
            try:
                data = self.graph.graphql(SEARCH, {"input": {"query": "*", "start": start, "count": 200,
                                                             "orFilters": or_filters}}) or {}
            except Exception as e:
                raise EditError(f"Cairn Core's search did not answer: {e}"[:500], 503)
            res = data.get("searchAcrossEntities")
            if not isinstance(res, dict):
                raise EditError("Cairn Core's search did not answer", 503)
            hits = [h.get("entity") or {} for h in res.get("searchResults") or []]
            for e in hits:
                if e.get("urn"):
                    out.append({"urn": e["urn"], "type": e.get("type") or "",
                                "name": e.get("name") or (e.get("properties") or {}).get("name") or tail(e["urn"])})
            total = int(res.get("total") or 0)
            if total > MAX_CONTENTS:
                raise EditError(f"more than {MAX_CONTENTS} assets: narrow it down first", 409)
            start += len(hits)
            if not hits or start >= total:
                return out


# ---- the glossary -------------------------------------------------------------------------------------------------
class Glossary(CoreEditor):
    category = "glossary"

    # reading
    def _nodes(self) -> dict[str, dict]:
        return {tail(r["urn"]): r for r in self._scroll("glossaryNode", NODE_ASPECTS)
                if r.get("glossaryNodeInfo") and not removed(r)}

    def _terms(self) -> dict[str, dict]:
        return {tail(r["urn"]): r for r in self._scroll("glossaryTerm", TERM_ASPECTS)
                if r.get("glossaryTermInfo") and not removed(r)}

    def _node(self, nid: str) -> Optional[dict]:
        a = self._aspects(node_urn(nid), NODE_ASPECTS)
        return a if a.get("glossaryNodeInfo") and not removed(a) else None

    def _term(self, tid: str) -> Optional[dict]:
        a = self._aspects(term_urn(tid), TERM_ASPECTS)
        return a if a.get("glossaryTermInfo") and not removed(a) else None

    def ancestors(self, nid: Optional[str]) -> list[str]:
        """The URNs of group ``nid`` and the groups above it (nearest first)."""
        out: list[str] = []
        while nid and len(out) < MAX_DEPTH:
            urn = node_urn(nid)
            if urn in out:
                break
            out.append(urn)
            a = self._aspects(urn, ["glossaryNodeInfo"]).get("glossaryNodeInfo") or {}
            parent = a.get("parentNode")
            nid = tail(parent) if parent else None
        return out

    def may_manage(self, p, urn: str, parent: Optional[str]) -> bool:
        return self._may(p, MANAGE, [urn, *self.ancestors(parent)])

    def _require(self, p, urn: str, parent: Optional[str], what: str) -> None:
        if not self.may_manage(p, urn, parent):
            raise EditError(f"{MANAGE} is needed on {what} (or a term group above it)", 403)

    def list_nodes(self, p) -> list[dict]:
        check = self.can_view(p)
        out = [node_record(r["urn"], r) for r in self._nodes().values() if check(r["urn"])]
        return sorted(out, key=lambda n: (n["name"].lower(), n["id"]))

    def list_terms(self, p) -> list[dict]:
        check = self.can_view(p)
        out = [term_record(r["urn"], r) for r in self._terms().values() if check(r["urn"])]
        return sorted(out, key=lambda n: (n["name"].lower(), n["id"]))

    def get_node(self, p, nid: str) -> dict:
        a = self._node(nid)
        if a is None or not self.can_view(p)(node_urn(nid)):
            raise EditError(f"no term group {nid}", 404)
        return node_record(node_urn(nid), a)

    def get_term(self, p, tid: str) -> dict:
        a = self._term(tid)
        if a is None or not self.can_view(p)(term_urn(tid)):
            raise EditError(f"no glossary term {tid}", 404)
        return term_record(term_urn(tid), a)

    def rights(self, p, urn: Optional[str]) -> dict:
        """What ``p`` may do on an entry (or, with none, at the top of the glossary), with the reason when not."""
        if not urn:
            ok = self.authorize(p, MANAGE, None)
            return {"may_manage": ok, "reason": "" if ok else f"{MANAGE} is needed to add to the top of the glossary"}
        kind = urn.split(":")[2] if urn.startswith("urn:li:") else ""
        if kind not in ("glossaryNode", "glossaryTerm"):
            raise EditError(f"{urn}: a term group or term URN", 422)
        a = (self._node if kind == "glossaryNode" else self._term)(tail(urn))
        if a is None:
            raise EditError(f"no {urn}", 404)
        info = a.get("glossaryNodeInfo") or a.get("glossaryTermInfo") or {}
        parent = tail(info["parentNode"]) if info.get("parentNode") else None
        ok = self.may_manage(p, urn, parent)
        return {"may_manage": ok, "reason": "" if ok else f"{MANAGE} is needed on it or on a term group above it"}

    # term groups
    def save_node(self, p, spec: NodeIn, create: bool) -> dict:
        urn = node_urn(spec.id)
        before = self._aspects(urn, NODE_ASPECTS)
        exists = bool(before.get("glossaryNodeInfo")) and not removed(before)
        if create and exists:
            raise EditError(f"a term group {spec.id} exists", 409)
        if not create and not exists:
            raise EditError(f"no term group {spec.id}", 404)
        if spec.parent and self._node(spec.parent) is None:
            raise EditError(f"no term group {spec.parent} to put it in", 422)
        info_before = (before.get("glossaryNodeInfo") or {}) if exists else {}
        old_parent = tail(info_before["parentNode"]) if info_before.get("parentNode") else None
        if exists:
            self._require(p, urn, old_parent, spec.id)
        if not exists or spec.parent != old_parent:
            self._require(p, urn, spec.parent, spec.parent or "the top of the glossary")
        if spec.parent and urn in self.ancestors(spec.parent):
            raise EditError("a group cannot be put in a group inside it", 422)
        stamp = self._stamp(p.urn)
        info: dict[str, Any] = {k: v for k, v in info_before.items() if k not in ("parentNode", "customProperties")}
        info.update({"name": spec.name, "definition": spec.description, "customProperties": spec.custom_properties})
        if spec.parent:
            info["parentNode"] = node_urn(spec.parent)
        kept_stewards = people(before.get("ownership"))[1] if exists else []     # set in Core's UI: kept
        own = ownership(before.get("ownership") if exists else None, spec.owners, kept_stewards, stamp)
        mcps = [make_mcp(urn, "glossaryNodeInfo", info), make_mcp(urn, "ownership", own)]
        if removed(before):                     # made again: back in search and the glossary
            mcps.append(make_mcp(urn, "status", {"removed": False}))
        self._write(p.urn, mcps, "glossary.node_created" if create else "glossary.node_updated", urn,
                    {"parent": spec.parent, "owners": len(spec.owners)})
        return node_record(urn, {"glossaryNodeInfo": info, "ownership": own})

    def delete_node(self, p, nid: str) -> None:
        urn = node_urn(nid)
        a = self._node(nid)
        if a is None:
            raise EditError(f"no term group {nid}", 404)
        parent = (a.get("glossaryNodeInfo") or {}).get("parentNode")
        self._require(p, urn, tail(parent) if parent else None, nid)
        inside = [k for k, r in self._nodes().items() if (r["glossaryNodeInfo"] or {}).get("parentNode") == urn]
        inside += [k for k, r in self._terms().items() if (r["glossaryTermInfo"] or {}).get("parentNode") == urn]
        if inside:
            raise EditError(f"the group holds {len(inside)} terms and groups ({', '.join(sorted(inside)[:5])}): "
                            "move or delete them first", 409)
        self._write(p.urn, [make_mcp(urn, "status", {"removed": True})], "glossary.node_deleted", urn, {})

    # terms
    def save_term(self, p, spec: TermIn, create: bool) -> dict:
        urn = term_urn(spec.id)
        before = self._aspects(urn, TERM_ASPECTS)
        exists = bool(before.get("glossaryTermInfo")) and not removed(before)
        if create and exists:
            raise EditError(f"a glossary term {spec.id} exists", 409)
        if not create and not exists:
            raise EditError(f"no glossary term {spec.id}", 404)
        if spec.parent and self._node(spec.parent) is None:
            raise EditError(f"no term group {spec.parent} to put it in", 422)
        info_before = (before.get("glossaryTermInfo") or {}) if exists else {}
        old_parent = tail(info_before["parentNode"]) if info_before.get("parentNode") else None
        if exists:
            self._require(p, urn, old_parent, spec.id)
        if not exists or spec.parent != old_parent:
            self._require(p, urn, spec.parent, spec.parent or "the top of the glossary")
        missing = [t for t in {*spec.is_a, *spec.has_a, *spec.related} if self._term(t) is None]
        if missing:
            raise EditError(f"no glossary term {', '.join(sorted(missing))} to relate it to", 422)
        stamp = self._stamp(p.urn)
        info: dict[str, Any] = {k: v for k, v in info_before.items()
                                if k not in ("parentNode", "customProperties", "sourceUrl")}
        info.update({"name": spec.name, "definition": spec.definition, "customProperties": spec.custom_properties})
        info.setdefault("termSource", "INTERNAL")
        if spec.parent:
            info["parentNode"] = node_urn(spec.parent)
        if spec.source_url:
            info["sourceUrl"] = spec.source_url
        rel_before = (before.get("glossaryRelatedTerms") or {}) if exists else {}
        rel = {k: v for k, v in rel_before.items() if k not in ("isRelatedTerms", "hasRelatedTerms", "relatedTerms")}
        rel.update({"isRelatedTerms": [term_urn(t) for t in spec.is_a],
                    "hasRelatedTerms": [term_urn(t) for t in spec.has_a],
                    "relatedTerms": [term_urn(t) for t in spec.related]})
        own = ownership(before.get("ownership") if exists else None, spec.owners, spec.stewards, stamp)
        mcps = [make_mcp(urn, "glossaryTermInfo", info), make_mcp(urn, "glossaryRelatedTerms", rel),
                make_mcp(urn, "ownership", own)]
        dep_before = before.get("deprecation") or {}
        dep = None
        if spec.deprecated or dep_before.get("deprecated"):
            dep = {"deprecated": spec.deprecated, "note": spec.deprecation_note if spec.deprecated else "",
                   "actor": p.urn}
            mcps.append(make_mcp(urn, "deprecation", dep))
        if removed(before):
            mcps.append(make_mcp(urn, "status", {"removed": False}))
        details = {"parent": spec.parent, "stewards": len(spec.stewards), "deprecated": spec.deprecated}
        self._write(p.urn, mcps, "glossary.term_created" if create else "glossary.term_updated", urn, details)
        return term_record(urn, {"glossaryTermInfo": info, "glossaryRelatedTerms": rel, "ownership": own,
                                 "deprecation": dep or {}})

    def term_assets(self, p, tid: str) -> list[dict]:
        """The assets that carry the term (on themselves or on a column) that ``p`` may see."""
        self.get_term(p, tid)
        check = self.can_view(p)
        return [a for a in self._uses(tid) if check(a["urn"])]

    def _uses(self, tid: str) -> list[dict]:
        urn = term_urn(tid)
        return self.search([{"and": [{"field": "glossaryTerms", "values": [urn]}]},
                            {"and": [{"field": "fieldGlossaryTerms", "values": [urn]}]}])

    def delete_term(self, p, tid: str, force: bool = False) -> dict:
        urn = term_urn(tid)
        a = self._term(tid)
        if a is None:
            raise EditError(f"no glossary term {tid}", 404)
        parent = (a.get("glossaryTermInfo") or {}).get("parentNode")
        self._require(p, urn, tail(parent) if parent else None, tid)
        uses = self._uses(tid)
        if uses and not force:
            raise EditError(f"the term is on {len(uses)} assets: remove it from them first, or delete with force", 409)
        mcps, refused = [], []
        for u in uses:
            ops = self._removals(p, u["urn"], urn, refused)
            mcps.extend(ops)
        if refused:
            raise EditError(f"you may not remove the term from {len(refused)} of its assets: "
                            f"{', '.join(refused[:5])}", 403)
        if mcps:
            self._write(p.urn, mcps, "glossary.term_unapplied", urn, {"assets": len(uses), "reason": "term deleted"})
        self._write(p.urn, [make_mcp(urn, "status", {"removed": True})], "glossary.term_deleted", urn,
                    {"removed_from": len(uses)})
        return {"id": tid, "urn": urn, "removed_from": len(uses)}

    def _removals(self, p, asset: str, term: str, refused: list[str]) -> list[dict]:
        """The patches that take ``term`` off ``asset`` and its columns, if ``p`` may (else ``asset`` is refused)."""
        if not self.can_view(p)(asset):
            refused.append(asset)
            return []
        out: list[dict] = []
        gt = self._aspects(asset, ["glossaryTerms"]).get("glossaryTerms") or {}
        if any(t.get("urn") == term for t in gt.get("terms") or []):
            if not self.authorize(p, EDIT_TERMS, asset):
                refused.append(asset)
                return []
            out.append(patch_mcp(asset, "glossaryTerms", [{"op": "remove", "path": pointer("terms", term)}]))
        if asset.startswith("urn:li:dataset:"):
            esm = self._aspects(asset, ["editableSchemaMetadata"]).get("editableSchemaMetadata") or {}
            ops = [field_label_op("remove", f["fieldPath"], term) for f in esm.get("editableSchemaFieldInfo") or []
                   if any(t.get("urn") == term for t in ((f.get("glossaryTerms") or {}).get("terms") or []))]
            if ops:
                if not self.authorize(p, EDIT_COLUMN_TERMS, asset):
                    refused.append(asset)
                    return []
                out.append(patch_mcp(asset, "editableSchemaMetadata", ops))
        return out

    # terms on assets
    def labels(self, p, asset: str) -> dict:
        """An asset's terms (on it and on its columns) and domain, and what ``p`` may change, with reasons."""
        self._visible(p, asset)
        a = self._aspects(asset, ["glossaryTerms", "editableSchemaMetadata", "schemaMetadata", "domains"])
        cols: dict[str, set] = {}
        for f in (a.get("schemaMetadata") or {}).get("fields") or []:
            cols.setdefault(f["fieldPath"], set()).update(
                t["urn"] for t in ((f.get("glossaryTerms") or {}).get("terms") or []) if t.get("urn"))
        for f in (a.get("editableSchemaMetadata") or {}).get("editableSchemaFieldInfo") or []:
            cols.setdefault(f["fieldPath"], set()).update(
                t["urn"] for t in ((f.get("glossaryTerms") or {}).get("terms") or []) if t.get("urn"))
        domains = (a.get("domains") or {}).get("domains") or []
        may_terms = self.authorize(p, EDIT_TERMS, asset)
        may_cols = asset.startswith("urn:li:dataset:") and self.authorize(p, EDIT_COLUMN_TERMS, asset)
        may_domain = not asset.startswith("urn:li:dataProduct:") and self.authorize(p, "EDIT_ENTITY_DOMAINS", asset)
        return {"asset": asset, "terms": sorted(t["urn"] for t in (a.get("glossaryTerms") or {}).get("terms") or []
                                               if t.get("urn")),
                "columns": {k: sorted(v) for k, v in sorted(cols.items())}, "domain": domains[0] if domains else None,
                "may": {"terms": may_terms, "column_terms": may_cols, "domain": may_domain},
                "why": {"terms": "" if may_terms else f"{EDIT_TERMS} is needed on this asset",
                        "column_terms": "" if may_cols else (f"{EDIT_COLUMN_TERMS} is needed on this dataset"
                                                             if asset.startswith("urn:li:dataset:") else
                                                             "only a dataset has columns"),
                        "domain": "" if may_domain else ("a data product's domain is changed on the product"
                                                         if asset.startswith("urn:li:dataProduct:") else
                                                         "EDIT_ENTITY_DOMAINS is needed on this asset")}}

    def apply(self, p, asset: str, term: str, field: Optional[str] = None, on: bool = True) -> dict:
        """Put ``term`` on ``asset`` (``field``: on that column) or, with ``on`` False, take it off."""
        tid = entity_id(term, "glossaryTerm")
        turn = term_urn(tid)
        t = self._term(tid)
        if t is None or not self.can_view(p)(turn):
            raise EditError(f"no glossary term {tid}", 404)
        if on and (t.get("deprecation") or {}).get("deprecated"):
            raise EditError(f"{tid} is deprecated: {(t.get('deprecation') or {}).get('note') or 'use another term'}", 409)
        self._visible(p, asset)
        field = (field or "").strip() or None
        if field:
            if not asset.startswith("urn:li:dataset:"):
                raise EditError("only a dataset has columns", 422)
            a = self._aspects(asset, ["schemaMetadata", "editableSchemaMetadata"])
            paths = {f["fieldPath"] for f in (a.get("schemaMetadata") or {}).get("fields") or []}
            if field not in paths:
                raise EditError(f"{asset} has no column {field}", 404)
            if not self.authorize(p, EDIT_COLUMN_TERMS, asset):
                raise EditError(f"{EDIT_COLUMN_TERMS} is needed on {asset}", 403)
            info = next((f for f in (a.get("editableSchemaMetadata") or {}).get("editableSchemaFieldInfo") or []
                         if f.get("fieldPath") == field), {})
            present = any(x.get("urn") == turn for x in ((info.get("glossaryTerms") or {}).get("terms") or []))
            if not on and not present:
                ingested = next((f for f in (a.get("schemaMetadata") or {}).get("fields") or []
                                 if f.get("fieldPath") == field), {})
                if any(x.get("urn") == turn for x in ((ingested.get("glossaryTerms") or {}).get("terms") or [])):
                    raise EditError(f"the term on {field} comes from ingestion: remove it at the source", 409)
            mcp = patch_mcp(asset, "editableSchemaMetadata",
                            [field_label_op("add" if on else "remove", field, turn, {"urn": turn} if on else None)])
        else:
            if not self.authorize(p, EDIT_TERMS, asset):
                raise EditError(f"{EDIT_TERMS} is needed on {asset}", 403)
            gt = self._aspects(asset, ["glossaryTerms"]).get("glossaryTerms") or {}
            present = any(x.get("urn") == turn for x in gt.get("terms") or [])
            op: dict[str, Any] = {"op": "add" if on else "remove", "path": pointer("terms", turn)}
            if on:
                op["value"] = {"urn": turn}
            mcp = patch_mcp(asset, "glossaryTerms", [op])
        out = {"asset": asset, "term": turn, "field": field, "applied": on}
        if present == on:
            return {**out, "changed": False}
        self._write(p.urn, [mcp], "glossary.term_applied" if on else "glossary.term_unapplied", asset,
                    {"term": turn, "field": field})
        return {**out, "changed": True}


__all__ = ["CoreEditor", "EditError", "Glossary", "NodeIn", "TermIn", "MANAGE", "EDIT_TERMS", "EDIT_COLUMN_TERMS",
           "entity_id", "node_urn", "term_urn", "node_record", "term_record"]
