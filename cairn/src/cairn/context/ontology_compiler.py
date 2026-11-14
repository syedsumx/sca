"""Cairn Ontology: the ontology compiler (``cairn-ontology``, a Rust service) fed from Cairn and read back into Cairn.

The compiler is a port of the compiler at the heart of NVIDIA's open-source auto-ontology (Apache-2.0; see
``ontology/NOTICE``). Cairn is its only client:

* **Sync** (on a schedule and on demand): Cairn pushes its catalog (every dataset in the metadata graph with its
  columns, types, nullability, keys, descriptions, deprecation and profile, plus a sample of rows read through the
  governed data path as ``CAIRN_ONTOLOGY_SAMPLE_AS``, masking applied) and its Trino query history (``/v1/queries``
  data: SQL, how often, by whom, when) to the compiler, which diffs the catalog and merges equivalent queries.
* **Compile**: the compiler profiles columns, detects foreign keys and junction tables, names business Terms and
  their columns, resolves relationships (declared, inferred, observed in queries), suggests metrics from the query
  log and join patterns for bridge tables, and embeds what it built. Its model calls go through Cairn's AI gateway
  under its own agent identity (scope ``model``), so they are governed and metered.
* **Proposals**: a finished run comes back into the context inbox as findings: a Term becomes a glossary-term
  proposal linked to its columns, a relationship a join proposal, a metric a metric definition. Accepting one
  (``EDIT_ENTITY_GLOSSARY_TERMS`` on its datasets) writes the glossary term and its column links, or records the
  approved join or metric that Ask-your-data and text to SQL are then given.
* **Reads** (terms, relationships, metrics, search, graph, provenance, SQL validation) follow catalog visibility:
  a person sees an element only when they may see every dataset it is about.
* **Ask-your-data**: the relationships among the tables it chose ground the SQL it writes, and the SQL is then
  checked with the compiler's ``/validate-sql``; the findings are shown. When the compiler is down the answer
  carries a warning instead: it never blocks governed execution.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Optional

import httpx
from sqlalchemy import select

from cairn.common import metrics
from cairn.common.db import advisory_lock
from cairn.context.miner import Finding, findings_table

logger = logging.getLogger(__name__)

ONTOLOGY_NODE = "urn:li:glossaryNode:Ontology"
SOURCE = "ontology"
LOCK_KEY = 0x0D4E_0121
COMPILE_PRIVILEGE = "MANAGE_GLOSSARIES"
AGENT_SCOPES = ["model"]
_DATASET = re.compile(r"^urn:li:dataset:\(urn:li:dataPlatform:([^,]+),(.+),[^,]+\)$")


class OntologyError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _secret(name: str) -> str:
    path = os.environ.get(f"{name}_FILE")
    if path:
        with open(path) as fh:
            return fh.read().strip()
    return os.environ.get(name, "")


def table_parts(urn: str) -> tuple[str, str, str, str]:
    """(platform, catalog, schema, name) of a dataset urn."""
    m = _DATASET.match(urn)
    if not m:
        return "", "", "", urn
    parts = m.group(2).split(".")
    name = parts[-1]
    schema = parts[-2] if len(parts) > 1 else ""
    catalog = ".".join(parts[:-2]) if len(parts) > 2 else ""
    return m.group(1), catalog, schema, name


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", name.title()) or "Term"


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)


# --------------------------------------------------------------------------------------------- client
class OntologyClient:
    """The compiler's HTTP API, with its bearer token (from a file; never logged)."""

    def __init__(self, url: str, token: str, timeout: float = 30.0, http: Optional[httpx.Client] = None):
        if not url.startswith(("http://", "https://")):
            raise ValueError("CAIRN_ONTOLOGY_URL: an http(s) URL")
        if len(token) < 16:
            raise ValueError("the ontology compiler's token must be at least 16 characters")
        self.url = url.rstrip("/")
        self._token = token
        self.http = http or httpx.Client(timeout=timeout)

    def _call(self, method: str, path: str, body: Any = None, params: Optional[dict] = None,
              raw: bool = False, timeout: Optional[float] = None) -> Any:
        try:
            r = self.http.request(method, f"{self.url}{path}", json=body, params=params,
                                  headers={"Authorization": f"Bearer {self._token}"},
                                  **({"timeout": timeout} if timeout else {}))
        except httpx.HTTPError as e:
            raise OntologyError(f"the ontology compiler is not reachable: {type(e).__name__}", 503) from None
        if r.status_code >= 400:
            try:
                msg = r.json().get("error") or r.text
            except ValueError:
                msg = r.text
            raise OntologyError(str(msg)[:500], r.status_code if r.status_code < 500 else 502)
        return r.text if raw else r.json()

    def ready(self) -> bool:
        try:
            return self.http.get(f"{self.url}/-/ready", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False

    def put_catalog(self, snapshot: dict) -> dict:
        return self._call("PUT", "/v1/catalog", snapshot, timeout=300)

    def post_queries(self, queries: list[dict], dialects: Optional[list[str]] = None) -> dict:
        return self._call("POST", "/v1/queries", {"queries": queries, "dialects": dialects or []}, timeout=300)

    def start_run(self, mode: str, requested_by: str, tables: Optional[list[str]] = None) -> dict:
        body: dict = {"mode": mode, "requested_by": requested_by}
        if tables:
            body["tables"] = tables
        return self._call("POST", "/v1/runs", body)

    def estimate(self, mode: str, tables: Optional[list[str]] = None) -> dict:
        return self._call("POST", "/v1/estimate", {"mode": mode, **({"tables": tables} if tables else {})})

    def catalog(self) -> dict:
        return self._call("GET", "/v1/catalog", timeout=120)

    def runs(self) -> dict:
        return self._call("GET", "/v1/runs")

    def run(self, run_id: str) -> dict:
        return self._call("GET", f"/v1/runs/{run_id}")

    def cancel(self, run_id: str) -> dict:
        return self._call("POST", f"/v1/runs/{run_id}/cancel")

    def resume(self, run_id: str) -> dict:
        return self._call("POST", f"/v1/runs/{run_id}/resume")

    def ontology(self, run_id: Optional[str] = None) -> dict:
        return self._call("GET", "/v1/ontology", params={"run": run_id} if run_id else None, timeout=120)

    def search(self, q: str, mode: str, limit: int) -> dict:
        return self._call("POST", "/v1/search", {"q": q, "mode": mode, "limit": limit})

    def graph(self, fmt: str, tables: Optional[list[str]]) -> Any:
        return self._call("POST", "/v1/graph", {"format": fmt, "tables": tables}, raw=fmt != "json")

    def validate(self, sql: str, dialects: Optional[list[str]] = None, definitions: Optional[list[dict]] = None,
                 timeout: Optional[float] = None) -> dict:
        return self._call("POST", "/v1/validate-sql", {"sql": sql, "dialects": dialects or [],
                                                       "definitions": definitions or []}, timeout=timeout)

    def provenance(self, kind: str, eid: str) -> dict:
        return self._call("GET", f"/v1/provenance/{kind}/{eid}")


# --------------------------------------------------------------------------------------------- service
class OntologyCompiler:
    def __init__(self, client: OntologyClient, engine, graph, context=None, query_history=None, data_access=None,
                 audit=None, sample_as: Optional[str] = None, sample_rows: int = 200, interval_seconds: int = 3600,
                 table_allow: Optional[str] = None, table_deny: Optional[str] = None, agents=None):
        self.client = client
        self.engine = engine
        self.graph = graph
        self.context = context          # ContextStore: proposals land in its findings inbox
        self.query_history = query_history
        self.data_access = data_access
        self.audit = audit
        self.sample_as = sample_as
        self.sample_rows = sample_rows
        self.interval = interval_seconds
        self.table_allow, self.table_deny = table_allow, table_deny
        self.agents = agents
        self._cache: tuple[float, Optional[dict]] = (0.0, None)
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, engine, graph, **kw) -> Optional["OntologyCompiler"]:
        url = os.environ.get("CAIRN_ONTOLOGY_URL", "").strip()
        if not url:
            return None
        token = _secret("CAIRN_ONTOLOGY_TOKEN")
        client = OntologyClient(url, token, float(os.environ.get("CAIRN_ONTOLOGY_TIMEOUT_SECONDS", "30")))
        return cls(client, engine, graph, sample_as=os.environ.get("CAIRN_ONTOLOGY_SAMPLE_AS") or None,
                   sample_rows=int(os.environ.get("CAIRN_ONTOLOGY_SAMPLE_ROWS", "200")),
                   interval_seconds=int(os.environ.get("CAIRN_ONTOLOGY_INTERVAL_SECONDS", "3600")),
                   table_allow=os.environ.get("CAIRN_ONTOLOGY_TABLE_ALLOW_REGEX") or None,
                   table_deny=os.environ.get("CAIRN_ONTOLOGY_TABLE_DENY_REGEX") or None, **kw)

    def _audit(self, action: str, actor: str, subject: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("ontology", action, actor, subject, details)

    # ---- the compiler's identity ------------------------------------------------------------
    def create_agent(self, owner: str, actor: str) -> dict:
        """The compiler's agent identity for model calls through the AI gateway: a key agent with scope ``model``
        acting for ``owner`` (who needs ``USE_AI_GATEWAY`` on the model). The key is returned once: put it in the
        compiler's ``CAIRN_ONTOLOGY_MODEL_API_KEY_FILE`` (a Kubernetes Secret)."""
        from cairn.identity.agents import AgentIn

        if self.agents is None:
            raise OntologyError("agents are not configured", 503)
        made = self.agents.create_key_agent(AgentIn(name="Cairn Ontology compiler", scopes=AGENT_SCOPES, owner=owner,
                                                    description="Model calls of the ontology compiler"), actor)
        self._audit("ontology.agent_created", actor, made["id"], {"owner": owner})
        return made

    # ---- catalog -----------------------------------------------------------------------------
    def _profile(self, urn: str) -> dict:
        getter = getattr(self.graph, "get_timeseries", None)
        if getter is None:
            return {}
        try:
            rows = getter(urn, "datasetProfile", 1)
        except Exception:  # noqa: BLE001 - a profile is optional
            return {}
        return rows[0] if rows else {}

    def _samples(self, urn: str) -> dict[str, list]:
        """Column → sampled values, read as ``sample_as`` through the governed data path (masking applied)."""
        if not self.sample_as or self.data_access is None:
            return {}
        try:
            out = self.data_access.preview(self.sample_as, urn, self.sample_rows)
        except Exception as e:  # noqa: BLE001 - a table without a sample still compiles
            logger.info("ontology: no sample of %s: %s", urn, e)
            return {}
        cols = out.get("columns") or []
        masked = set(out.get("masked_columns") or [])
        samples: dict[str, list] = {c: [] for c in cols if c not in masked}
        # In a stable order: a table whose rows did not change is the same snapshot (an incremental run reuses it).
        for row in sorted(out.get("rows") or [], key=lambda r: json.dumps(_jsonable(r), sort_keys=True)):
            for c, v in zip(cols, row):
                if c in samples:
                    samples[c].append(_jsonable(v))
        return samples

    def snapshot(self) -> dict:
        """The catalog as the compiler takes it."""
        if self.graph is None:
            raise OntologyError("the metadata graph is not configured", 503)
        rows = self.graph.scroll_entities("dataset", ["schemaMetadata", "editableSchemaMetadata", "datasetProperties",
                                                      "editableDatasetProperties", "status", "deprecation"])
        tables = []
        for row in rows:
            urn = row["urn"]
            schema = row.get("schemaMetadata") or {}
            fields = schema.get("fields") or []
            if not fields or (row.get("status") or {}).get("removed"):
                continue
            _, catalog, sch, name = table_parts(urn)
            editable = {i.get("fieldPath"): i for i in
                        (row.get("editableSchemaMetadata") or {}).get("editableSchemaFieldInfo", [])}
            profile = self._profile(urn)
            fprof = {p.get("fieldPath"): p for p in profile.get("fieldProfiles") or []}
            samples = self._samples(urn)
            pk = list(schema.get("primaryKeys") or [f["fieldPath"] for f in fields if f.get("isPartOfKey")])
            fks = []
            for fk in schema.get("foreignKeys") or []:
                src = [str(u).rsplit(",", 1)[-1].rstrip(")") for u in fk.get("sourceFields") or []]
                dst = [str(u).rsplit(",", 1)[-1].rstrip(")") for u in fk.get("foreignFields") or []]
                if fk.get("foreignDataset") and src:
                    fks.append({"columns": src, "target_table": fk["foreignDataset"], "target_columns": dst})
            columns = []
            for i, f in enumerate(fields):
                path = f["fieldPath"]
                col = path.split(".")[-1] if path.startswith("[") else path
                ed = editable.get(path) or {}
                tags = [t.get("tag", "") for t in ((f.get("globalTags") or {}).get("tags") or [])
                        + ((ed.get("globalTags") or {}).get("tags") or [])]
                desc = ed.get("description") or f.get("description")
                p = fprof.get(path) or {}
                distinct = None
                if p.get("distinctValueFrequencies"):
                    distinct = [d.get("value") for d in p["distinctValueFrequencies"]]
                columns.append({
                    "name": col, "data_type": f.get("nativeDataType") or "", "nullable": f.get("nullable"),
                    "ordinal": i + 1, "description": desc,
                    "samples": samples.get(col) or [_jsonable(v) for v in (p.get("sampleValues") or [])],
                    "distinct_values": distinct,
                    "is_unique": (p.get("uniqueCount") is not None and profile.get("rowCount")
                                  and p.get("uniqueCount") == profile.get("rowCount") - (p.get("nullCount") or 0)) or None,
                    "null_count": p.get("nullCount"),
                    "deprecated": any("deprecated" in t.lower() for t in tags)
                                  or bool(desc and str(desc).upper().startswith("DEPRECATED")),
                    "tags": tags,
                })
            props = row.get("datasetProperties") or {}
            tables.append({
                "id": urn, "catalog": catalog, "schema": sch, "name": name,
                "table_type": ((props.get("customProperties") or {}).get("table_type") or "base table").lower(),
                "description": (row.get("editableDatasetProperties") or {}).get("description") or props.get("description"),
                "row_count": profile.get("rowCount"), "primary_key": pk, "foreign_keys": fks,
                "deprecated": bool((row.get("deprecation") or {}).get("deprecated")), "columns": columns,
            })
        snap: dict = {"database": "cairn", "tables": tables, "replace": True}
        if self.table_allow or self.table_deny:
            snap["filter"] = {"table_allow_regex": self.table_allow, "table_deny_regex": self.table_deny}
        return snap

    def push_catalog(self) -> dict:
        return self.client.put_catalog(self.snapshot())

    # ---- query log ---------------------------------------------------------------------------
    def push_queries(self, batch: int = 2000) -> dict:
        """Finished Trino queries since the last push (Cairn's query history), in batches."""
        if self.query_history is None:
            return {"received": 0, "note": "no query history (Trino's event listener is not configured)"}
        from cairn.dataaccess.query_history import query_history as qh

        cursor = int(self._cursor("ontology_queries") or 0)
        with self.engine.connect() as conn:
            rows = conn.execute(select(qh.c.query_id, qh.c.sql, qh.c.user, qh.c.create_ms)
                                .where(qh.c.state == "FINISHED", qh.c.create_ms > cursor)
                                .order_by(qh.c.create_ms).limit(50_000)).all()
        totals = {"received": 0, "added": 0, "merged": 0, "skipped": 0, "failed": 0}
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            out = self.client.post_queries([{"id": r.query_id, "sql": r.sql, "count": 1, "user": r.user,
                                             "ts_ms": int(r.create_ms)} for r in chunk if r.sql], ["generic"])
            for k in ("received", "added", "merged", "skipped"):
                totals[k] += int(out.get(k) or 0)
            totals["failed"] += len(out.get("failed") or [])
            self._set_cursor("ontology_queries", int(chunk[-1].create_ms))
        return totals

    def _cursor(self, key: str) -> Any:
        return self.context.cursor(key) if self.context is not None else None

    def _set_cursor(self, key: str, value: Any) -> None:
        if self.context is not None:
            self.context.set_cursor(key, value)

    # ---- compiling ---------------------------------------------------------------------------
    def sync(self, actor: str) -> dict:
        out = {"catalog": self.push_catalog(), "queries": self.push_queries()}
        self._audit("ontology.synced", actor, "ontology", {"tables": out["catalog"].get("tables"),
                                                           "queries": out["queries"].get("received")})
        return out

    def compile(self, actor: str, mode: str = "incremental", sync: bool = True, wait: bool = False,
                timeout: float = 1800, tables: Optional[list[str]] = None) -> dict:
        """A run (``tables``: only these are compiled; the others keep what the published run made of them)."""
        if mode not in ("full", "incremental"):
            raise OntologyError("mode is full or incremental", 422)
        synced = self.sync(actor) if sync else None
        run = self.client.start_run(mode, actor, tables)
        self._audit("ontology.compile_started", actor, run["id"],
                    {"mode": mode, **({"tables": len(tables)} if tables else {})})
        out: dict = {"run": run, "sync": synced}
        if wait:
            run = self.wait(run["id"], timeout)
            out["run"] = run
            if run["state"] == "completed":
                out["imported"] = self.import_run(run["id"], actor)
        return out

    def wait(self, run_id: str, timeout: float = 1800) -> dict:
        end = time.monotonic() + timeout
        while True:
            run = self.client.run(run_id)
            if run["state"] != "running" or time.monotonic() > end:
                return run
            time.sleep(0.5)

    # ---- proposals -----------------------------------------------------------------------------
    @staticmethod
    def term_urn(name: str) -> str:
        return f"urn:li:glossaryTerm:Ontology.{_slug(name)}"

    def findings(self, o: dict) -> list[Finding]:
        attrs: dict[str, list[dict]] = {}
        for a in o.get("attributes") or []:
            attrs.setdefault(a["term_id"], []).append(a)
        out: list[Finding] = []
        base = {"source": SOURCE, "run_id": o["run_id"], "version": o["version"]}
        for t in o.get("terms") or []:
            term_urn = self.term_urn(t["name"])
            cols = attrs.get(t["id"], [])
            col_lines = "\n".join(f"- `{a['column']}` → **{a['name']}**" + (f" — {a['description']}" if a.get("description")
                                                                              else "") for a in cols[:40])
            changes = [{"kind": "create_term", "entity_urn": None, "field_path": None,
                        "value": {"name": t["name"], "definition": t.get("description") or t["name"],
                                  "expression": "", "urn": term_urn, "parent": ONTOLOGY_NODE,
                                  "parent_name": "Ontology", "synonyms": t.get("synonyms") or []}}]
            changes += [{"kind": "add_term", "entity_urn": t["table_id"], "field_path": a["column"], "value": term_urn}
                        for a in cols]
            out.append(Finding(
                type="TERM_SUGGESTION", key=("ontology", t["table_id"], t["name"].lower()),
                title=f"Add glossary term “{t['name']}”",
                summary_md=f"{t.get('description') or ''}\n\nCompiled from `{t.get('table_name')}` (ontology run "
                           f"{o['version']}). Linked columns:\n{col_lines}",
                confidence=max([p.get("confidence") or 0.5 for p in t.get("provenance") or []] or [0.5]),
                entities=[t["table_id"]],
                evidence=[{"source": p["source"], "ref": p.get("run_id") or "", "snippet": p.get("detail", "")[:300]}
                          for p in t.get("provenance") or []],
                proposed_changes=changes,
                data={**base, "kind": "term", "element_id": t["id"], "synonyms": t.get("synonyms") or [],
                      "attributes": [{"column": a["column"], "name": a["name"], "format": a.get("format")} for a in cols]}))
        for r in o.get("relationships") or []:
            st, tt = table_parts(r["source_table"]), table_parts(r["target_table"])
            out.append(Finding(
                type="JOIN_RELATIONSHIP",
                key=("ontology", r["source_table"], r["source_column"].lower(), r["target_table"], r["target_column"].lower()),
                title=f"{st[2]}.{st[3]}.{r['source_column']} → {tt[2]}.{tt[3]}.{r['target_column']}",
                summary_md=f"`{st[3]}.{r['source_column']}` references `{tt[3]}.{r['target_column']}` "
                           f"({r['cardinality'].replace('_', ' ')}); evidence: "
                           + "; ".join(f"{p['source']}: {p.get('detail', '')}" for p in r.get("provenance") or []),
                confidence=round(float(r.get("confidence") or 0.5), 2), entities=[r["source_table"], r["target_table"]],
                evidence=[{"source": p["source"], "ref": p.get("run_id") or "", "snippet": p.get("detail", "")[:300]}
                          for p in r.get("provenance") or []],
                proposed_changes=[{"kind": "create_relationship", "entity_urn": r["source_table"],
                                   "field_path": r["source_column"],
                                   "value": {"source": r["source_table"], "destination": r["target_table"],
                                             "source_field": r["source_column"],
                                             "destination_field": r["target_column"]}}],
                data={**base, "kind": "relationship", "element_id": r["id"],
                      "provenance": [p["source"] for p in r.get("provenance") or []],
                      "join_default": r.get("join_default", True), "evidence": r.get("evidence")}))
        for m in o.get("metrics") or []:
            if not m.get("tables"):
                continue
            out.append(Finding(
                type="METRIC_DEFINITION", key=("ontology", m["term_id"], m["name"].lower()),
                title=f"Metric “{m['name']}” = {m['expression']}",
                summary_md=f"{m.get('description') or ''}\n\nOn **{m['term_name']}**, as SQL:\n\n```sql\n{m['sql']}\n```",
                confidence=max([p.get("confidence") or 0.5 for p in m.get("provenance") or []] or [0.5]),
                entities=list(m["tables"]),
                evidence=[{"source": "query", "ref": q, "snippet": ""} for q in (m.get("source_query_ids") or [])[:5]],
                proposed_changes=[{"kind": "create_term", "entity_urn": None, "field_path": None,
                                   "value": {"name": m["name"], "definition": m.get("description") or m["expression"],
                                             "expression": m["expression"],
                                             "urn": f"urn:li:glossaryTerm:Ontology.Metric.{_slug(m['name'])}",
                                             "parent": ONTOLOGY_NODE, "parent_name": "Ontology"}}],
                data={**base, "kind": "metric", "element_id": m["id"], "name": m["name"], "canonical": m["expression"],
                      "description": m.get("description") or "", "sql": m["sql"], "metric_source": m["source"]}))
        return out

    def import_run(self, run_id: Optional[str], actor: str) -> dict:
        """The run's ontology (default: the published one) as proposals in the context inbox."""
        if self.context is None:
            raise OntologyError("the context inbox is not configured", 503)
        o = self.client.ontology(run_id)
        stats = self.context.upsert(self.findings(o))
        self._set_cursor("ontology_imported", {"run_id": o["run_id"], "version": o["version"], **stats})
        self._audit("ontology.imported", actor, o["run_id"], stats)
        self._cache = (0.0, None)
        return {"run_id": o["run_id"], "version": o["version"], **stats}

    def proposals(self, can_view: Callable[[str], bool], status: Optional[str] = "OPEN", kind: Optional[str] = None,
                  limit: int = 500) -> list[dict]:
        stmt = select(findings_table).order_by(findings_table.c.confidence.desc(), findings_table.c.updated_at_ms.desc())
        if status:
            stmt = stmt.where(findings_table.c.status == status)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        out = []
        for r in rows:
            data = (r.doc or {}).get("data") or {}
            if data.get("source") != SOURCE or (kind and data.get("kind") != kind):
                continue
            if not all(can_view(e) for e in r.entities or [] if e.startswith("urn:li:")):
                continue
            out.append({"id": r.id, "type": r.type, "kind": data.get("kind"), "status": r.status,
                        "confidence": r.confidence, "title": r.doc.get("title"), "summary_md": r.doc.get("summary_md"),
                        "entities": r.entities or [], "element_id": data.get("element_id"),
                        "version": data.get("version"), "proposed_changes": r.doc.get("proposed_changes")})
            if len(out) >= limit:
                break
        return out

    # ---- reads under catalog visibility ----------------------------------------------------------
    def published(self, max_age: float = 30.0) -> Optional[dict]:
        """The published ontology, cached briefly; None when there is none yet."""
        with self._lock:
            at, cached = self._cache
            if cached is not None and time.monotonic() - at < max_age:
                return cached
        try:
            o = self.client.ontology()
        except OntologyError as e:
            if e.status == 404:
                return None
            raise
        with self._lock:
            self._cache = (time.monotonic(), o)
        return o

    @staticmethod
    def visible(o: dict, can_view: Callable[[str], bool]) -> dict:
        memo: dict[str, bool] = {}

        def ok(urn: str) -> bool:
            if urn not in memo:
                memo[urn] = bool(can_view(urn))
            return memo[urn]
        return {
            "run_id": o.get("run_id"), "version": o.get("version"),
            "terms": [t for t in o.get("terms") or [] if ok(t["table_id"])],
            "attributes": [a for a in o.get("attributes") or [] if ok(a["table_id"])],
            "relationships": [r for r in o.get("relationships") or [] if ok(r["source_table"]) and ok(r["target_table"])],
            "metrics": [m for m in o.get("metrics") or [] if m.get("tables") and all(ok(t) for t in m["tables"])],
            "bridges": [b for b in o.get("bridges") or [] if ok(b["table_id"]) and all(ok(p["target_table"])
                                                                                       for p in b["fk_pairs"])],
            "tables": [t for t in o.get("tables") or [] if ok(t["table_id"])],
        }

    def view(self, can_view: Callable[[str], bool]) -> dict:
        o = self.published()
        if o is None:
            return {"run_id": None, "version": None, "terms": [], "attributes": [], "relationships": [], "metrics": [],
                    "bridges": [], "tables": []}
        return self.visible(o, can_view)

    def terms(self, can_view, q: str = "") -> list[dict]:
        v = self.view(can_view)
        attrs: dict[str, list] = {}
        for a in v["attributes"]:
            attrs.setdefault(a["term_id"], []).append({"column": a["column"], "name": a["name"],
                                                       "datatype": a["datatype"], "format": a.get("format")})
        ql = q.lower().strip()
        out = []
        for t in v["terms"]:
            if ql and ql not in t["name"].lower() and not any(ql in s.lower() for s in t.get("synonyms") or []):
                continue
            out.append({"id": t["id"], "name": t["name"], "description": t["description"], "synonyms": t["synonyms"],
                        "table": t["table_id"], "table_name": t["table_name"], "attributes": attrs.get(t["id"], []),
                        "provenance": [p["source"] for p in t.get("provenance") or []]})
        return out

    def relationships(self, can_view, datasets: Optional[list[str]] = None, between: bool = False) -> list[dict]:
        v = self.view(can_view)
        wanted = set(datasets or [])
        out = []
        for r in v["relationships"]:
            if wanted:
                ends = {r["source_table"], r["target_table"]}
                if (between and not ends <= wanted) or (not between and not ends & wanted):
                    continue
            out.append({"id": r["id"], "source": r["source_table"], "source_column": r["source_column"],
                        "target": r["target_table"], "target_column": r["target_column"],
                        "cardinality": r["cardinality"], "confidence": r["confidence"],
                        "join_default": r.get("join_default", True),
                        "provenance": [p["source"] for p in r.get("provenance") or []],
                        "evidence": r.get("evidence") or {}})
        return out

    def search(self, can_view, q: str, mode: str = "hybrid", limit: int = 10) -> list[dict]:
        hits = self.client.search(q, mode, min(100, limit * 4)).get("hits") or []
        out = []
        for h in hits:
            tables = h.get("tables") or ([h["table_id"]] if h.get("table_id") else [])
            if tables and all(can_view(t) for t in tables):
                out.append(h)
            if len(out) >= limit:
                break
        return out

    def graph_export(self, can_view, fmt: str) -> Any:
        v = self.view(can_view)
        tables = sorted({t["table_id"] for t in v["tables"]} | {t["table_id"] for t in v["terms"]})
        return self.client.graph(fmt, tables)

    def provenance(self, can_view, kind: str, eid: str) -> dict:
        p = self.client.provenance(kind, eid)
        if not all(can_view(t) for t in p.get("tables") or []):
            raise OntologyError("no such element", 404)      # not "forbidden": its existence is not disclosed
        return p

    def validate_sql(self, sql: str, can_view: Optional[Callable[[str], bool]] = None, dialect: Optional[str] = None,
                     definitions: Optional[list[dict]] = None, timeout: Optional[float] = None) -> dict:
        out = self.client.validate(sql, [dialect] if dialect and dialect != "trino" else ["generic"], definitions,
                                   timeout)
        if can_view is None:
            return out
        hidden = {u for u in (out.get("tables") or {}).values() if u and not can_view(u)}
        if hidden:
            # Nothing about tables the caller may not see: their findings and joins are left out.
            out["findings"] = [f for f in out.get("findings") or [] if not set(f.get("tables") or []) & hidden]
            out["findings"].append({"kind": "not_visible", "severity": "error", "tables": [],
                                    "message": "the query reads a table you may not see"})
            out["joins"] = []
            out["tables"] = {k: (v if v not in hidden else None) for k, v in (out.get("tables") or {}).items()}
            out["valid"] = False
        return out

    # ---- Ask-your-data -------------------------------------------------------------------------
    def join_definitions(self, urns: set[str]) -> list[dict]:
        """The relationships among ``urns`` as join definitions for text to SQL (empty when the compiler is down)."""
        try:
            o = self.published()
        except OntologyError as e:
            logger.warning("ontology: no relationships for grounding (%s)", e)
            return []
        if o is None:
            return []
        out = []
        # Every role between two tables (orders.customer_id and orders.buyer_ref both reach customers.id), the
        # default join of each group first: the question decides which role it means.
        rels = sorted(o.get("relationships") or [], key=lambda r: (not r.get("join_default", True), -r["confidence"]))
        for r in rels:
            if r["source_table"] in urns and r["target_table"] in urns and r["source_table"] != r["target_table"]:
                role = "default join" if r.get("join_default", True) else "another role"
                out.append({"kind": "join", "name": f"ontology:{r['id']}", "version": int(o.get("version") or 0),
                            "doc": {"source": r["source_table"], "destination": r["target_table"],
                                    "source_field": r["source_column"], "destination_field": r["target_column"],
                                    "datasets": [r["source_table"], r["target_table"]],
                                    "description": f"from the ontology ({role}; "
                                                   + ", ".join(p["source"] for p in r.get("provenance") or []) + ")"}})
        return out

    def grounding(self, urns: set[str], definitions: list[dict]) -> dict:
        """What of the ontology an answer was grounded in: the terms of its tables and the compiled relationships
        given to the model (the ``ontology:`` join definitions). Empty when the compiler is down."""
        try:
            o = self.published()
        except OntologyError:
            return {"terms": [], "relationships": []}
        if o is None:
            return {"terms": [], "relationships": []}
        used = {d["name"].split(":", 1)[1] for d in definitions if str(d.get("name", "")).startswith("ontology:")}
        terms = [{"id": t["id"], "name": t["name"], "table": t["table_id"]}
                 for t in o.get("terms") or [] if t["table_id"] in urns]
        rels = [{"id": r["id"], "label": f"{table_parts(r['source_table'])[3]}.{r['source_column']} → "
                                         f"{table_parts(r['target_table'])[3]}.{r['target_column']}"}
                for r in o.get("relationships") or [] if r["id"] in used]
        return {"terms": terms, "relationships": rels}

    def check(self, sql: str, dialect: Optional[str], can_view, definitions: Optional[list[dict]] = None) -> dict:
        """Ask-your-data's check of generated SQL: findings, or a warning when the compiler cannot answer (fail
        open: the governed run goes ahead either way)."""
        try:
            v = self.validate_sql(sql, can_view, dialect, definitions, timeout=10)
            return {"valid": v.get("valid", True), "findings": v.get("findings") or [], "joins": v.get("joins") or []}
        except OntologyError as e:
            return {"valid": None, "findings": [], "warning": f"the SQL was not checked against the ontology: {e}"}

    # ---- schedule --------------------------------------------------------------------------------
    def run_once(self, actor: str = "urn:li:corpuser:cairn-ontology-scheduler") -> dict:
        out = self.compile(actor, "incremental", sync=True, wait=True)
        run = out["run"]
        status = {"run_id": run["id"], "state": run["state"], "stats": run.get("stats"),
                  "imported": out.get("imported")}
        self._set_cursor("ontology_last_run", status)
        return status

    def run_forever(self, stop: Optional[threading.Event] = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                with advisory_lock(self.engine, LOCK_KEY) as leader:      # one scheduler at a time
                    if leader:
                        logger.info("ontology cycle: %s", self.run_once())
                metrics.worker_cycle("ontology", True)
            except Exception:  # noqa: BLE001 - the next cycle tries again
                metrics.worker_cycle("ontology", False)
                logger.exception("ontology cycle failed")
            stop.wait(self.interval)

    def status(self) -> dict:
        ready = self.client.ready()
        out: dict = {"configured": True, "ready": ready, "last_import": self._cursor("ontology_imported"),
                     "last_run": self._cursor("ontology_last_run")}
        if ready:
            try:
                runs = self.client.runs()
                out.update(published=runs.get("published"), active=runs.get("active"))
            except OntologyError as e:
                out["error"] = str(e)
        return out
