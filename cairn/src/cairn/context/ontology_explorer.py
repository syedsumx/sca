"""The Ontology explorer's reads and review actions (``/v1/ontology``): the compiled ontology shaped for the explorer
page, a term with its evidence and provenance, the business terms of one dataset, proposals decided in batches, a
run's estimate and its diff against the run before.

Every read starts from :meth:`OntologyCompiler.view` under the caller's ``can_view``: an element is shown only when
the caller may see every dataset it is about, and so is every proposal, relationship and metric that hangs off it.
Sampled values are never shown (they were read as the compiler's sampling identity, not as the caller).

Deciding a proposal is a person's act (agents are refused): ``MANAGE_GLOSSARIES``, plus, to accept,
``EDIT_ENTITY_GLOSSARY_TERMS`` on each of its datasets; each decision is audited with its comment.
"""

from __future__ import annotations

import csv
import io
import logging
import threading
import time
from typing import Any, Callable, Optional

from sqlalchemy import select, update

from cairn.context.miner import findings_table
from cairn.context.ontology_compiler import SOURCE, OntologyCompiler, OntologyError, table_parts

logger = logging.getLogger(__name__)

CanView = Callable[[str], bool]
MAX_BATCH = 500
STATUS = {"OPEN": "proposed", "ACCEPTED": "accepted", "DISMISSED": "rejected"}


class ReviewError(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def _now_ms() -> int:
    return int(time.time() * 1000)


def _evidence(r: dict) -> dict:
    """A relationship's evidence by kind: declared keys, joins in the query log (with counts), a model's judgement."""
    prov = r.get("provenance") or []
    ev = r.get("evidence") or {}
    out: dict[str, Any] = {"sources": [p["source"] for p in prov]}
    fk = [p for p in prov if p["source"] in ("catalog_fk", "catalog")]
    if fk:
        out["foreign_key"] = {"detail": fk[0].get("detail", "")}
    if any(p["source"] == "observed_in_queries" for p in prov) or ev.get("queries"):
        out["query_log"] = {"queries": int(ev.get("queries") or 0), "executions": int(ev.get("executions") or 0),
                            "query_ids": list(ev.get("query_ids") or [])[:20]}
    model = [p for p in prov if p["source"] in ("model", "inferred")]
    if model:
        out["model"] = {"confidence": max(float(p.get("confidence") or 0) for p in model),
                        "detail": model[0].get("detail", ""), "inferred_without_model": model[0]["source"] == "inferred"}
    return out


class OntologyExplorer:
    def __init__(self, onto: OntologyCompiler, graph=None, context=None, audit=None):
        self.onto = onto
        self.graph = graph
        self.context = context
        self.audit = audit
        self._meta: tuple[float, dict] = (0.0, {})
        self._lock = threading.Lock()

    # ---- what Cairn knows of each dataset (domain, owners), cached briefly --------------------------
    def _dataset_meta(self, max_age: float = 30.0) -> dict[str, dict]:
        with self._lock:
            at, cached = self._meta
            if cached and time.monotonic() - at < max_age:
                return cached
        out: dict[str, dict] = {}
        if self.graph is not None:
            try:
                rows = self.graph.scroll_entities("dataset", ["domains", "ownership"])
            except Exception as e:  # noqa: BLE001 - domains and owners are a nicety; the ontology still reads
                logger.warning("ontology explorer: no dataset metadata (%s)", e)
                rows = []
            names: dict[str, str] = {}
            for row in rows:
                domains = [str(d) for d in ((row.get("domains") or {}).get("domains") or [])]
                for d in domains:
                    if d not in names:
                        try:
                            props = self.graph.get_aspect(d, "domainProperties") or {}
                        except Exception:  # noqa: BLE001
                            props = {}
                        names[d] = props.get("name") or d.rsplit(":", 1)[-1]
                owners = [o.get("owner") for o in ((row.get("ownership") or {}).get("owners") or []) if o.get("owner")]
                out[row["urn"]] = {"domain": domains[0] if domains else None,
                                   "domain_name": names[domains[0]] if domains else None, "owners": owners}
        with self._lock:
            self._meta = (time.monotonic(), out)
        return out

    def _place(self, urn: str, meta: dict[str, dict]) -> dict:
        _, catalog, schema, name = table_parts(urn)
        m = meta.get(urn) or {}
        return {"catalog": catalog, "schema": schema, "table_label": f"{schema}.{name}" if schema else name,
                "domain": m.get("domain"), "domain_name": m.get("domain_name"), "owners": m.get("owners") or []}

    # ---- the review state of each element ------------------------------------------------------------
    def _findings(self) -> list[Any]:
        with self.onto.engine.connect() as conn:
            rows = conn.execute(select(findings_table).order_by(findings_table.c.updated_at_ms.desc())).fetchall()
        return [r for r in rows if ((r.doc or {}).get("data") or {}).get("source") == SOURCE]

    def _review_state(self) -> dict[str, dict]:
        """element id → its newest proposal (id, status, confidence)."""
        out: dict[str, dict] = {}
        for r in self._findings():
            eid = ((r.doc or {}).get("data") or {}).get("element_id")
            if eid and eid not in out:
                out[eid] = {"proposal": r.id, "status": STATUS.get(r.status, r.status.lower()),
                            "confidence": r.confidence}
        return out

    @staticmethod
    def _term_of_table(v: dict) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for t in v["terms"]:
            out.setdefault(t["table_id"], t)
        return out

    # ---- the explorer's graph ---------------------------------------------------------------------------
    def explorer(self, can_view: CanView) -> dict:
        v = self.onto.view(can_view)
        meta = self._dataset_meta()
        state = self._review_state()
        attrs: dict[str, int] = {}
        formats: dict[str, int] = {}
        for a in v["attributes"]:
            attrs[a["term_id"]] = attrs.get(a["term_id"], 0) + 1
            if a.get("format"):
                formats[a["term_id"]] = formats.get(a["term_id"], 0) + 1
        junction = {t["table_id"] for t in v["tables"] if t.get("is_junction")} | {b["table_id"] for b in v["bridges"]}
        terms = []
        for t in v["terms"]:
            st = state.get(t["id"]) or {}
            conf = max([float(p.get("confidence") or 0) for p in t.get("provenance") or []] or [0.5])
            terms.append({"id": t["id"], "name": t["name"], "description": t.get("description") or "",
                          "synonyms": t.get("synonyms") or [], "table": t["table_id"], "table_name": t["table_name"],
                          **self._place(t["table_id"], meta), "attributes": attrs.get(t["id"], 0),
                          "date_formats": formats.get(t["id"], 0), "bridge": t["table_id"] in junction,
                          "provenance": [p["source"] for p in t.get("provenance") or []],
                          "confidence": round(st.get("confidence", conf), 2), "status": st.get("status", "compiled"),
                          "proposal": st.get("proposal") if st.get("status") == "proposed" else None})
        tot = self._term_of_table(v)
        rels = []
        for r in v["relationships"]:
            st = state.get(r["id"]) or {}
            src, dst = tot.get(r["source_table"]), tot.get(r["target_table"])
            rels.append({"id": r["id"], "source": r["source_table"], "target": r["target_table"],
                         "source_column": r["source_column"], "target_column": r["target_column"],
                         "source_term": src["id"] if src else None, "target_term": dst["id"] if dst else None,
                         "cardinality": r["cardinality"], "confidence": round(float(r.get("confidence") or 0), 2),
                         "join_default": r.get("join_default", True),
                         "provenance": [p["source"] for p in r.get("provenance") or []],
                         "queries": int((r.get("evidence") or {}).get("queries") or 0),
                         "status": st.get("status", "compiled"),
                         "proposal": st.get("proposal") if st.get("status") == "proposed" else None,
                         "bridge": r["source_table"] in junction})
        bridges = [{"id": b["id"], "table": b["table_id"], "table_name": b["table_name"],
                    **self._place(b["table_id"], meta), "term": (tot.get(b["table_id"]) or {}).get("id"),
                    "targets": [{"table": p["target_table"], "column": p["target_column"],
                                 "source_column": p["source_column"],
                                 "term": (tot.get(p["target_table"]) or {}).get("id")} for p in b["fk_pairs"]],
                    "metric": b.get("metric_id")} for b in v["bridges"]]
        tables = {}
        for urn in {t["table_id"] for t in v["tables"]} | {t["table"] for t in terms} \
                | {r["source"] for r in rels} | {r["target"] for r in rels}:
            tables[urn] = {"id": urn, **self._place(urn, meta), "bridge": urn in junction,
                           "term": (tot.get(urn) or {}).get("id")}
        metrics = [{"id": m["id"], "name": m["name"], "term": m.get("term_id"), "tables": m.get("tables") or [],
                    "source": m.get("source"), "status": (state.get(m["id"]) or {}).get("status", "compiled")}
                   for m in v["metrics"]]
        return {"run_id": v["run_id"], "version": v["version"], "terms": terms, "relationships": rels,
                "bridges": bridges, "metrics": metrics, "tables": sorted(tables.values(), key=lambda t: t["id"])}

    # ---- one term -----------------------------------------------------------------------------------------
    def term(self, can_view: CanView, tid: str) -> dict:
        v = self.onto.view(can_view)
        t = next((x for x in v["terms"] if x["id"] == tid), None)
        if t is None:
            raise OntologyError("no such term", 404)      # unseen and unknown look the same
        meta = self._dataset_meta()
        state = self._review_state()
        tot = self._term_of_table(v)
        table = t["table_id"]
        attributes = [{"id": a["id"], "column": a["column"], "name": a["name"], "datatype": a.get("datatype"),
                       "description": a.get("description"), "format": a.get("format"), "is_unique": a.get("is_unique"),
                       "deprecated": bool(a.get("deprecated")), "table": a["table_id"],
                       "provenance": [p["source"] for p in a.get("provenance") or []]}
                      for a in v["attributes"] if a["term_id"] == tid]
        rels = []
        for r in v["relationships"]:
            if table not in (r["source_table"], r["target_table"]):
                continue
            outgoing = r["source_table"] == table
            other = r["target_table"] if outgoing else r["source_table"]
            ot = tot.get(other)
            rels.append({"id": r["id"], "direction": "out" if outgoing else "in",
                         "source": r["source_table"], "source_column": r["source_column"],
                         "target": r["target_table"], "target_column": r["target_column"],
                         "join": f"{table_parts(r['source_table'])[3]}.{r['source_column']} = "
                                 f"{table_parts(r['target_table'])[3]}.{r['target_column']}",
                         "other_table": other, "other_term": {"id": ot["id"], "name": ot["name"]} if ot else None,
                         "cardinality": r["cardinality"], "confidence": round(float(r.get("confidence") or 0), 2),
                         "join_default": r.get("join_default", True), "rationale": r.get("rationale") or "",
                         "evidence": _evidence(r), "status": (state.get(r["id"]) or {}).get("status", "compiled")})
        metrics = [{"id": m["id"], "name": m["name"], "description": m.get("description") or "",
                    "expression": m.get("expression"), "sql": m.get("sql"), "source": m.get("source"),
                    "usage": m.get("usage"), "status": (state.get(m["id"]) or {}).get("status", "compiled")}
                   for m in v["metrics"] if m.get("term_id") == tid or table in (m.get("tables") or [])]
        bridges = [{"id": b["id"], "table": b["table_id"], "table_name": b["table_name"],
                    "targets": [p["target_table"] for p in b["fk_pairs"]]}
                   for b in v["bridges"] if b["table_id"] == table or any(p["target_table"] == table
                                                                          for p in b["fk_pairs"])]
        run_id = next((p.get("run_id") for p in t.get("provenance") or [] if p.get("run_id")), None) or v["run_id"]
        run: dict = {"id": run_id}
        try:
            r = self.onto.client.run(run_id)
            run.update(version=r.get("version"), mode=r.get("mode"), state=r.get("state"),
                       started_ms=r.get("started_ms"), finished_ms=r.get("finished_ms"),
                       requested_by=r.get("requested_by"))
        except OntologyError:
            pass
        queries = sorted({q for rel in rels for q in (rel["evidence"].get("query_log") or {}).get("query_ids", [])}
                         | {q for m in v["metrics"] if m.get("term_id") == tid for q in m.get("source_query_ids") or []})
        related = {tid} | {r["id"] for r in rels} | {m["id"] for m in metrics}
        proposals = [p for p in self.onto.proposals(can_view, "OPEN") if p.get("element_id") in related]
        st = state.get(tid) or {}
        return {
            "id": t["id"], "name": t["name"], "description": t.get("description") or "", "synonyms": t.get("synonyms") or [],
            "proposed_name": t.get("proposed_name") or t["name"], "table": table, "table_name": t["table_name"],
            **self._place(table, meta), "status": st.get("status", "compiled"),
            "glossary_term": OntologyCompiler.term_urn(t["name"]) if st.get("status") == "accepted" else None,
            "attributes": attributes,
            "date_formats": [{"column": a["column"], "name": a["name"], "format": a["format"]}
                             for a in attributes if a.get("format")],
            "relationships": rels, "metrics": metrics, "bridges": bridges,
            "provenance": {"run": run, "version": v["version"],
                           "sources": [{"source": p["source"], "detail": p.get("detail", ""),
                                        "confidence": p.get("confidence"), "run_id": p.get("run_id")}
                                       for p in t.get("provenance") or []],
                           "inputs": {"tables": sorted({table} | {r["other_table"] for r in rels}),
                                      "columns": [a["column"] for a in attributes], "queries": queries[:50]}},
            "proposals": proposals,
        }

    # ---- one dataset ---------------------------------------------------------------------------------------
    def asset(self, can_view: CanView, urn: str) -> dict:
        if not can_view(urn):
            raise OntologyError("no such dataset", 404)
        v = self.onto.view(can_view)
        state = self._review_state()
        tot = self._term_of_table(v)
        terms = [{"id": t["id"], "name": t["name"], "description": t.get("description") or "",
                  "synonyms": t.get("synonyms") or [], "status": (state.get(t["id"]) or {}).get("status", "compiled")}
                 for t in v["terms"] if t["table_id"] == urn]
        columns = [{"column": a["column"], "name": a["name"], "term": a["term_id"], "term_name": a["term_name"],
                    "format": a.get("format"), "datatype": a.get("datatype")}
                   for a in v["attributes"] if a["table_id"] == urn]
        rels = []
        for r in v["relationships"]:
            if urn not in (r["source_table"], r["target_table"]):
                continue
            other = r["target_table"] if r["source_table"] == urn else r["source_table"]
            ot = tot.get(other)
            rels.append({"id": r["id"], "column": r["source_column"] if r["source_table"] == urn else r["target_column"],
                         "other_table": other, "other_column": r["target_column"] if r["source_table"] == urn
                         else r["source_column"], "other_term": {"id": ot["id"], "name": ot["name"]} if ot else None,
                         "cardinality": r["cardinality"], "confidence": round(float(r.get("confidence") or 0), 2)})
        bridge = any(b["table_id"] == urn for b in v["bridges"])
        return {"urn": urn, "version": v["version"], "terms": terms, "columns": columns, "relationships": rels,
                "bridge": bridge}

    # ---- search ---------------------------------------------------------------------------------------------
    def search_terms(self, can_view: CanView, q: str, limit: int = 10) -> list[dict]:
        """Terms whose name or a synonym matches ``q`` (exact first, then prefix, then contained)."""
        ql = q.lower().strip()
        if not ql:
            return []
        out = []
        for t in self.onto.view(can_view)["terms"]:
            name = t["name"].lower()
            syn = next((s for s in t.get("synonyms") or [] if ql in s.lower()), None)
            if name == ql or (syn and syn.lower() == ql):
                score = 1.0
            elif name.startswith(ql):
                score = 0.8
            elif ql in name or syn:
                score = 0.6
            else:
                continue
            out.append({"id": t["id"], "name": t["name"], "description": t.get("description") or "",
                        "synonyms": t.get("synonyms") or [], "table": t["table_id"], "table_name": t["table_name"],
                        "matched": "synonym" if syn and ql not in name else "name", "synonym": syn, "score": score})
        out.sort(key=lambda h: (-h["score"], h["name"]))
        return out[:limit]

    # ---- export -------------------------------------------------------------------------------------------
    def csv(self, can_view: CanView) -> str:
        v = self.onto.view(can_view)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["kind", "id", "name", "table", "column", "related_table", "related_column", "cardinality",
                    "confidence", "provenance", "description"])
        for t in v["terms"]:
            w.writerow(["term", t["id"], t["name"], t["table_id"], "", "", "", "",
                        max([p.get("confidence") or 0 for p in t.get("provenance") or []] or [""]),
                        " ".join(p["source"] for p in t.get("provenance") or []), t.get("description") or ""])
        for a in v["attributes"]:
            w.writerow(["attribute", a["id"], a["name"], a["table_id"], a["column"], "", "", "", "",
                        " ".join(p["source"] for p in a.get("provenance") or []), a.get("description") or ""])
        for r in v["relationships"]:
            w.writerow(["relationship", r["id"], f"{r['source_column']} = {r['target_column']}", r["source_table"],
                        r["source_column"], r["target_table"], r["target_column"], r["cardinality"], r["confidence"],
                        " ".join(p["source"] for p in r.get("provenance") or []), r.get("rationale") or ""])
        for m in v["metrics"]:
            w.writerow(["metric", m["id"], m["name"], " ".join(m.get("tables") or []), "", "", "", "", "",
                        m.get("source") or "", m.get("description") or ""])
        return buf.getvalue()

    # ---- runs: scope, estimate, diff -------------------------------------------------------------------------
    def catalog(self, can_view: CanView) -> dict:
        """The compiler's tables the caller may see, for the compile wizard's pickers."""
        tables = []
        for t in self.onto.client.catalog().get("tables") or []:
            if not can_view(t["id"]):
                continue
            _, catalog, schema, name = table_parts(t["id"])
            tables.append({"id": t["id"], "name": t.get("name") or name, "catalog": catalog, "schema": schema,
                           "columns": t.get("columns")})
        tables.sort(key=lambda t: (t["catalog"], t["schema"], t["name"]))
        return {"tables": tables, "schemas": sorted({f"{t['catalog']}.{t['schema']}".strip(".") for t in tables})}

    def scope(self, can_view: CanView, tables: Optional[list[str]], schemas: Optional[list[str]]) -> Optional[list[str]]:
        """The table ids a scoped run compiles; every one must be a table the caller may see (fail closed)."""
        if not tables and not schemas:
            return None
        known = self.catalog(can_view)["tables"]
        ids = {t["id"] for t in known}
        out: set[str] = set()
        for urn in tables or []:
            if urn not in ids:
                raise OntologyError(f"not a table you may compile: {urn}", 422)
            out.add(urn)
        for s in schemas or []:
            hit = {t["id"] for t in known if s in (t["schema"], f"{t['catalog']}.{t['schema']}".strip("."))}
            if not hit:
                raise OntologyError(f"no tables you may compile in schema {s}", 422)
            out |= hit
        return sorted(out)

    def diff(self, can_view: CanView, rid: str, against: Optional[str] = None) -> dict:
        """What changed in ``rid`` since ``against`` (default: the completed run before it), over what the caller
        may see."""
        runs = self.onto.client.runs().get("runs") or []
        this = next((r for r in runs if r["id"] == rid), None)
        if this is None:
            this = self.onto.client.run(rid)
        if this.get("state") != "completed":
            raise OntologyError(f"run {rid} is {this.get('state')}: only a completed run has an ontology", 409)
        if against is None:
            before = [r for r in runs if r["state"] == "completed" and r["version"] < this["version"]]
            against = max(before, key=lambda r: r["version"])["id"] if before else None
        new = OntologyCompiler.visible(self.onto.client.ontology(rid), can_view)
        old = OntologyCompiler.visible(self.onto.client.ontology(against), can_view) if against else \
            {"terms": [], "relationships": [], "metrics": [], "attributes": [], "bridges": []}

        def label(kind: str, e: dict) -> str:
            if kind == "relationships":
                return (f"{table_parts(e['source_table'])[3]}.{e['source_column']} → "
                        f"{table_parts(e['target_table'])[3]}.{e['target_column']}")
            return e.get("name") or e["id"]

        def fields(kind: str, e: dict) -> dict:
            if kind == "terms":
                return {"name": e["name"], "description": e.get("description"), "synonyms": sorted(e.get("synonyms") or []),
                        "attributes": len(e.get("attribute_ids") or [])}
            if kind == "relationships":
                return {"cardinality": e["cardinality"], "confidence": round(float(e.get("confidence") or 0), 2),
                        "provenance": sorted(p["source"] for p in e.get("provenance") or []),
                        "join_default": e.get("join_default", True)}
            return {"name": e.get("name"), "expression": e.get("expression"), "sql": e.get("sql")}

        out: dict[str, Any] = {"run": rid, "against": against, "version": this.get("version")}
        for kind in ("terms", "relationships", "metrics"):
            a = {e["id"]: e for e in old.get(kind) or []}
            b = {e["id"]: e for e in new.get(kind) or []}
            added = [{"id": i, "label": label(kind, b[i])} for i in sorted(b.keys() - a.keys())]
            removed = [{"id": i, "label": label(kind, a[i])} for i in sorted(a.keys() - b.keys())]
            changed = []
            for i in sorted(a.keys() & b.keys()):
                fa, fb = fields(kind, a[i]), fields(kind, b[i])
                delta = {k: {"before": fa[k], "after": fb[k]} for k in fa if fa[k] != fb[k]}
                if delta:
                    changed.append({"id": i, "label": label(kind, b[i]), "changes": delta})
            out[kind] = {"added": added[:500], "removed": removed[:500], "changed": changed[:500],
                         "counts": {"added": len(added), "removed": len(removed), "changed": len(changed),
                                    "unchanged": len(a.keys() & b.keys()) - len(changed)}}
        return out

    # ---- deciding proposals ------------------------------------------------------------------------------------
    def _raw(self, fid: str) -> Any:
        with self.onto.engine.connect() as conn:
            return conn.execute(select(findings_table).where(findings_table.c.id == fid)).first()

    def _note(self, fid: str, review: dict, edits: Optional[dict] = None) -> None:
        """Keeps the decision (who, when, comment) with the proposal, and applies a reviewer's edits to a term."""
        with self.onto.engine.begin() as conn:
            row = conn.execute(select(findings_table.c.doc).where(findings_table.c.id == fid)).first()
            doc = dict(row.doc or {})
            data = dict(doc.get("data") or {})
            data["reviews"] = list(data.get("reviews") or []) + [review]
            if edits:
                changes = []
                for c in doc.get("proposed_changes") or []:
                    if c.get("kind") == "create_term":
                        val = dict(c["value"])
                        if edits.get("name"):
                            val["name"] = edits["name"]
                        if edits.get("definition"):
                            val["definition"] = edits["definition"]
                        if edits.get("synonyms") is not None:
                            val["synonyms"] = list(edits["synonyms"])
                        c = {**c, "value": val}
                    changes.append(c)
                doc["proposed_changes"] = changes
                if edits.get("name") and data.get("kind") == "term":
                    doc["title"] = f"Add glossary term “{edits['name']}”"
                data["edited"] = {k: v for k, v in edits.items() if v is not None}
            doc["data"] = data
            conn.execute(update(findings_table).where(findings_table.c.id == fid).values(doc=doc))

    def decide(self, principal, authorize, can_view: CanView, fid: str, action: str, comment: str = "",
               edits: Optional[dict] = None) -> dict:
        """Accepts or rejects one proposal as ``principal``; raises :class:`ReviewError` when it may not."""
        if self.context is None:
            raise ReviewError("the context inbox is not configured", 503)
        if action not in ("accept", "reject"):
            raise ReviewError("action is accept or reject", 422)
        if getattr(principal, "via", "") == "agent":
            raise ReviewError("reviewing the ontology is for people, not agents", 403)
        row = self._raw(fid)
        data = ((row.doc or {}).get("data") or {}) if row is not None else {}
        # Not an ontology proposal, or about a dataset the caller may not see: not found either way.
        if row is None or data.get("source") != SOURCE or not all(
                can_view(e) for e in row.entities or [] if e.startswith("urn:li:")):
            raise ReviewError("proposal not found", 404)
        if row.status != "OPEN":
            raise ReviewError(f"the proposal is already {STATUS.get(row.status, row.status.lower())}", 409)
        authorize(principal, "MANAGE_GLOSSARIES", None)
        if action == "accept":
            for e in row.entities or []:
                if e.startswith("urn:li:dataset:"):
                    authorize(principal, "EDIT_ENTITY_GLOSSARY_TERMS", e)
        if edits and (action != "accept" or data.get("kind") != "term"):
            raise ReviewError("only a term proposal can be edited, as it is accepted", 422)
        review = {"action": action, "by": principal.urn, "at_ms": _now_ms(), "comment": comment[:2000]}
        self._note(fid, review, edits)
        if action == "reject":
            out = self.context.set_status(fid, "DISMISSED", principal.urn)
        else:
            try:
                out = self.context.accept(fid, principal.urn)
            except ValueError as e:
                raise ReviewError(str(e), 409)
        if self.audit is not None:
            self.audit.record("ontology", f"ontology.proposal_{'accepted' if action == 'accept' else 'rejected'}",
                              principal.urn, fid, {"kind": data.get("kind"), "element_id": data.get("element_id"),
                                                   "comment": comment[:2000], "edited": bool(edits)})
        self.onto._cache = (0.0, None)
        return out

    def decide_many(self, principal, authorize, can_view: CanView, ids: list[str], action: str,
                    comment: str = "") -> dict:
        """Each proposal decided on its own (one refusal does not stop the others); the outcome of each."""
        if len(ids) > MAX_BATCH:
            raise ReviewError(f"at most {MAX_BATCH} proposals at a time", 422)
        if getattr(principal, "via", "") == "agent":
            raise ReviewError("reviewing the ontology is for people, not agents", 403)
        from fastapi import HTTPException

        results = []
        for fid in dict.fromkeys(ids):
            try:
                out = self.decide(principal, authorize, can_view, fid, action, comment)
                results.append({"id": fid, "ok": True, "status": out.get("status")})
            except ReviewError as e:
                results.append({"id": fid, "ok": False, "status_code": e.status, "error": str(e)})
            except HTTPException as e:          # the PDP's refusal
                results.append({"id": fid, "ok": False, "status_code": e.status_code, "error": str(e.detail)})
        done = sum(1 for r in results if r["ok"])
        if self.audit is not None:
            self.audit.record("ontology", f"ontology.proposals_{action}_batch", principal.urn, "ontology",
                              {"requested": len(results), "done": done, "comment": comment[:2000]})
        return {"action": action, "done": done, "refused": len(results) - done, "results": results}
