"""dbt runs into Cairn: ``POST /pipelines/dbt/runs`` takes a run's artifacts (``manifest.json`` and
``run_results.json``) and records, for every model, seed and snapshot the run built:

* **docs**: its description and its columns' descriptions, where the dataset has none yet (a table or
  column comment from Trino, or a description written in Cairn, is never overwritten), and ``dbt.*``
  properties (unique id, materialization, package, path, last run status);
* **tags** from dbt (``tags:``) and **owners** from ``meta.owner`` (added, never removed);
* **lineage** between models and sources, from the manifest's ``depends_on`` (the upstreams dbt knows,
  marked as dbt's, so a removed ``ref`` disappears on the next run; lineage from elsewhere is kept);
* **tests**, through the external-quality path: each test becomes a check on the model it tests.

Datasets are named the way Trino's access control, lineage and the catalog crawler name them
(``TrinoAccess.urn``), so dbt's metadata lands on the same datasets people query.
"""

from typing import Any, Callable, Optional

from cairn.common.proposals import make_mcp

BUILT = ("model", "seed", "snapshot")
UPSTREAM = ("model", "seed", "snapshot", "source")
MARK = {"source": "dbt"}


class DbtError(Exception):
    pass


def _owners(meta: dict) -> list[str]:
    raw = (meta or {}).get("owner") or (meta or {}).get("owners") or []
    raw = [raw] if isinstance(raw, str) else list(raw)
    out = []
    for o in raw:
        o = str(o).strip()
        if not o:
            continue
        if o.startswith("urn:li:"):
            out.append(o)
        else:
            out.append(f"urn:li:corpuser:{o}")  # a Cairn user name (often the e-mail address)
    return out


class DbtRuns:
    def __init__(self, graph, external_quality, audit, urn_of: Optional[Callable[[str, str, str], str]] = None):
        self.graph, self.external, self.audit, self.urn_of = graph, external_quality, audit, urn_of

    def _urn(self, adapter: str, node: dict) -> str:
        table = node.get("alias") or node.get("identifier") or node.get("name")
        db, schema = node.get("database") or "", node.get("schema") or ""
        if self.urn_of is not None and adapter == "trino":
            return self.urn_of(db, schema, table)
        name = ".".join(p for p in (db, schema, table) if p)
        if adapter in ("snowflake", "bigquery"):
            name = name.lower()
        return f"urn:li:dataset:(urn:li:dataPlatform:{adapter},{name},PROD)"

    def ingest(self, body: dict, actor: str) -> dict:
        manifest, run_results = body.get("manifest"), body.get("run_results")
        if not isinstance(manifest, dict) or not isinstance(manifest.get("nodes"), dict):
            raise DbtError("send {\"manifest\": <manifest.json>, \"run_results\": <run_results.json>}")
        meta = manifest.get("metadata") or {}
        adapter = meta.get("adapter_type") or "trino"
        project = meta.get("project_name") or ""
        nodes = {**(manifest.get("nodes") or {}), **(manifest.get("sources") or {})}
        results = {r.get("unique_id"): r for r in ((run_results or {}).get("results") or [])}
        mcps: list[dict] = []
        models = 0
        edges = 0
        for uid, node in nodes.items():
            kind = uid.split(".")[0]
            if kind not in BUILT:
                continue
            if results and uid not in results:
                continue  # not part of this run (dbt run -s ...)
            urn = self._urn(adapter, node)
            models += 1
            status = (results.get(uid) or {}).get("status")
            mcps += self._docs(urn, node, uid, project, status)
            mcps += self._tags_and_owners(urn, node)
            ups = [self._urn(adapter, nodes[d]) for d in (node.get("depends_on") or {}).get("nodes") or []
                   if d in nodes and d.split(".")[0] in UPSTREAM]
            mcps += self._lineage(urn, ups)
            edges += len(ups)
        if mcps:
            self.graph.emit(mcps)
        tests = None
        if run_results and any(str(k).startswith("test.") for k in results):
            tests = self.external.ingest("dbt", {"run_results": run_results, "manifest": manifest}, adapter, None, actor,
                                         urn_of=self.urn_of if adapter == "trino" else None)
        summary = {"project": project, "adapter": adapter, "models": models, "lineage_edges": edges,
                   "tests": tests, "invocation_id": meta.get("invocation_id")}
        if self.audit is not None:
            self.audit.record("pipelines", "dbt.run_recorded", actor, None,
                              {k: v for k, v in summary.items() if k != "tests"} |
                              {"tests": (tests or {}).get("checks") if isinstance(tests, dict) else None})
        return summary

    def _docs(self, urn: str, node: dict, uid: str, project: str, status: Optional[str]) -> list[dict]:
        out = []
        props = dict(self.graph.get_aspect(urn, "datasetProperties") or {})
        custom = dict(props.get("customProperties") or {})
        custom.update({k: v for k, v in {
            "dbt.unique_id": uid, "dbt.project": project, "dbt.package": node.get("package_name") or "",
            "dbt.materialized": (node.get("config") or {}).get("materialized") or "",
            "dbt.path": node.get("original_file_path") or "", "dbt.last_status": status or ""}.items() if v})
        props["customProperties"] = custom
        props.setdefault("name", node.get("alias") or node.get("name") or "")
        if not props.get("description") and (node.get("description") or "").strip():
            props["description"] = node["description"].strip()
        out.append(make_mcp(urn, "datasetProperties", props))
        schema = self.graph.get_aspect(urn, "schemaMetadata")
        cols = {k.lower(): (v or {}).get("description") for k, v in (node.get("columns") or {}).items()}
        if schema and any(cols.values()):
            changed = False
            for f in schema.get("fields") or []:
                doc = cols.get(str(f.get("fieldPath", "")).lower())
                if doc and not f.get("description"):
                    f["description"] = doc.strip()
                    changed = True
            if changed:
                out.append(make_mcp(urn, "schemaMetadata", schema))
        return out

    def _tags_and_owners(self, urn: str, node: dict) -> list[dict]:
        out = []
        tags = [t for t in (node.get("tags") or []) if isinstance(t, str) and t.strip()]
        if tags:
            cur = self.graph.get_aspect(urn, "globalTags") or {"tags": []}
            have = {t.get("tag") for t in cur.get("tags") or []}
            new = [{"tag": f"urn:li:tag:{t.strip()}"} for t in tags if f"urn:li:tag:{t.strip()}" not in have]
            if new:
                out.append(make_mcp(urn, "globalTags", {"tags": list(cur.get("tags") or []) + new}))
        owners = _owners((node.get("config") or {}).get("meta") or node.get("meta") or {})
        if owners:
            cur = self.graph.get_aspect(urn, "ownership") or {"owners": []}
            have = {o.get("owner") for o in cur.get("owners") or []}
            new = [{"owner": o, "type": "TECHNICAL_OWNER"} for o in owners if o not in have]
            if new:
                out.append(make_mcp(urn, "ownership", {**cur, "owners": list(cur.get("owners") or []) + new}))
        return out

    def _lineage(self, urn: str, ups: list[str]) -> list[dict]:
        cur = self.graph.get_aspect(urn, "upstreamLineage") or {}
        others = [u for u in cur.get("upstreams") or [] if (u.get("properties") or {}).get("source") != "dbt"]
        mine = [{"dataset": u, "type": "TRANSFORMED", "properties": dict(MARK),
                 "auditStamp": {"time": 0, "actor": "urn:li:corpuser:cairn-dbt"}} for u in sorted(set(ups)) if u != urn]
        want = {**cur, "upstreams": others + mine}
        if want == cur or (not mine and not cur):
            return []
        return [make_mcp(urn, "upstreamLineage", want)]


def build_dbt_router(runs: DbtRuns, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Depends, HTTPException, Request
    from starlette.concurrency import run_in_threadpool

    from cairn.quality.external import ExternalError

    router = APIRouter(prefix="/pipelines/dbt", tags=["pipelines"])

    @router.post("/runs")
    async def record_run(request: Request, p=Depends(current_principal)):
        """A dbt run's artifacts: docs, tags, owners, lineage and test results land on the datasets it built."""
        authorize(p, "EDIT_LINEAGE", None)
        authorize(p, "EDIT_ENTITY_ASSERTIONS", None)
        if int(request.headers.get("content-length") or 0) > 64 << 20:
            raise HTTPException(413, "artifacts over 64 MB: send run_results with a trimmed manifest")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "the body is not JSON")
        if not isinstance(body, dict):
            raise HTTPException(422, "the body is a JSON object")
        try:
            return await run_in_threadpool(runs.ingest, body, p.urn)
        except (DbtError, ExternalError) as e:
            raise HTTPException(422, str(e))

    return router
