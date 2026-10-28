"""The context layer for analytics agents: how questions were answered before, vetted definitions,
and exports to the warehouses' own semantic layers.

* **Query patterns**: every mined query is indexed by the question it answers (its name and
  description, the metrics it computes, what it groups by, the tables it reads), so an agent can
  ask "how was this answered before" and get the SQL people actually ran, limited to tables the
  caller may see.
* **Definitions**: accepting a metric or join finding in the Context inbox (optionally edited)
  records an approved definition; every change is a new version.
* **Exports**: approved metrics and joins as a Snowflake semantic view or a Databricks metric
  view, for Snowflake Intelligence, Cortex Analyst and Genie.
"""

from __future__ import annotations

import math
import re
import time
import uuid
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, and_, select, update
from sqlalchemy.engine import Engine

from cairn.common.db import JSONText, metadata
from cairn.context.sql import analyze_sql

query_patterns = Table(
    "cairn_query_patterns", metadata,
    Column("urn", String(512), primary_key=True),
    Column("title", String(500), nullable=False),
    Column("text", JSONText, nullable=False),          # the searchable description (a string)
    Column("sql", JSONText, nullable=False),
    Column("tables", JSONText, nullable=False),
    Column("vector", JSONText, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

definitions = Table(
    "cairn_context_definitions", metadata,
    Column("id", String(40), primary_key=True),
    Column("kind", String(16), nullable=False),        # metric | join
    Column("name", String(200), nullable=False),
    Column("version", Integer, nullable=False),
    Column("doc", JSONText, nullable=False),           # metric: {expression, description, datasets}; join: {...}
    Column("state", String(16), nullable=False),       # APPROVED | RETIRED
    Column("finding_id", String(40), nullable=True),
    Column("approved_by", String(512), nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

definition_versions = Table(
    "cairn_context_definition_versions", metadata,
    Column("id", String(40), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("doc", JSONText, nullable=False),
    Column("name", String(200), nullable=False),
    Column("approved_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)


def _now() -> int:
    return int(time.time() * 1000)


def _table(urn: str) -> str:
    m = re.match(r"^urn:li:dataset:\([^,]+,(.+),[^,]+\)$", urn)
    return m.group(1) if m else urn


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _ident(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_").lower() or "x"


# --------------------------------------------------------------------------- query patterns
def pattern_text(query: dict) -> tuple[str, str]:
    """(title, searchable text) for a mined query."""
    facts = analyze_sql(query["sql"], query.get("platform"))
    metrics = [m.name for m in facts.metrics] if not facts.error else []
    dims = [d.name for d in facts.dimensions] if not facts.error else []
    tables = [_table(u).split(".")[-1] for u in query.get("subjects") or []]
    title = query.get("name") or (f"{', '.join(metrics) or 'rows'}" + (f" by {', '.join(dims)}" if dims else "")
                                  + (f" from {', '.join(tables)}" if tables else ""))
    words = [query.get("name") or "", query.get("description") or "", *metrics, *dims, *tables,
             " ".join(re.findall(r"[a-z_]{3,}", query["sql"].lower()))[:2000]]
    return title[:500], " ".join(w.replace("_", " ") for w in words if w)


def _cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    den = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return num / den if den else 0.0


class ContextLayer:
    def __init__(self, engine: Engine, embed: Callable[[str], list[float]], audit=None):
        self.engine = engine
        self.embed = embed
        self.audit = audit

    # ---- query patterns ----------------------------------------------------------------
    def index_queries(self, queries: Iterable[dict]) -> int:
        n, now = 0, _now()
        with self.engine.begin() as conn:
            for q in queries:
                title, text = pattern_text(q)
                vector = self.embed(text)
                conn.execute(query_patterns.delete().where(query_patterns.c.urn == q["urn"]))
                conn.execute(query_patterns.insert().values(urn=q["urn"], title=title, text=text, sql=q["sql"],
                                                            tables=q.get("subjects") or [], vector=vector, updated_ms=now))
                n += 1
        return n

    def answers(self, question: str, can_view: Callable[[str], bool], limit: int = 5) -> list[dict]:
        """Queries that answered similar questions, the caller seeing every table they read."""
        qv = self.embed(question)
        with self.engine.connect() as conn:
            rows = conn.execute(select(query_patterns)).all()
        scored = [(_cosine(qv, r.vector), r) for r in rows if r.tables and all(can_view(t) for t in r.tables)]
        scored.sort(key=lambda s: -s[0])
        approved = self.definitions()
        out = []
        for score, r in scored[:limit]:
            if score <= 0:
                break
            used = [d for d in approved if set(d["doc"].get("datasets") or []) & set(r.tables)]
            out.append({"urn": r.urn, "title": r.title, "sql": r.sql, "tables": r.tables, "score": round(score, 4),
                        "definitions": [{"name": d["name"], "kind": d["kind"], "version": d["version"],
                                         "expression": d["doc"].get("expression")} for d in used]})
        return out

    # ---- definitions ---------------------------------------------------------------------
    def record(self, finding: dict, actor: str, edits: Optional[dict] = None) -> Optional[dict]:
        """An approved definition (new, or a new version) from an accepted finding."""
        edits = edits or {}
        data = finding.get("data") or {}
        if finding["type"] in ("METRIC_DEFINITION", "TERM_SUGGESTION"):
            change = next((c for c in finding.get("proposed_changes") or [] if c["kind"] == "create_term"), None)
            name = edits.get("name") or data.get("name") or (change or {}).get("value", {}).get("name")
            expression = edits.get("expression") or data.get("canonical") or (change or {}).get("value", {}).get("expression")
            if not name or not expression:
                return None
            kind, doc = "metric", {"expression": expression, "description": edits.get("description") or "",
                                   "datasets": [e for e in finding.get("entities") or [] if e.startswith("urn:li:dataset:")]}
        elif finding["type"] == "JOIN_RELATIONSHIP":
            change = next((c for c in finding.get("proposed_changes") or [] if c["kind"] == "create_relationship"), None)
            if change is None:
                return None
            v = change["value"]
            name = edits.get("name") or f"{_table(v['source']).split('.')[-1]}_to_{_table(v['destination']).split('.')[-1]}"
            kind, doc = "join", {**v, "datasets": [v["source"], v["destination"]],
                                 "description": edits.get("description") or ""}
        else:
            return None
        name = name.strip()[:200]
        now = _now()
        with self.engine.begin() as conn:
            row = conn.execute(select(definitions).where(and_(definitions.c.kind == kind, definitions.c.name == name,
                                                              definitions.c.state == "APPROVED"))).first()
            if row is not None and row.doc == doc:
                return self._public(row)
            if row is None:
                did, version = f"def_{uuid.uuid4().hex[:12]}", 1
                conn.execute(definitions.insert().values(id=did, kind=kind, name=name, version=1, doc=doc,
                                                         state="APPROVED", finding_id=finding.get("id"),
                                                         approved_by=actor, updated_ms=now))
            else:
                did, version = row.id, row.version + 1
                conn.execute(update(definitions).where(definitions.c.id == did).values(
                    version=version, doc=doc, finding_id=finding.get("id"), approved_by=actor, updated_ms=now))
            conn.execute(definition_versions.insert().values(id=did, version=version, doc=doc, name=name,
                                                             approved_by=actor, created_ms=now))
        if self.audit is not None:
            self.audit.record("context", "definition.approved", actor, did, {"name": name, "version": version})
        return self.get(did)

    def get(self, did: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(definitions).where(definitions.c.id == did)).first()
        if r is None:
            raise KeyError("definition not found")
        return self._public(r)

    @staticmethod
    def _public(r) -> dict:
        return {"id": r.id, "kind": r.kind, "name": r.name, "version": r.version, "doc": r.doc, "state": r.state,
                "approved_by": r.approved_by, "updated_ms": r.updated_ms}

    def definitions(self, state: str = "APPROVED") -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(definitions).where(definitions.c.state == state)
                                .order_by(definitions.c.kind, definitions.c.name)).all()
        return [self._public(r) for r in rows]

    def versions(self, did: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(definition_versions).where(definition_versions.c.id == did)
                                .order_by(definition_versions.c.version.desc())).all()
        return [{"version": r.version, "name": r.name, "doc": r.doc, "approved_by": r.approved_by,
                 "created_ms": r.created_ms} for r in rows]

    def retire(self, did: str, actor: str) -> dict:
        self.get(did)
        with self.engine.begin() as conn:
            conn.execute(update(definitions).where(definitions.c.id == did).values(state="RETIRED", updated_ms=_now()))
        if self.audit is not None:
            self.audit.record("context", "definition.retired", actor, did, {})
        return self.get(did)


# --------------------------------------------------------------------------- exports
def _scope(defs: list[dict], datasets: Optional[list[str]]) -> tuple[list[dict], list[dict], list[str]]:
    metrics = [d for d in defs if d["kind"] == "metric" and d["doc"].get("datasets")]
    joins = [d for d in defs if d["kind"] == "join"]
    if datasets:
        keep = set(datasets)
        metrics = [m for m in metrics if set(m["doc"]["datasets"]) & keep]
        joins = [j for j in joins if {j["doc"]["source"], j["doc"]["destination"]} <= keep]
    tables = sorted({u for m in metrics for u in m["doc"]["datasets"]} | {u for j in joins for u in j["doc"]["datasets"]})
    return metrics, joins, tables


def export_snowflake(defs: list[dict], view: str, datasets: Optional[list[str]] = None) -> str:
    """A ``CREATE SEMANTIC VIEW`` from approved metrics and joins. Review it before running: primary
    keys come from join targets, and expressions are as approved."""
    metrics, joins, tables = _scope(defs, datasets)
    alias = {u: _ident(_table(u).split(".")[-1]) for u in tables}
    keys: dict[str, set[str]] = {}
    for j in joins:
        keys.setdefault(j["doc"]["destination"], set()).add(j["doc"]["destination_field"])
    lines = [f"CREATE OR REPLACE SEMANTIC VIEW {view}", "  TABLES ("]
    lines.append(",\n".join(
        f"    {alias[u]} AS {_table(u).upper()}" + (f" PRIMARY KEY ({', '.join(sorted(keys[u]))})" if u in keys else "")
        for u in tables))
    lines.append("  )")
    if joins:
        lines.append("  RELATIONSHIPS (")
        lines.append(",\n".join(
            f"    {_ident(j['name'])} AS {alias[j['doc']['source']]} ({j['doc']['source_field']}) "
            f"REFERENCES {alias[j['doc']['destination']]}" for j in joins))
        lines.append("  )")
    if metrics:
        lines.append("  METRICS (")
        lines.append(",\n".join(
            f"    {alias[m['doc']['datasets'][0]]}.{_ident(m['name'])} AS {m['doc']['expression']}"
            + (f" COMMENT = {_literal(m['doc']['description'])}" if m["doc"].get("description") else "")
            for m in metrics))
        lines.append("  )")
    lines.append("  COMMENT = 'Approved definitions from Cairn'")
    return "\n".join(lines) + ";\n"


def export_databricks(defs: list[dict], view: str, source: str, datasets: Optional[list[str]] = None) -> str:
    """A Unity Catalog metric view (``WITH METRICS LANGUAGE YAML``) over ``source``: approved joins
    from it become joins, approved metrics on its tables become measures."""
    import yaml

    metrics, joins, _ = _scope(defs, datasets)
    joins = [j for j in joins if j["doc"]["source"] == source]
    joined = {j["doc"]["destination"] for j in joins}
    measures = [m for m in metrics if set(m["doc"]["datasets"]) & ({source} | joined)]
    spec: dict[str, Any] = {"version": 0.1, "source": _table(source), "comment": "Approved definitions from Cairn"}
    if joins:
        spec["joins"] = [{"name": _ident(_table(j["doc"]["destination"]).split(".")[-1]),
                          "source": _table(j["doc"]["destination"]),
                          "on": f"source.{j['doc']['source_field']} = "
                                f"{_ident(_table(j['doc']['destination']).split('.')[-1])}.{j['doc']['destination_field']}"}
                         for j in joins]
    spec["measures"] = [{"name": _ident(m["name"]), "expr": m["doc"]["expression"],
                         **({"comment": m["doc"]["description"]} if m["doc"].get("description") else {})} for m in measures]
    body = yaml.safe_dump(spec, sort_keys=False)
    return f"CREATE OR REPLACE VIEW {view}\nWITH METRICS\nLANGUAGE YAML\nAS $$\n{body}$$;\n"
