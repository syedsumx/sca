"""Context Agents: continuously mine queries, dbt, BI and runs into reviewable findings.

Agents (each a pure function of fetched metadata → findings):

* ``query``: METRIC_DEFINITION, JOIN_RELATIONSHIP, TERM_LINK (from ``query`` entities)
* ``dbt``: METRIC_DEFINITION from model SQL (``viewProperties.viewLogic``), TERM_LINK from docs
* ``bi``: METRIC_DEFINITION / TERM_LINK from chart input fields
* ``runs``: RUN_ANOMALY (failure streaks) from ``dataProcessInstance`` run events
* ``drift``: DEFINITION_DRIFT when one business name has several materially different definitions
* ``staleness``: DOC_STALENESS when documentation and schema disagree
* ``glossary``: TERM_SUGGESTION for frequently used metric names missing from the glossary

Findings are de-duplicated by a stable fingerprint, so re-mining refreshes evidence and confidence
but never resurrects a dismissed finding. Accepting a finding writes native Cairn Core metadata with
``attribution.source = urn:li:dataHubAction:cairn-context-agent``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import BigInteger, Column, Float, String, Table, select, update
from sqlalchemy.engine import Engine

from cairn.common.audit import AuditLog
from cairn.common.db import JSONText, metadata
from cairn.common.proposals import audit_stamp, make_mcp, schema_field_urn
from cairn.context.sql import analyze_sql, normalize_name, same_definition

logger = logging.getLogger(__name__)

CONTEXT_SOURCE = "urn:li:dataHubAction:cairn-context-agent"
CONTEXT_ACTOR = "urn:li:corpuser:cairn-context-agent"
MINED_NODE = "urn:li:glossaryNode:Mined"
FINDING_TYPES = ("TERM_SUGGESTION", "TERM_LINK", "METRIC_DEFINITION", "DEFINITION_DRIFT", "DOC_STALENESS",
                 "JOIN_RELATIONSHIP", "RUN_ANOMALY")

findings_table = Table(
    "cairn_context_findings", metadata,
    Column("id", String(40), primary_key=True),
    Column("fingerprint", String(64), nullable=False, unique=True),
    Column("type", String(32), nullable=False, index=True),
    Column("status", String(16), nullable=False, index=True),
    Column("confidence", Float, nullable=False),
    Column("doc", JSONText, nullable=False),
    Column("entities", JSONText, nullable=False),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("updated_at_ms", BigInteger, nullable=False),
)
cursors_table = Table(
    "cairn_context_cursors", metadata,
    Column("source", String(64), primary_key=True),
    Column("cursor", JSONText, nullable=True),
    Column("updated_at_ms", BigInteger, nullable=False),
)


@dataclass
class Finding:
    type: str
    key: tuple
    title: str
    summary_md: str
    confidence: float
    entities: list[str]
    evidence: list[dict] = field(default_factory=list)
    proposed_changes: list[dict] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps([self.type, *self.key], default=str).encode()).hexdigest()


def _now() -> int:
    return int(time.time() * 1000)


def _iso(ms: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat() if ms else None


def _dataset_parts(urn: str) -> tuple[Optional[str], str]:
    m = re.match(r"^urn:li:dataset:\(urn:li:dataPlatform:([^,]+),(.+),[A-Z_]+\)$", urn)
    return (m.group(1), m.group(2).lower()) if m else (None, urn.lower())


def map_table(table: Optional[str], candidates: Iterable[str]) -> Optional[str]:
    """Map a SQL table reference to the best-matching dataset urn among ``candidates``."""
    if not table:
        return None
    t = table.lower()
    best, best_len = None, 0
    for urn in candidates:
        _, name = _dataset_parts(urn)
        if name == t:
            return urn
        if name.endswith("." + t) or t.endswith("." + name) or name.split(".")[-1] == t.split(".")[-1]:
            score = len(set(name.split(".")) & set(t.split(".")))
            if score > best_len:
                best, best_len = urn, score
    return best


# --------------------------------------------------------------------------- fetching
@dataclass
class Snapshot:
    """Everything the agents need, fetched once per mining cycle."""

    queries: list[dict] = field(default_factory=list)        # {urn, sql, subjects, platform}
    datasets: dict[str, dict] = field(default_factory=dict)  # urn → aspects
    charts: dict[str, dict] = field(default_factory=dict)    # urn → aspects
    runs: list[dict] = field(default_factory=list)           # {instance, template, ts, status}
    glossary: dict[str, dict] = field(default_factory=dict)  # term urn → glossaryTermInfo


def fetch_snapshot(graph, max_datasets: int = 5000, max_runs: int = 500) -> Snapshot:
    snap = Snapshot()
    for row in graph.scroll_entities("query", ["queryProperties", "querySubjects"]):
        props = row.get("queryProperties") or {}
        sql = ((props.get("statement") or {}).get("value") or "").strip()
        if not sql:
            continue
        subjects = [s.get("entity") for s in (row.get("querySubjects") or {}).get("subjects", []) if s.get("entity")]
        ds_subjects = [s for s in subjects if s.startswith("urn:li:dataset:")]
        platform = _dataset_parts(ds_subjects[0])[0] if ds_subjects else None
        snap.queries.append({"urn": row["urn"], "sql": sql, "subjects": ds_subjects, "platform": platform,
                             "name": props.get("name"), "description": props.get("description")})
    for row in graph.scroll_entities("dataset", ["schemaMetadata", "editableSchemaMetadata", "viewProperties",
                                                 "datasetProperties", "editableDatasetProperties", "ownership"])[:max_datasets]:
        snap.datasets[row["urn"]] = row
    for row in graph.scroll_entities("chart", ["chartInfo", "inputFields"]):
        snap.charts[row["urn"]] = row
    for row in graph.scroll_entities("glossaryTerm", ["glossaryTermInfo"]):
        if row.get("glossaryTermInfo"):
            snap.glossary[row["urn"]] = row["glossaryTermInfo"]
    instances = graph.scroll_entities("dataProcessInstance", ["dataProcessInstanceRelationships",
                                                              "dataProcessInstanceProperties"])
    getter = getattr(graph, "get_timeseries", None)
    for row in instances[-max_runs:]:
        template = (row.get("dataProcessInstanceRelationships") or {}).get("parentTemplate")
        if not template or getter is None:
            continue
        for ev in getter(row["urn"], "dataProcessInstanceRunEvent", 5):
            if ev.get("status") == "COMPLETE":
                snap.runs.append({"instance": row["urn"], "template": template, "ts": ev.get("timestampMillis", 0),
                                  "status": (ev.get("result") or {}).get("type", "UNKNOWN")})
    return snap


# --------------------------------------------------------------------------- agents
def _term_index(glossary: dict[str, dict]) -> dict[str, str]:
    return {normalize_name(info.get("name") or urn.split(":")[-1].split(".")[-1]): urn
            for urn, info in glossary.items()}


def _metric_findings(defs: list[dict]) -> list[Finding]:
    """One METRIC_DEFINITION finding per (name, canonical expression)."""
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for d in defs:
        by_key[(normalize_name(d["name"]), d["canonical"])].append(d)
    out = []
    for (name, canonical), items in by_key.items():
        datasets = sorted({u for i in items for u in i["datasets"]})
        sources = sorted({i["source"] for i in items})
        conf = min(0.95, 0.5 + 0.1 * len(items) + (0.1 if len(sources) > 1 else 0))
        out.append(Finding(
            type="METRIC_DEFINITION", key=(name, canonical), title=f"Metric “{name}” = {canonical}",
            summary_md=f"`{name}` is computed as `{canonical}` in {len(items)} place(s) "
                       f"({', '.join(sources)}).",
            confidence=round(conf, 2), entities=datasets,
            evidence=[{"source": i["source"], "ref": i["ref"], "snippet": i["expression"][:300]} for i in items[:5]],
            proposed_changes=[{"kind": "create_term", "entity_urn": None, "field_path": None,
                               "value": {"name": name.title(), "definition": f"Computed as `{canonical}`.",
                                         "expression": canonical}}],
            data={"name": name, "canonical": canonical, "occurrences": len(items), "sources": sources}))
    return out


def _drift_findings(defs: list[dict], glossary_terms: dict[str, str]) -> list[Finding]:
    by_name: dict[str, list[dict]] = defaultdict(list)
    for d in defs:
        by_name[normalize_name(d["name"])].append(d)
    out = []
    for name, items in by_name.items():
        variants: list[tuple[str, list[dict]]] = []
        for d in items:
            for canon, group in variants:
                if same_definition(canon, d["canonical"]):
                    group.append(d)
                    break
            else:
                variants.append((d["canonical"], [d]))
        if len(variants) < 2:
            continue
        variants.sort(key=lambda v: -len(v[1]))
        lines = [f"- `{canon}` — {len(group)}× in {', '.join(sorted({g['source'] for g in group}))}"
                 for canon, group in variants]
        datasets = sorted({u for d in items for u in d["datasets"]})
        term = glossary_terms.get(name)
        out.append(Finding(
            type="DEFINITION_DRIFT", key=(name, tuple(sorted(v[0] for v in variants))),
            title=f"“{name}” has {len(variants)} conflicting definitions",
            summary_md=f"The business metric **{name}** is defined differently across sources:\n" + "\n".join(lines)
                       + ("\n\nGlossary term exists: align it with the agreed definition." if term else ""),
            confidence=round(min(0.95, 0.6 + 0.05 * len(items)), 2), entities=datasets + ([term] if term else []),
            evidence=[{"source": g["source"], "ref": g["ref"], "snippet": g["expression"][:300]}
                      for _, group in variants for g in group[:2]],
            proposed_changes=[{"kind": "open_discussion", "entity_urn": u, "field_path": None,
                               "value": f"Context Agent: **{name}** has conflicting definitions:\n" + "\n".join(lines)}
                              for u in datasets[:3]],
            data={"name": name, "variants": [v[0] for v in variants]}))
    return out


def mine(snap: Snapshot, min_term_occurrences: int = 3) -> list[Finding]:
    findings: list[Finding] = []
    defs: list[dict] = []
    dataset_urns = list(snap.datasets) or sorted({s for q in snap.queries for s in q["subjects"]})
    terms = _term_index(snap.glossary)
    joins: Counter = Counter()
    join_evidence: dict[tuple, list[dict]] = defaultdict(list)
    dims: Counter = Counter()
    dim_evidence: dict[tuple, list[dict]] = defaultdict(list)

    # -- query agent ------------------------------------------------------------------
    for q in snap.queries:
        facts = analyze_sql(q["sql"], q["platform"])
        if facts.error:
            continue
        candidates = q["subjects"] or dataset_urns
        for m in facts.metrics:
            ds = sorted({u for c in m.columns if (u := map_table(c.table, candidates))})
            defs.append({"name": m.name, "canonical": m.canonical, "expression": m.expression, "datasets": ds,
                         "source": "query", "ref": q["urn"]})
        for j in facts.joins:
            lu, ru = map_table(j.left.table, candidates), map_table(j.right.table, candidates)
            if lu and ru and lu != ru:
                key = tuple(sorted([(lu, j.left.column), (ru, j.right.column)]))
                joins[key] += 1
                join_evidence[key].append({"source": "query", "ref": q["urn"], "snippet": q["sql"][:240]})
        for d in facts.dimensions:
            u = map_table(d.column.table, candidates)
            if u:
                key = (u, d.column.column, normalize_name(d.name))
                dims[key] += 1
                dim_evidence[key].append({"source": "query", "ref": q["urn"], "snippet": f"{d.column.column} AS {d.name}"})

    # -- dbt agent ----------------------------------------------------------------------
    for urn, row in snap.datasets.items():
        platform, _ = _dataset_parts(urn)
        logic = ((row.get("viewProperties") or {}).get("viewLogic") or "")
        if platform == "dbt" and logic:
            facts = analyze_sql(logic, None)
            for m in facts.metrics:
                defs.append({"name": m.name, "canonical": m.canonical, "expression": m.expression, "datasets": [urn],
                             "source": "dbt", "ref": urn})

    # -- BI agent: input fields of charts --------------------------------------------------
    for urn, row in snap.charts.items():
        for f in ((row.get("inputFields") or {}).get("fields") or []):
            sf = f.get("schemaField") or {}
            desc = sf.get("description") or ""
            m = re.search(r"((?:sum|count|avg|min|max)\s*\(.+\))", desc, re.I)
            if m:
                facts = analyze_sql(f"select {m.group(1)} as {re.sub(r'[^A-Za-z0-9_]', '_', sf.get('fieldPath', 'm'))} from t")
                for mm in facts.metrics:
                    defs.append({"name": sf.get("fieldPath", mm.name), "canonical": mm.canonical,
                                 "expression": m.group(1), "datasets": [], "source": "bi", "ref": urn})

    findings += _metric_findings(defs)
    findings += _drift_findings(defs, terms)

    for key, n in joins.items():
        (lu, lc), (ru, rc) = key
        findings.append(Finding(
            type="JOIN_RELATIONSHIP", key=key, title=f"{_dataset_parts(lu)[1]}.{lc} ↔ {_dataset_parts(ru)[1]}.{rc}",
            summary_md=f"These datasets are joined on `{lc}` = `{rc}` in {n} quer{'y' if n == 1 else 'ies'}.",
            confidence=round(min(0.95, 0.4 + 0.15 * n), 2), entities=[lu, ru], evidence=join_evidence[key][:5],
            proposed_changes=[{"kind": "create_relationship", "entity_urn": lu, "field_path": lc,
                               "value": {"source": lu, "destination": ru, "source_field": lc, "destination_field": rc}}],
            data={"count": n}))

    for (u, col, name), n in dims.items():
        term = terms.get(name)
        if not term:
            continue
        findings.append(Finding(
            type="TERM_LINK", key=(u, col, term), title=f"Link “{name}” → {_dataset_parts(u)[1]}.{col}",
            summary_md=f"`{col}` is consistently aliased as **{name}** ({n}×), matching glossary term `{term}`.",
            confidence=round(min(0.95, 0.55 + 0.1 * n), 2), entities=[u, term], evidence=dim_evidence[(u, col, name)][:5],
            proposed_changes=[{"kind": "add_term", "entity_urn": u, "field_path": col, "value": term}]))

    # -- glossary suggestion agent ----------------------------------------------------------
    usage = Counter(normalize_name(d["name"]) for d in defs)
    for name, n in usage.items():
        if n >= min_term_occurrences and name not in terms:
            ds = sorted({u for d in defs if normalize_name(d["name"]) == name for u in d["datasets"]})
            canon = Counter(d["canonical"] for d in defs if normalize_name(d["name"]) == name).most_common(1)[0][0]
            findings.append(Finding(
                type="TERM_SUGGESTION", key=(name,), title=f"Add glossary term “{name.title()}”",
                summary_md=f"**{name}** is used {n}× across queries/models but has no glossary term. "
                           f"Most common definition: `{canon}`.",
                confidence=round(min(0.9, 0.4 + 0.08 * n), 2), entities=ds,
                proposed_changes=[{"kind": "create_term", "entity_urn": None, "field_path": None,
                                   "value": {"name": name.title(), "definition": f"Computed as `{canon}`.",
                                             "expression": canon}}]))

    # -- documentation staleness agent ------------------------------------------------------
    for urn, row in snap.datasets.items():
        fields = {f["fieldPath"]: f for f in (row.get("schemaMetadata") or {}).get("fields", [])}
        docs = {i["fieldPath"]: i.get("description") for i in
                (row.get("editableSchemaMetadata") or {}).get("editableSchemaFieldInfo", []) if i.get("description")}
        docs.update({p: f.get("description") for p, f in fields.items() if f.get("description") and p not in docs})
        if not fields or not docs:
            continue
        orphaned = sorted(set(docs) - set(fields))
        undocumented = sorted(set(fields) - set(docs))
        coverage = 1 - len(undocumented) / len(fields)
        if orphaned or (undocumented and coverage >= 0.5):
            owners = [o.get("owner") for o in (row.get("ownership") or {}).get("owners", []) if o.get("owner")]
            mention = " ".join(f"@{o}" for o in owners[:3])
            parts = []
            if orphaned:
                parts.append(f"documented columns no longer in the schema: {', '.join(f'`{c}`' for c in orphaned[:8])}")
            if undocumented:
                parts.append(f"new undocumented columns: {', '.join(f'`{c}`' for c in undocumented[:8])}")
            findings.append(Finding(
                type="DOC_STALENESS", key=(urn, tuple(orphaned), tuple(undocumented)),
                title=f"Docs out of date on {_dataset_parts(urn)[1]}",
                summary_md="Schema changed after documentation was written — " + "; ".join(parts) + ".",
                confidence=0.9 if orphaned else 0.7, entities=[urn],
                proposed_changes=[{"kind": "open_discussion", "entity_urn": urn, "field_path": None,
                                   "value": f"Context Agent: documentation looks stale — {'; '.join(parts)}. {mention}"}]))

    # -- run anomaly agent --------------------------------------------------------------------
    by_template: dict[str, list[dict]] = defaultdict(list)
    for r in snap.runs:
        by_template[r["template"]].append(r)
    for template, runs in by_template.items():
        runs.sort(key=lambda r: r["ts"], reverse=True)
        streak = 0
        for r in runs:
            if r["status"] == "FAILURE":
                streak += 1
            else:
                break
        if streak >= 2:
            findings.append(Finding(
                type="RUN_ANOMALY", key=(template, runs[0]["ts"] // 86_400_000),
                title=f"{template.split(',')[-1].rstrip(')')} failed {streak} runs in a row",
                summary_md=f"The last {streak} runs of `{template}` failed; downstream data may be stale.",
                confidence=0.9, entities=[template],
                evidence=[{"source": "run", "ref": r["instance"], "snippet": f"{r['status']} at {_iso(r['ts'])}"}
                          for r in runs[:streak]],
                data={"streak": streak, "last_ts": runs[0]["ts"]}))
    return findings


# --------------------------------------------------------------------------- store + actions
class ContextStore:
    def __init__(self, engine: Engine, graph=None, collab=None, audit: Optional[AuditLog] = None,
                 notify=None, layer=None):
        self.layer = layer          # ContextLayer: approved definitions and query patterns
        self.engine = engine
        self.graph = graph
        self.collab = collab
        self.audit = audit
        self.notify = notify

    def upsert(self, findings: list[Finding]) -> dict[str, int]:
        stats = {"new": 0, "updated": 0, "unchanged_dismissed": 0}
        now = _now()
        drifted: list[Finding] = []
        with self.engine.begin() as conn:
            for f in findings:
                doc = {"title": f.title, "summary_md": f.summary_md, "evidence": f.evidence,
                       "proposed_changes": f.proposed_changes, "data": f.data}
                row = conn.execute(select(findings_table).where(findings_table.c.fingerprint == f.fingerprint)).first()
                if row is None:
                    conn.execute(findings_table.insert().values(
                        id=f"fnd_{uuid.uuid4().hex[:16]}", fingerprint=f.fingerprint, type=f.type, status="OPEN",
                        confidence=f.confidence, doc=doc, entities=f.entities, created_at_ms=now, updated_at_ms=now))
                    stats["new"] += 1
                    if f.type == "DEFINITION_DRIFT":
                        drifted.append(f)
                elif row.status == "DISMISSED":
                    stats["unchanged_dismissed"] += 1
                else:
                    conn.execute(update(findings_table).where(findings_table.c.id == row.id).values(
                        confidence=f.confidence, doc=doc, entities=f.entities, updated_at_ms=now))
                    stats["updated"] += 1
        # Publish after commit: never hold the findings transaction open across notification writes.
        if self.notify is not None:
            from cairn.notify.hub import Event

            for f in drifted:
                self.notify.publish(Event(type="context.drift", title=f.title, body=f.summary_md[:600],
                                          actor=CONTEXT_ACTOR, entity_urn=(f.entities or [None])[0],
                                          include_followers=True, include_owners=True))
        return stats

    def _dict(self, r) -> dict:
        return {"id": r.id, "type": r.type, "status": r.status, "confidence": r.confidence,
                "entities": r.entities or [], "created_at": _iso(r.created_at_ms),
                "updated_at": _iso(r.updated_at_ms), **{k: v for k, v in r.doc.items() if k != "data"}}

    def list(self, type_: Optional[str] = None, status: Optional[str] = "OPEN", entity_urn: Optional[str] = None,
             limit: int = 200) -> list[dict]:
        stmt = select(findings_table).order_by(findings_table.c.confidence.desc(),
                                               findings_table.c.updated_at_ms.desc()).limit(2000)
        if type_:
            stmt = stmt.where(findings_table.c.type == type_)
        if status:
            stmt = stmt.where(findings_table.c.status == status)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        items = [self._dict(r) for r in rows]
        if entity_urn:
            items = [i for i in items if entity_urn in i["entities"]]
        return items[:limit]

    def get(self, fid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(findings_table).where(findings_table.c.id == fid)).first()
        if not r:
            raise KeyError("finding not found")
        return self._dict(r)

    def set_status(self, fid: str, status: str, actor: str) -> dict:
        with self.engine.begin() as conn:
            conn.execute(update(findings_table).where(findings_table.c.id == fid).values(status=status,
                                                                                          updated_at_ms=_now()))
        if self.audit:
            self.audit.record("context", f"finding.{status.lower()}", actor, fid, {})
        return self.get(fid)

    def accept(self, fid: str, actor: str, edits: Optional[dict] = None) -> dict:
        """Apply a finding's changes. ``edits`` (name, expression, description) refine a metric or
        join before it becomes an approved, versioned definition."""
        f = self.get(fid)
        if f["status"] != "OPEN":
            raise ValueError(f"finding is {f['status']}")
        with self.engine.connect() as conn:
            raw = conn.execute(select(findings_table.c.doc).where(findings_table.c.id == fid)).scalar()
        f["data"] = (raw or {}).get("data") or {}
        if edits:
            for change in f.get("proposed_changes", []):
                if change["kind"] == "create_term":
                    v = change["value"]
                    v["name"] = edits.get("name") or v["name"]
                    if edits.get("expression"):
                        v["expression"] = edits["expression"]
                        v["definition"] = edits.get("description") or f"Computed as `{edits['expression']}`."
        mcps: list[dict] = []
        for change in f.get("proposed_changes", []):
            mcps += self._apply(change, actor, f)
        if mcps and self.graph is not None:
            self.graph.emit(mcps)
        definition = self.layer.record(f, actor, edits) if self.layer is not None else None
        out = self.set_status(fid, "ACCEPTED", actor)
        out["applied_changes"] = len(mcps)
        out["definition"] = definition
        return out

    def _apply(self, change: dict, actor: str, finding: dict) -> list[dict]:
        kind = change["kind"]
        detail = {"finding_id": finding["id"], "approved_by": actor}
        if kind == "add_term":
            from cairn.governance.pii import apply_terms_mcps

            return apply_terms_mcps(self.graph, [(schema_field_urn(change["entity_urn"], change["field_path"]),
                                                  change["value"], CONTEXT_SOURCE, detail)], actor=actor)
        if kind == "create_term":
            v = change["value"]
            slug = re.sub(r"[^A-Za-z0-9]+", "", v["name"].title()) or "Term"
            term_urn = f"urn:li:glossaryTerm:Mined.{slug}"
            return [
                make_mcp(MINED_NODE, "glossaryNodeInfo", {"name": "Mined", "definition":
                         "Business terms discovered by Cairn Context Agents from queries, dbt and BI."}),
                make_mcp(term_urn, "glossaryTermInfo", {
                    "name": v["name"], "definition": v["definition"], "termSource": "INTERNAL",
                    "parentNode": MINED_NODE,
                    "customProperties": {"cairn_expression": v.get("expression", ""), "cairn_finding": finding["id"],
                                         "cairn_approved_by": actor}}),
            ]
        if kind == "create_relationship":
            v = change["value"]
            rid = hashlib.sha1(f"{v['source']}|{v['source_field']}|{v['destination']}|{v['destination_field']}"
                               .encode()).hexdigest()[:24]
            return [make_mcp(f"urn:li:erModelRelationship:cairn-{rid}", "erModelRelationshipProperties", {
                "name": f"{_dataset_parts(v['source'])[1].split('.')[-1]}.{v['source_field']} → "
                        f"{_dataset_parts(v['destination'])[1].split('.')[-1]}.{v['destination_field']}",
                "source": v["source"], "destination": v["destination"],
                "relationshipFieldMappings": [{"sourceField": v["source_field"],
                                               "destinationField": v["destination_field"]}],
                "created": audit_stamp(actor), "lastModified": audit_stamp(actor)})]
        if kind == "open_discussion" and self.collab is not None and change.get("entity_urn"):
            from cairn.collab.service import ThreadIn

            self.collab.create_thread(CONTEXT_ACTOR, ThreadIn(entity_urn=change["entity_urn"], body_md=change["value"]))
            return []
        return []

    def ontology(self, limit: int = 300, domain_filter: Optional[Callable[[str], bool]] = None) -> dict:
        nodes: dict[str, dict] = {}
        edges: dict[tuple, dict] = {}

        def node(nid: str, label: str, kind: str) -> None:
            nodes.setdefault(nid, {"id": nid, "label": label, "kind": kind})

        def edge(a: str, b: str, kind: str, w: float = 1.0) -> None:
            k = (a, b, kind)
            if k in edges:
                edges[k]["weight"] += w
            else:
                edges[k] = {"source": a, "target": b, "kind": kind, "weight": w}

        for f in self.list(status=None, limit=2000):
            if f["status"] == "DISMISSED":
                continue
            ents = [e for e in f["entities"] if not domain_filter or domain_filter(e)]
            if f["type"] in ("METRIC_DEFINITION", "TERM_SUGGESTION"):
                name = f["title"].split("“")[1].split("”")[0] if "“" in f["title"] else f["title"]
                mid = f"metric:{normalize_name(name)}"
                node(mid, name, "metric")
                for e in ents:
                    node(e, _dataset_parts(e)[1].split(".")[-1], "dataset")
                    edge(mid, e, "computed_from")
            elif f["type"] == "JOIN_RELATIONSHIP" and len(ents) == 2:
                for e in ents:
                    node(e, _dataset_parts(e)[1].split(".")[-1], "dataset")
                edge(ents[0], ents[1], "joins_with", f["confidence"])
            elif f["type"] == "TERM_LINK":
                ch = f["proposed_changes"][0]
                col = f"{ch['entity_urn']}#{ch['field_path']}"
                node(ch["value"], ch["value"].split(":")[-1].split(".")[-1], "term")
                node(col, ch["field_path"], "column")
                node(ch["entity_urn"], _dataset_parts(ch["entity_urn"])[1].split(".")[-1], "dataset")
                edge(ch["value"], col, "describes", f["confidence"])
                edge(col, ch["entity_urn"], "column_of")
            if len(nodes) >= limit:
                break
        return {"nodes": list(nodes.values()), "edges": list(edges.values())}

    def cursor(self, source: str) -> Optional[Any]:
        with self.engine.connect() as conn:
            r = conn.execute(select(cursors_table).where(cursors_table.c.source == source)).first()
        return r.cursor if r else None

    def set_cursor(self, source: str, value: Any) -> None:
        with self.engine.begin() as conn:
            if conn.execute(select(cursors_table.c.source).where(cursors_table.c.source == source)).first():
                conn.execute(update(cursors_table).where(cursors_table.c.source == source)
                             .values(cursor=value, updated_at_ms=_now()))
            else:
                conn.execute(cursors_table.insert().values(source=source, cursor=value, updated_at_ms=_now()))


class ContextMiner:
    def __init__(self, graph, store: ContextStore, interval_seconds: int = 300):
        self.graph = graph
        self.store = store
        self.interval = interval_seconds

    def run_once(self) -> dict:
        started = _now()
        snap = fetch_snapshot(self.graph)
        findings = mine(snap)
        stats = self.store.upsert(findings)
        if self.store.layer is not None:
            stats["query_patterns"] = self.store.layer.index_queries(snap.queries)
        status = {"started_at": _iso(started), "duration_ms": _now() - started, "queries": len(snap.queries),
                  "datasets": len(snap.datasets), "charts": len(snap.charts), "runs": len(snap.runs),
                  "findings": len(findings), **stats}
        self.store.set_cursor("last_run", status)
        return status

    def run_forever(self, stop=None) -> None:
        import threading

        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                logger.info("context mining cycle: %s", self.run_once())
            except Exception:
                logger.exception("context mining cycle failed")
            stop.wait(self.interval)
