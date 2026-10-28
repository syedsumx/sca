"""AI stewards: enrich undocumented, unowned and unclassified datasets in bulk, for people to review.

A run walks datasets that lack a description, an owner or a classification, most used first, and
proposes, each with its evidence and a confidence:

* **owners**: the people who query the table most, who last edited its metadata, and who own its
  domain and its upstream tables;
* **classifications**: PII terms for columns, from column names and sampled values;
* **descriptions**: a grounded draft (with an LLM configured, within the AI budget).

Proposals wait in a review queue where owners accept, edit or reject many at once. Accepted ones
are written as one undoable batch attributed to the reviewer; classifications at or above
``CAIRN_STEWARD_AUTO_ACCEPT`` confidence (off by default) are applied straight away.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Float, String, Table, and_, select, update
from sqlalchemy.engine import Engine

from cairn.common.db import JSONText, metadata
from cairn.governance.pii import classify_schema

logger = logging.getLogger(__name__)

steward_proposals = Table(
    "cairn_steward_proposals", metadata,
    Column("id", String(40), primary_key=True),
    Column("urn", String(512), nullable=False, index=True),
    Column("kind", String(16), nullable=False),            # owner | term | description
    Column("field", String(512), nullable=False),          # column path, or "" for the dataset
    Column("value", JSONText, nullable=False),
    Column("evidence", JSONText, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("state", String(16), nullable=False, index=True),   # PROPOSED | ACCEPTED | AUTO_ACCEPTED | REJECTED
    Column("run_id", String(40), nullable=False),
    Column("priority", Float, nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("decided_by", String(512), nullable=True),
    Column("decided_ms", BigInteger, nullable=True),
    Column("batch_id", String(40), nullable=True),
)

SCAN_ASPECTS = ["datasetProperties", "editableDatasetProperties", "ownership", "globalTags", "glossaryTerms",
                "domains", "schemaMetadata", "editableSchemaMetadata", "status"]
SYSTEM_ACTORS = ("urn:li:corpuser:datahub", "urn:li:corpuser:__datahub_system", "urn:li:corpuser:admin",
                 "urn:li:corpuser:unknown")
PRIVILEGE = {"owner": "EDIT_ENTITY_OWNERS", "term": "EDIT_DATASET_COL_GLOSSARY_TERMS", "description": "EDIT_ENTITY_DOCS"}


def _now() -> int:
    return int(time.time() * 1000)


def is_person(urn: Optional[str]) -> bool:
    return bool(urn) and urn.startswith(("urn:li:corpuser:", "urn:li:corpGroup:")) and urn not in SYSTEM_ACTORS \
        and not urn.startswith("urn:li:corpuser:cairn-")


def gaps(aspects: dict) -> set[str]:
    """What a dataset is missing: description, owner, classification."""
    out = set()
    desc = (aspects.get("editableDatasetProperties") or {}).get("description") or \
        (aspects.get("datasetProperties") or {}).get("description")
    if not (desc or "").strip():
        out.add("description")
    if not (aspects.get("ownership") or {}).get("owners"):
        out.add("owner")
    labelled = any(((f.get("glossaryTerms") or {}).get("terms")) for f in
                   ((aspects.get("editableSchemaMetadata") or {}).get("editableSchemaFieldInfo") or [])
                   + ((aspects.get("schemaMetadata") or {}).get("fields") or []))
    if not labelled and not (aspects.get("glossaryTerms") or {}).get("terms"):
        out.add("classification")
    return out


def owner_candidates(usage: list[dict], modifiers: list[str], domain_owners: list[str],
                     upstream_owners: list[str]) -> list[dict]:
    """Score people as likely owners. Usage share counts most; the evidence says why."""
    score: dict[str, float] = {}
    why: dict[str, list[str]] = {}

    def add(who: str, points: float, reason: str) -> None:
        if not is_person(who):
            return
        score[who] = 1 - (1 - score.get(who, 0.0)) * (1 - points)
        why.setdefault(who, []).append(reason)

    usage = [u for u in usage if is_person(u.get("user"))]          # service accounts' queries don't count
    total = sum(u.get("count", 0) for u in usage) or 1
    for u in usage:
        share = u.get("count", 0) / total
        if share >= 0.1:
            add(u.get("user"), 0.25 + 0.5 * share, f"ran {u.get('count')} of the {total} recent queries")
    for m in dict.fromkeys(modifiers):
        add(m, 0.35, "last edited this dataset's metadata")
    for d in dict.fromkeys(domain_owners):
        add(d, 0.3, "owns the dataset's domain")
    counts: dict[str, int] = {}
    for o in upstream_owners:
        counts[o] = counts.get(o, 0) + 1
    for o, n in counts.items():
        add(o, min(0.5, 0.2 * n), f"owns {n} upstream table(s)")
    ranked = sorted(score.items(), key=lambda kv: -kv[1])
    return [{"owner": who, "confidence": round(min(s, 0.95), 3), "evidence": why[who]} for who, s in ranked[:3]
            if s >= 0.3]


class StewardError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class StewardService:
    def __init__(self, engine: Engine, graph, bulk, *, popularity: Callable[[str], float] = lambda urn: 0.0,
                 upstreams: Callable[[str], list[str]] = lambda urn: [], docgen=None, audit=None,
                 auto_accept: Optional[float] = None):
        self.engine = engine
        self.graph = graph
        self.bulk = bulk
        self.popularity = popularity
        self.upstreams = upstreams
        self.docgen = docgen
        self.audit = audit
        env = os.environ.get("CAIRN_STEWARD_AUTO_ACCEPT")
        self.auto_accept = auto_accept if auto_accept is not None else (float(env) if env else None)

    # ---- proposing -----------------------------------------------------------------------
    def _usage(self, urn: str) -> list[dict]:
        try:
            rows = self.graph.get_timeseries(urn, "datasetUsageStatistics", 30)
        except Exception:  # noqa: BLE001 - usage is optional evidence
            return []
        counts: dict[str, int] = {}
        for r in rows:
            for u in r.get("userCounts") or []:
                if u.get("user"):
                    counts[u["user"]] = counts.get(u["user"], 0) + int(u.get("count") or 0)
        return [{"user": k, "count": v} for k, v in counts.items()]

    def _samples(self, urn: str) -> dict[str, list]:
        try:
            prof = (self.graph.get_timeseries(urn, "datasetProfile", 1) or [{}])[0]
        except Exception:  # noqa: BLE001
            return {}
        return {f["fieldPath"]: f.get("sampleValues") or [] for f in prof.get("fieldProfiles") or [] if f.get("fieldPath")}

    def _owners_of(self, urn: str) -> list[str]:
        return [o["owner"] for o in (self.graph.get_aspect(urn, "ownership") or {}).get("owners", []) if o.get("owner")]

    def propose_for(self, urn: str, aspects: dict, missing: set[str], drafts_left: list[int]) -> list[dict]:
        out: list[dict] = []
        if "owner" in missing:
            modifiers = [((aspects.get(a) or {}).get("lastModified") or {}).get("actor")
                         for a in ("editableDatasetProperties", "editableSchemaMetadata", "globalTags")]
            domains = (aspects.get("domains") or {}).get("domains") or []
            domain_owners = [o for d in domains for o in self._owners_of(d)]
            upstream_owners = [o for u in self.upstreams(urn) for o in self._owners_of(u)]
            for c in owner_candidates(self._usage(urn), [m for m in modifiers if m], domain_owners, upstream_owners):
                out.append({"kind": "owner", "field": "", "value": c["owner"], "confidence": c["confidence"],
                            "evidence": c["evidence"]})
        fields = (aspects.get("schemaMetadata") or {}).get("fields") or []
        if fields:
            labelled = {f.get("fieldPath") for f in ((aspects.get("editableSchemaMetadata") or {})
                                                     .get("editableSchemaFieldInfo") or [])
                        if (f.get("glossaryTerms") or {}).get("terms")}
            for c in classify_schema(fields, self._samples(urn)):
                if c.field_path in labelled:
                    continue
                d = c.as_dict()
                out.append({"kind": "term", "field": c.field_path, "value": d["term"], "confidence": d["confidence"],
                            "evidence": d["evidence"]})
        if "description" in missing and self.docgen is not None and drafts_left[0] > 0:
            try:
                draft = self.docgen.draft(urn)
                drafts_left[0] -= 1
                out.append({"kind": "description", "field": "", "confidence": float(draft.get("confidence", 0.5)),
                            "value": {"description": draft["description"], "columns": draft.get("columns", []),
                                      "model": draft.get("model")},
                            "evidence": ["drafted from the schema, lineage and neighbouring documentation"]
                            + [f"unknown: {u}" for u in draft.get("unknowns", [])[:3]]})
            except Exception as e:  # noqa: BLE001 - out of budget, no LLM, a bad draft: keep the rest
                logger.info("steward: no description draft for %s (%s)", urn, e)
                drafts_left[0] = 0
        return out

    def run(self, actor: str = "urn:li:corpuser:cairn-steward", limit: int = 100, max_drafts: int = 10,
            urns: Optional[list[str]] = None) -> dict:
        """Propose for up to ``limit`` datasets with gaps (or only ``urns``), most used first. Pending
        proposals are not repeated; rejected ones are not proposed again."""
        candidates = []
        wanted = set(urns) if urns else None
        for row in self.graph.scroll_entities("dataset", SCAN_ASPECTS):
            if (row.get("status") or {}).get("removed") or (wanted is not None and row["urn"] not in wanted):
                continue
            missing = gaps(row)
            if missing:
                candidates.append((self.popularity(row["urn"]), row["urn"], row, missing))
        candidates.sort(key=lambda c: (-c[0], c[1]))
        with self.engine.connect() as conn:
            seen = {(r.urn, r.kind, r.field, str(r.value)) for r in conn.execute(select(
                steward_proposals.c.urn, steward_proposals.c.kind, steward_proposals.c.field, steward_proposals.c.value))}
        rid, now = f"srun_{uuid.uuid4().hex[:12]}", _now()
        made, auto = [], []
        drafts_left = [max_drafts]
        for priority, urn, aspects, missing in candidates[:limit]:
            for p in self.propose_for(urn, aspects, missing, drafts_left):
                if (urn, p["kind"], p["field"], str(p["value"])) in seen:
                    continue
                pid = f"sp_{uuid.uuid4().hex[:14]}"
                made.append({"id": pid, "urn": urn, "priority": priority, **p})
        with self.engine.begin() as conn:
            for p in made:
                conn.execute(steward_proposals.insert().values(
                    id=p["id"], urn=p["urn"], kind=p["kind"], field=p["field"], value=p["value"], evidence=p["evidence"],
                    confidence=p["confidence"], state="PROPOSED", run_id=rid, priority=p["priority"], created_ms=now))
        if self.auto_accept is not None:
            auto = [p["id"] for p in made if p["kind"] == "term" and p["confidence"] >= self.auto_accept]
            if auto:
                self.decide(auto, "accept", actor, authorize=lambda priv, urn: True, state="AUTO_ACCEPTED")
        if self.audit is not None:
            self.audit.record("ai", "steward.run", actor, rid,
                              {"datasets": min(len(candidates), limit), "proposals": len(made), "auto_accepted": len(auto)})
        return {"run_id": rid, "datasets_with_gaps": len(candidates), "examined": min(len(candidates), limit),
                "proposed": len(made), "auto_accepted": len(auto),
                "by_kind": {k: sum(1 for p in made if p["kind"] == k) for k in ("owner", "term", "description")}}

    # ---- reviewing -----------------------------------------------------------------------
    def proposals(self, state: str = "PROPOSED", kind: Optional[str] = None, urn: Optional[str] = None,
                  limit: int = 200) -> list[dict]:
        stmt = select(steward_proposals).where(steward_proposals.c.state == state)
        if kind:
            stmt = stmt.where(steward_proposals.c.kind == kind)
        if urn:
            stmt = stmt.where(steward_proposals.c.urn == urn)
        stmt = stmt.order_by(steward_proposals.c.priority.desc(), steward_proposals.c.confidence.desc()).limit(limit)
        with self.engine.connect() as conn:
            return [self._public(r) for r in conn.execute(stmt)]

    @staticmethod
    def _public(r) -> dict:
        return {"id": r.id, "urn": r.urn, "kind": r.kind, "field": r.field, "value": r.value, "evidence": r.evidence,
                "confidence": r.confidence, "state": r.state, "priority": r.priority, "decided_by": r.decided_by,
                "batch_id": r.batch_id}

    def _changes(self, rows: list, edits: dict[str, Any]) -> list[dict]:
        """Bulk-edit changes for accepted owner and term proposals (additive)."""
        from cairn.governance.bulk import ASPECTS, _current

        changes: dict[tuple[str, str, str], dict] = {}
        for r in rows:
            if r.kind == "description":
                continue
            what = "owners" if r.kind == "owner" else "glossary_terms"
            key = (r.urn, r.field, what)
            if key not in changes:
                a = {n: self.graph.get_aspect(r.urn, n) or {} for n in ASPECTS + ("domains", "ownership", "globalTags",
                                                                                  "glossaryTerms")}
                cur = _current(r.urn, r.field, a)[what]
                changes[key] = {"urn": r.urn, "field": r.field, "what": what, "from": sorted(cur), "to": list(cur),
                                "add": [], "remove": []}
            value = edits.get(r.id, r.value)
            c = changes[key]
            if value not in c["to"]:
                c["to"].append(value)
                c["add"].append(value)
        return [c for c in changes.values() if c["add"]]

    def decide(self, ids: list[str], action: str, actor: str, authorize: Callable[[str, str], bool],
               edits: Optional[dict[str, Any]] = None, state: Optional[str] = None) -> dict:
        if action not in ("accept", "reject"):
            raise StewardError("action is accept or reject")
        edits = edits or {}
        with self.engine.connect() as conn:
            rows = conn.execute(select(steward_proposals).where(and_(
                steward_proposals.c.id.in_(ids or [""]), steward_proposals.c.state == "PROPOSED"))).all()
        denied = [r.id for r in rows if not authorize(PRIVILEGE[r.kind], r.urn)]
        if denied:
            raise StewardError(f"you may not decide {len(denied)} of these proposals", 403)
        batch = None
        if action == "accept":
            changes = self._changes(rows, edits)
            if changes:
                batch = self.bulk.record(changes, actor, "ai-steward", {"proposals": len(rows)})["batch_id"]
            for r in rows:
                if r.kind == "description":
                    from cairn.ai.docgen import apply_documentation

                    draft = dict(r.value)
                    if isinstance(edits.get(r.id), str):
                        draft["description"] = edits[r.id]
                    apply_documentation(self.graph, r.urn, draft, actor)
        final = state or ("ACCEPTED" if action == "accept" else "REJECTED")
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(update(steward_proposals).where(steward_proposals.c.id.in_([r.id for r in rows] or [""]))
                         .values(state=final, decided_by=actor, decided_ms=now, batch_id=batch))
        if self.audit is not None and rows:
            self.audit.record("ai", f"steward.{final.lower()}", actor, batch or rows[0].urn, {"proposals": len(rows)})
        return {"decided": len(rows), "state": final, "batch_id": batch,
                "skipped": len(set(ids) - {r.id for r in rows})}
