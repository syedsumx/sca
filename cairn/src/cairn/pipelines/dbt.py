"""dbt runs into Cairn: ``POST /pipelines/dbt/runs`` takes a run's artifacts (``manifest.json`` and
``run_results.json``) and records, for every model, seed and snapshot the run built:

* **docs**: its description and its columns' descriptions, where the dataset has none yet (a table or
  column comment from Trino, or a description written in Cairn, is never overwritten), and ``dbt.*``
  properties (unique id, materialization, package, path, last run status);
* **tags** from dbt (``tags:``) and **owners** from ``meta.owner`` (added, never removed);
* **lineage** between models and sources, from the manifest's ``depends_on`` (the upstreams dbt knows,
  marked as dbt's, so a removed ``ref`` disappears on the next run; lineage from elsewhere is kept);
* **tests**, through the external-quality path: each data test and unit test becomes a check on the
  model it tests, and ``source freshness`` (``sources.json``) a check on the source;
* **governance**: a model's contract (enforced or not), access (public, protected, private), group
  (and the group's owner) and version, as ``dbt.*`` properties; a ``deprecation_date`` deprecates the
  dataset in Cairn (and lifting it lifts dbt's deprecation);
* **exposures** become dashboards (or applications, notebooks, ...) in Cairn, downstream of the models
  they use, owned by their owner;
* **semantic models and metrics** (MetricFlow) go to the Context inbox as metric definitions to approve;
* the **run** itself: its command, environment, status, timing and counts (``GET /pipelines/dbt/runs``),
  and, for a successful run, its manifest as the environment's **state**
  (``GET /pipelines/dbt/state``), which ``cairn dbt`` defers to (``CAIRN_DBT_DEFER_TO``) for Slim CI;
* **docs**: ``cairn dbt docs generate`` publishes the project's dbt docs site, served at
  ``/pipelines/dbt/docs/<project>/`` with only the models each viewer may see.

Datasets are named the way Trino's access control, lineage and the catalog crawler name them
(``TrinoAccess.urn``), so dbt's metadata lands on the same datasets people query.
"""

import gzip
import json
import logging
import time
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Integer, LargeBinary, String, Table, Text, select

from cairn.common.db import metadata
from cairn.common.proposals import make_mcp

logger = logging.getLogger(__name__)
BUILT =("model", "seed", "snapshot")
SUCCESS = ("success", "pass")
RUN_COMMANDS = ("run", "build", "seed", "snapshot")

dbt_runs = Table(
    "cairn_dbt_runs", metadata,
    Column("invocation", String(64), primary_key=True),
    Column("project", String(256), nullable=False, index=True),
    Column("environment", String(64), nullable=False),
    Column("command", String(64), nullable=False),
    Column("status", String(16), nullable=False),            # success | error
    Column("actor", String(256), nullable=False),
    Column("started_ms", BigInteger, nullable=True),
    Column("finished_ms", BigInteger, nullable=False),
    Column("elapsed_s", Integer, nullable=True),
    Column("nodes", Integer, nullable=False),
    Column("errors", Integer, nullable=False),
    Column("tests_failed", Integer, nullable=False),
    Column("detail", Text, nullable=False),                  # JSON: per node status and timing
)
dbt_state = Table(
    "cairn_dbt_state", metadata,
    Column("project", String(256), primary_key=True),
    Column("environment", String(64), primary_key=True),
    Column("invocation", String(64), nullable=False),
    Column("saved_ms", BigInteger, nullable=False),
    Column("manifest_gz", LargeBinary, nullable=False),
)
dbt_docs = Table(
    "cairn_dbt_docs", metadata,
    Column("project", String(256), primary_key=True),
    Column("published_ms", BigInteger, nullable=False),
    Column("actor", String(256), nullable=False),
    Column("index_gz", LargeBinary, nullable=False),
    Column("manifest_gz", LargeBinary, nullable=False),
    Column("catalog_gz", LargeBinary, nullable=False),
)


def _gz(doc: Any) -> bytes:
    return gzip.compress((doc if isinstance(doc, str) else json.dumps(doc)).encode(), 6)


def _ungz(b: bytes, as_json: bool = True) -> Any:
    raw = gzip.decompress(b).decode()
    return json.loads(raw) if as_json else raw
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
        self.changes = None  # DatasetChanges: the datasets each run built
        self.engine = None   # runs, state and docs are kept when set
        self.semantic = None  # SemanticService: metrics to the Context inbox
        # Called with (invocation, project, ok, started_ms, finished_ms, results, built, actor) for each run kept
        # (pipeline observability, L145).
        self.observers: list = []

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
        built: list[str] = []
        for uid, node in nodes.items():
            kind = uid.split(".")[0]
            if kind not in BUILT:
                continue
            if results and uid not in results:
                continue  # not part of this run (dbt run -s ...)
            urn = self._urn(adapter, node)
            models += 1
            status = (results.get(uid) or {}).get("status")
            if status == "success":
                built.append(urn)
            mcps += self._docs(urn, node, uid, project, status, manifest)
            mcps += self._tags_and_owners(urn, node)
            mcps += self._deprecation(urn, node)
            ups = [self._urn(adapter, nodes[d]) for d in (node.get("depends_on") or {}).get("nodes") or []
                   if d in nodes and d.split(".")[0] in UPSTREAM]
            mcps += self._lineage(urn, ups)
            edges += len(ups)
        exposures = self._exposures(manifest, adapter, project, mcps)
        if mcps:
            self.graph.emit(mcps)
        if self.changes is not None and built:
            self.changes.record(built, "dbt", f"dbt:{project}", meta.get("invocation_id"))
        tests = None
        if run_results and any(str(k).startswith(("test.", "unit_test.")) for k in results):
            tests = self.external.ingest("dbt", {"run_results": run_results, "manifest": manifest}, adapter, None, actor,
                                         urn_of=self.urn_of if adapter == "trino" else None)
        if body.get("sources"):  # dbt source freshness
            fresh = self.external.ingest("dbt", {"sources": body["sources"], "manifest": manifest}, adapter, None, actor,
                                         urn_of=self.urn_of if adapter == "trino" else None)
            tests = {**(tests or {}), "freshness": fresh}
        metrics = self._metrics(manifest, project, actor)
        summary = {"project": project, "adapter": adapter, "models": models, "lineage_edges": edges,
                   "exposures": exposures, "metrics": metrics, "tests": tests, "invocation_id": meta.get("invocation_id")}
        kept = self._remember(body, manifest, run_results, project, actor)
        if kept:
            for fn in list(self.observers):
                try:
                    fn(kept["invocation"], project, kept["ok"], body.get("started_ms"), kept["finished_ms"],
                       (run_results or {}).get("results") or [], built, actor)
                except Exception:  # noqa: BLE001 - observing a run never rejects it
                    logger.exception("a dbt run observer failed")
        if self.audit is not None:
            self.audit.record("pipelines", "dbt.run_recorded", actor, None,
                              {k: v for k, v in summary.items() if k != "tests"} |
                              {"tests": (tests or {}).get("checks") if isinstance(tests, dict) else None})
        return summary

    def _docs(self, urn: str, node: dict, uid: str, project: str, status: Optional[str],
              manifest: Optional[dict] = None) -> list[dict]:
        out = []
        props = dict(self.graph.get_aspect(urn, "datasetProperties") or {})
        custom = {k: v for k, v in (props.get("customProperties") or {}).items()
                  if not k.startswith(("dbt.contract", "dbt.access", "dbt.group", "dbt.version", "dbt.latest_version"))}
        contract = (node.get("contract") or {}).get("enforced") if node.get("resource_type") == "model" else None
        group = node.get("group") or ""
        g_owner = (((manifest or {}).get("groups") or {}).get(f"group.{node.get('package_name')}.{group}") or {}).get("owner") or {}
        custom.update({k: v for k, v in {
            "dbt.unique_id": uid, "dbt.project": project, "dbt.package": node.get("package_name") or "",
            "dbt.materialized": (node.get("config") or {}).get("materialized") or "",
            "dbt.path": node.get("original_file_path") or "", "dbt.last_status": status or "",
            # Governance dbt declares: the contract it enforces, who may ref it, its group and version.
            "dbt.contract": ("enforced" if contract else "not enforced") if contract is not None else "",
            "dbt.access": node.get("access") or "", "dbt.group": group,
            "dbt.group_owner": g_owner.get("email") or g_owner.get("name") or "",
            "dbt.version": str(node["version"]) if node.get("version") is not None else "",
            "dbt.latest_version": str(node["latest_version"]) if node.get("latest_version") is not None else "",
        }.items() if v})
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

    def _deprecation(self, urn: str, node: dict) -> list[dict]:
        """dbt's deprecation_date deprecates the dataset; lifting it lifts only dbt's deprecation."""
        date = node.get("deprecation_date")
        cur = self.graph.get_aspect(urn, "deprecation") or {}
        mine = str(cur.get("note") or "").startswith("dbt:")
        if date:
            from datetime import datetime

            try:
                when = int(datetime.fromisoformat(str(date).replace("Z", "+00:00")).timestamp() * 1000)
            except ValueError:
                when = None
            want = {"deprecated": True, "note": f"dbt: deprecated in the dbt project (deprecation_date {date})",
                    "actor": "urn:li:corpuser:cairn-dbt", **({"decommissionTime": when} if when else {})}
            return [] if cur == want else [make_mcp(urn, "deprecation", want)]
        if mine and cur.get("deprecated"):
            return [make_mcp(urn, "deprecation", {**cur, "deprecated": False, "note": "dbt: no longer deprecated"})]
        return []

    def _exposures(self, manifest: dict, adapter: str, project: str, mcps: list) -> int:
        """dbt exposures: dashboards (and other consumers) downstream of the models they depend on."""
        nodes = {**(manifest.get("nodes") or {}), **(manifest.get("sources") or {})}
        n = 0
        for uid, ex in (manifest.get("exposures") or {}).items():
            name = ex.get("name") or uid.split(".")[-1]
            urn = f"urn:li:dashboard:(dbt,{project}.{name})"
            inputs = sorted({self._urn(adapter, nodes[d]) for d in (ex.get("depends_on") or {}).get("nodes") or []
                             if d in nodes and d.split(".")[0] in UPSTREAM})
            now = int(time.time() * 1000)
            stamp = {"time": now, "actor": "urn:li:corpuser:cairn-dbt"}
            info = {"title": ex.get("label") or name, "description": ex.get("description") or "",
                    "charts": [], "datasets": inputs, "lastModified": {"created": stamp, "lastModified": stamp},
                    "customProperties": {k: str(v) for k, v in {
                        "dbt.unique_id": uid, "dbt.type": ex.get("type"), "dbt.maturity": ex.get("maturity"),
                        "dbt.project": project}.items() if v}}
            if ex.get("url"):
                info["externalUrl"] = ex["url"]
            cur = self.graph.get_aspect(urn, "dashboardInfo") or {}
            if {k: v for k, v in cur.items() if k != "lastModified"} != {k: v for k, v in info.items() if k != "lastModified"}:
                mcps.append(make_mcp(urn, "dashboardInfo", info))
            owner = (ex.get("owner") or {}).get("email") or (ex.get("owner") or {}).get("name")
            if owner:
                o = owner if owner.startswith("urn:li:") else f"urn:li:corpuser:{owner}"
                own = self.graph.get_aspect(urn, "ownership") or {"owners": []}
                if o not in {x.get("owner") for x in own.get("owners") or []}:
                    mcps.append(make_mcp(urn, "ownership", {**own, "owners": list(own.get("owners") or []) +
                                                            [{"owner": o, "type": "BUSINESS_OWNER"}]}))
            n += 1
        return n

    def _metrics(self, manifest: dict, project: str, actor: str) -> Optional[dict]:
        """MetricFlow semantic models and metrics, to the Context inbox (approved there before use)."""
        sms, ms = manifest.get("semantic_models") or {}, manifest.get("metrics") or {}
        if self.semantic is None or not sms or not ms:
            return None
        import yaml

        models = []
        for sm in sms.values():
            rel = (sm.get("node_relation") or {})
            table = ".".join(p for p in (rel.get("database"), rel.get("schema_name"), rel.get("alias")) if p)
            models.append({**{k: sm.get(k) for k in ("name", "measures", "dimensions", "defaults", "description")},
                           "model": table or sm.get("model") or ""})
        doc = {"semantic_models": models,
               "metrics": [{k: m.get(k) for k in ("name", "label", "description", "type", "type_params", "filter")}
                           for m in ms.values()]}
        try:
            return self.semantic.import_definitions("metricflow", yaml.safe_dump(doc), f"dbt:{project}", actor)
        except Exception as e:  # noqa: BLE001 - the run is recorded; metrics that cannot be read are reported
            return {"error": str(e)[:300]}

    def _remember(self, body: dict, manifest: dict, run_results: Optional[dict], project: str,
                  actor: str) -> Optional[dict]:
        """The run, and for a successful run of the project the manifest as the environment's state."""
        if self.engine is None:
            return
        meta = manifest.get("metadata") or {}
        inv = str(meta.get("invocation_id") or body.get("invocation_id") or "")[:64]
        results = (run_results or {}).get("results") or []
        if not inv or not (results or body.get("sources")):
            return
        env = str(body.get("environment") or "prod")[:64]
        command = str(body.get("command") or (run_results or {}).get("args", {}).get("which") or "run")[:64]
        errors = sum(1 for r in results if r.get("status") in ("error", "runtime error"))
        failed = sum(1 for r in results if str(r.get("unique_id", "")).startswith(("test.", "unit_test."))
                     and r.get("status") in ("fail", "error"))
        ok = not errors and not failed
        now = int(time.time() * 1000)
        detail = [{"node": r.get("unique_id"), "status": r.get("status"),
                   "seconds": round(float(r.get("execution_time") or 0), 2)} for r in results[:2000]]
        row = {"project": project[:256], "environment": env, "command": command, "status": "success" if ok else "error",
               "actor": actor, "started_ms": body.get("started_ms"), "finished_ms": now,
               "elapsed_s": int((run_results or {}).get("elapsed_time") or 0), "nodes": len(results), "errors": errors,
               "tests_failed": failed, "detail": json.dumps(detail)}
        with self.engine.begin() as conn:
            if conn.execute(select(dbt_runs.c.invocation).where(dbt_runs.c.invocation == inv)).first():
                conn.execute(dbt_runs.update().where(dbt_runs.c.invocation == inv).values(**row))
            else:
                conn.execute(dbt_runs.insert().values(invocation=inv, **row))
            if ok and command in RUN_COMMANDS:
                q = (dbt_state.c.project == project[:256]) & (dbt_state.c.environment == env)
                values = {"invocation": inv, "saved_ms": now, "manifest_gz": _gz(manifest)}
                if conn.execute(select(dbt_state.c.project).where(q)).first():
                    conn.execute(dbt_state.update().where(q).values(**values))
                else:
                    conn.execute(dbt_state.insert().values(project=project[:256], environment=env, **values))
        return {"invocation": inv, "ok": ok, "finished_ms": now}

    def runs(self, project: Optional[str] = None, limit: int = 50) -> list[dict]:
        q = select(dbt_runs).order_by(dbt_runs.c.finished_ms.desc()).limit(max(1, min(limit, 500)))
        if project:
            q = q.where(dbt_runs.c.project == project)
        with self.engine.connect() as conn:
            return [{**{k: v for k, v in dict(r._mapping).items() if k != "detail"}, "detail": json.loads(r.detail)}
                    for r in conn.execute(q)]

    def state(self, project: str, environment: str) -> Optional[dict]:
        with self.engine.connect() as conn:
            row = conn.execute(select(dbt_state).where(dbt_state.c.project == project,
                                                       dbt_state.c.environment == environment)).first()
        return None if row is None else {"invocation": row.invocation, "saved_ms": row.saved_ms,
                                          "manifest": _ungz(row.manifest_gz)}

    def publish_docs(self, project: str, index_html: str, manifest: dict, catalog: dict, actor: str) -> dict:
        if not project or not index_html or not isinstance(manifest, dict) or not isinstance(catalog, dict):
            raise DbtError("send {project, index_html, manifest, catalog} from `dbt docs generate`")
        values = {"published_ms": int(time.time() * 1000), "actor": actor, "index_gz": _gz(index_html),
                  "manifest_gz": _gz(manifest), "catalog_gz": _gz(catalog)}
        with self.engine.begin() as conn:
            if conn.execute(select(dbt_docs.c.project).where(dbt_docs.c.project == project)).first():
                conn.execute(dbt_docs.update().where(dbt_docs.c.project == project).values(**values))
            else:
                conn.execute(dbt_docs.insert().values(project=project, **values))
            if self.audit is not None:
                self.audit.record("pipelines", "dbt.docs_published", actor, None, {"project": project}, conn=conn)
        return {"project": project, "url": f"/pipelines/dbt/docs/{project}/"}

    def docs(self, project: str, can_view: Callable[[str], bool]) -> Optional[dict]:
        """The project's docs, with only the nodes whose datasets ``can_view`` allows (and lineage among them)."""
        with self.engine.connect() as conn:
            row = conn.execute(select(dbt_docs).where(dbt_docs.c.project == project)).first()
        if row is None:
            return None
        manifest, catalog = _ungz(row.manifest_gz), _ungz(row.catalog_gz)
        adapter = (manifest.get("metadata") or {}).get("adapter_type") or "trino"
        keep = set()
        for kind in ("nodes", "sources"):
            for uid, n in (manifest.get(kind) or {}).items():
                head = uid.split(".")[0]
                if head in UPSTREAM and can_view(self._urn(adapter, n)):
                    keep.add(uid)
        for kind in ("nodes", "sources"):  # tests and other nodes about kept datasets only
            for uid, n in (manifest.get(kind) or {}).items():
                if uid.split(".")[0] not in UPSTREAM:
                    deps = (n.get("depends_on") or {}).get("nodes") or []
                    if deps and all(d in keep for d in deps):
                        keep.add(uid)
        for kind in ("nodes", "sources"):
            manifest[kind] = {u: n for u, n in (manifest.get(kind) or {}).items() if u in keep}
        for kind in ("exposures", "metrics", "semantic_models", "saved_queries", "unit_tests"):
            manifest[kind] = {u: n for u, n in (manifest.get(kind) or {}).items()
                              if all(d in keep for d in (n.get("depends_on") or {}).get("nodes") or [])}
        for m in ("parent_map", "child_map"):
            if isinstance(manifest.get(m), dict):
                manifest[m] = {u: [x for x in v if x in keep] for u, v in manifest[m].items() if u in keep}
        for kind in ("nodes", "sources"):
            catalog[kind] = {u: n for u, n in (catalog.get(kind) or {}).items() if u in keep}
        return {"index": _ungz(row.index_gz, as_json=False), "manifest": manifest, "catalog": catalog,
                "published_ms": row.published_ms}

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

    def need_store():
        if runs.engine is None:
            raise HTTPException(404, "dbt runs are not kept here")

    @router.get("/runs")
    def list_runs(project: Optional[str] = None, limit: int = 50, p=Depends(current_principal)):
        """dbt runs, newest first: command, environment, status, timing, counts and each node's result.
        For pipeline operators (MANAGE_INGESTION), and for each pipeline user its own runs."""
        need_store()
        try:
            authorize(p, "MANAGE_INGESTION", None)
            own = None
        except HTTPException:
            own = p.urn
        out = runs.runs(project, limit if own is None else 500)
        return [r for r in out if own is None or r["actor"] == own][:limit]

    @router.get("/state")
    def get_state(project: str, environment: str = "prod", p=Depends(current_principal)):
        """The manifest of the environment's last successful run, for ``--defer --state`` (Slim CI)."""
        need_store()
        authorize(p, "EDIT_LINEAGE", None)
        st = runs.state(project, environment)
        if st is None:
            raise HTTPException(404, f"no successful run of {project} in {environment} yet")
        return st

    @router.post("/docs")
    async def publish_docs(request: Request, p=Depends(current_principal)):
        """dbt's docs site from ``dbt docs generate``: {project, index_html, manifest, catalog}."""
        need_store()
        authorize(p, "EDIT_LINEAGE", None)
        if int(request.headers.get("content-length") or 0) > 96 << 20:
            raise HTTPException(413, "docs over 96 MB")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "the body is not JSON")
        try:
            return await run_in_threadpool(runs.publish_docs, str(body.get("project") or ""), body.get("index_html") or "",
                                           body.get("manifest"), body.get("catalog"), p.urn)
        except DbtError as e:
            raise HTTPException(422, str(e))

    def viewable(p):
        def can(urn: str) -> bool:
            try:
                authorize(p, "VIEW_ENTITY_PAGE", urn)
                return True
            except HTTPException:
                return False
        return can

    @router.get("/docs/{project}/{name}")
    def docs_file(project: str, name: str, p=Depends(current_principal)):
        """The project's dbt docs (index.html, manifest.json, catalog.json), with only what the viewer may see."""
        from fastapi.responses import HTMLResponse, JSONResponse

        need_store()
        if name not in ("", "index.html", "manifest.json", "catalog.json"):
            raise HTTPException(404, "not part of dbt's docs")
        d = runs.docs(project, viewable(p))
        if d is None:
            raise HTTPException(404, f"no docs published for {project}")
        if name == "manifest.json":
            return JSONResponse(d["manifest"], headers={"Cache-Control": "private, no-store"})
        if name == "catalog.json":
            return JSONResponse(d["catalog"], headers={"Cache-Control": "private, no-store"})
        return HTMLResponse(d["index"], headers={"Cache-Control": "private, no-store",
                                                 "Content-Security-Policy": "frame-ancestors 'self'"})

    @router.get("/docs/{project}/")
    def docs_index(project: str, p=Depends(current_principal)):
        return docs_file(project, "index.html", p)

    return router
