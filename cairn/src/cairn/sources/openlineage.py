"""OpenLineage in: lineage and run events from Airflow, Spark, dbt, Dagster, Flink and anything else
that speaks OpenLineage, pushed to ``POST /openlineage/api/v1/lineage`` (the HTTP transport's path).

Each event becomes catalog metadata, without polling:

* the **job** becomes a pipeline (``dataFlow``) and a task (``dataJob``), with the datasets it reads
  and writes and, from the ``columnLineage`` facet, column-level lineage (``dataJobInputOutput``).
  Inputs and outputs accumulate over a job's events; crawled lineage on the datasets is not touched;
* the **run** becomes a run of that task (``dataProcessInstance``) with its start and its result
  (``COMPLETE`` → success, ``FAIL`` → failure, ``ABORT`` → skipped), so run history and failure
  streaks show up next to the job;
* **datasets** unknown to the catalog are created, with their schema from the ``schema`` facet.

Dataset names follow OpenLineage's naming: the namespace's scheme names the platform
(``postgres://host:5432`` → postgres, ``snowflake://account`` → snowflake, ``s3://bucket`` → s3…).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

from cairn.common.proposals import make_mcp

SCHEMES = {
    "postgres": "postgres", "postgresql": "postgres", "mysql": "mysql", "mariadb": "mysql", "snowflake": "snowflake",
    "bigquery": "bigquery", "redshift": "redshift", "s3": "s3", "s3a": "s3", "s3n": "s3", "gs": "gcs", "gcs": "gcs",
    "abfss": "abs", "wasbs": "abs", "hdfs": "hdfs", "file": "file", "kafka": "kafka", "trino": "trino",
    "presto": "presto", "databricks": "databricks", "dbfs": "dbfs", "sqlserver": "mssql", "mssql": "mssql",
    "oracle": "oracle", "hive": "hive", "athena": "athena", "glue": "glue", "mongodb": "mongodb",
    "cassandra": "cassandra", "teradata": "teradata", "clickhouse": "clickhouse", "duckdb": "duckdb",
}
ORCHESTRATORS = {"airflow": "airflow", "spark": "spark", "dbt": "dbt", "dagster": "dagster", "flink": "flink",
                 "prefect": "prefect", "databricks": "databricks"}
LOWERCASE = {"snowflake", "bigquery"}                   # platforms whose catalog names are lower case
RESULTS = {"COMPLETE": "SUCCESS", "FAIL": "FAILURE", "ABORT": "SKIPPED"}
ACTOR = "urn:li:corpuser:cairn-openlineage"


class OpenLineageError(ValueError):
    pass


def _ms(ts: Optional[str]) -> int:
    if not ts:
        return int(datetime.now(timezone.utc).timestamp() * 1000)
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        raise OpenLineageError(f"eventTime {ts!r} is not an ISO 8601 time") from None


def dataset_urn(namespace: str, name: str, env: str = "PROD") -> str:
    """The catalog dataset for an OpenLineage dataset (namespace + name)."""
    scheme = (namespace.split("://", 1)[0] if "://" in namespace else namespace).lower()
    platform = SCHEMES.get(scheme, re.sub(r"[^a-z0-9_-]", "", scheme) or "openlineage")
    if platform in ("s3", "gcs", "hdfs", "abs", "file", "dbfs"):
        parts = urlsplit(namespace)
        host = parts.netloc.split("@", 1)[0] if platform == "abs" else parts.netloc
        path = name.lstrip("/")
        name = f"{host}/{path}" if host and platform != "file" else f"/{path}" if platform == "file" else path
    if platform in LOWERCASE:
        name = name.lower()
    if not name:
        raise OpenLineageError("a dataset needs a name")
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},{env})"


def job_urns(namespace: str, name: str, env: str = "PROD") -> tuple[str, str, str]:
    """``(orchestrator, flow urn, job urn)``. Airflow names tasks ``dag.task``: the DAG is the flow."""
    orchestrator = next((v for k, v in ORCHESTRATORS.items() if k in namespace.lower()), None)
    if orchestrator is None:
        orchestrator = re.sub(r"[^a-z0-9_-]", "", namespace.split("://", 1)[0].lower()) or "openlineage"
    if orchestrator == "airflow" and "." in name:
        flow, task = name.split(".", 1)
    else:
        flow, task = namespace.split("://", 1)[-1].strip("/") or orchestrator, name
    flow_urn = f"urn:li:dataFlow:({orchestrator},{flow},{env})"
    return orchestrator, flow_urn, f"urn:li:dataJob:({flow_urn},{task})"


def _field_urn(dataset: str, field: str) -> str:
    return f"urn:li:schemaField:({dataset},{field})"


def _schema(urn: str, facet: dict) -> dict:
    platform = urn.split("dataPlatform:", 1)[1].split(",", 1)[0]
    return {"schemaName": urn.split(",")[1], "platform": f"urn:li:dataPlatform:{platform}", "version": 0, "hash": "",
            "platformSchema": {"com.linkedin.schema.OtherSchema": {"rawSchema": ""}},
            "fields": [{"fieldPath": f["name"], "nativeDataType": str(f.get("type") or "unknown"),
                        **({"description": str(f["description"])} if f.get("description") else {}),
                        "type": {"type": {"com.linkedin.schema.NullType": {}}}} for f in facet.get("fields") or []
                       if f.get("name")]}


class OpenLineageReceiver:
    def __init__(self, graph, audit=None, env: str = "PROD"):
        self.graph = graph
        self.audit = audit
        self.env = env

    def _datasets(self, items: list[dict], mcps: list[dict]) -> list[str]:
        urns = []
        for d in items or []:
            if not isinstance(d, dict) or not d.get("namespace") or not d.get("name"):
                raise OpenLineageError("a dataset needs a namespace and a name")
            urn = dataset_urn(d["namespace"], d["name"], self.env)
            urns.append(urn)
            schema = (d.get("facets") or {}).get("schema")
            known = self.graph.get_aspect(urn, "schemaMetadata") or self.graph.get_aspect(urn, "datasetProperties")
            if not known:                                 # crawlers own what they catalogued
                mcps.append(make_mcp(urn, "status", {"removed": False}))
                if schema and schema.get("fields"):
                    mcps.append(make_mcp(urn, "schemaMetadata", _schema(urn, schema)))
        return urns

    def _column_lineage(self, outputs: list[dict]) -> list[dict]:
        out = []
        for d in outputs or []:
            facet = (d.get("facets") or {}).get("columnLineage") or {}
            down = dataset_urn(d["namespace"], d["name"], self.env)
            for column, spec in (facet.get("fields") or {}).items():
                ups = [_field_urn(dataset_urn(i["namespace"], i["name"], self.env), i["field"])
                       for i in (spec or {}).get("inputFields") or [] if i.get("namespace") and i.get("field")]
                if ups:
                    out.append({"upstreamType": "FIELD_SET", "upstreams": sorted(set(ups)), "downstreamType": "FIELD",
                                "downstreams": [_field_urn(down, column)],
                                "transformOperation": (spec.get("transformationType") or "TRANSFORMED")[:64]})
        return out

    def handle(self, event: dict, actor: str = ACTOR) -> dict:
        if not isinstance(event, dict):
            raise OpenLineageError("an event is a JSON object")
        mcps: list[dict] = []
        if "job" not in event:                          # a DatasetEvent: just the dataset
            if "dataset" not in event:
                raise OpenLineageError("not an OpenLineage RunEvent, JobEvent or DatasetEvent")
            self._datasets([event["dataset"]], mcps)
            self.graph.emit(mcps)
            return {"datasets": 1, "mcps": len(mcps)}
        job = event["job"] or {}
        if not job.get("namespace") or not job.get("name"):
            raise OpenLineageError("the job needs a namespace and a name")
        when = _ms(event.get("eventTime"))
        orchestrator, flow_urn, job_urn = job_urns(job["namespace"], job["name"], self.env)
        inputs = self._datasets(event.get("inputs") or [], mcps)
        outputs = self._datasets(event.get("outputs") or [], mcps)
        before = self.graph.get_aspect(job_urn, "dataJobInputOutput") or {}
        fine = {(tuple(f["upstreams"]), tuple(f["downstreams"])): f for f in before.get("fineGrainedLineages") or []}
        for f in self._column_lineage(event.get("outputs") or []):
            fine[(tuple(f["upstreams"]), tuple(f["downstreams"]))] = f
        io = {"inputDatasets": sorted(set(before.get("inputDatasets") or []) | set(inputs)),
              "outputDatasets": sorted(set(before.get("outputDatasets") or []) | set(outputs)),
              "fineGrainedLineages": list(fine.values())}
        if io != {k: before.get(k) or [] for k in io}:
            mcps.append(make_mcp(job_urn, "dataJobInputOutput", io))
        doc = ((job.get("facets") or {}).get("documentation") or {}).get("description")
        sql = ((job.get("facets") or {}).get("sql") or {}).get("query")
        if self.graph.get_aspect(flow_urn, "dataFlowInfo") is None:
            mcps.append(make_mcp(flow_urn, "dataFlowInfo", {"name": flow_urn.split(",")[1],
                                                            "customProperties": {"openlineage_namespace": job["namespace"]}}))
        info = {"name": job_urn.rsplit(",", 1)[1].rstrip(")"), "flowUrn": flow_urn,
                "type": {"com.linkedin.datajob.azkaban.AzkabanJobType": "COMMAND"},
                "customProperties": {"openlineage_namespace": job["namespace"], "openlineage_name": job["name"],
                                     "orchestrator": orchestrator}}
        if doc:
            info["description"] = str(doc)[:5000]
        if sql:
            info["customProperties"]["sql"] = str(sql)[:10_000]
        if (self.graph.get_aspect(job_urn, "dataJobInfo") or {}) != info:
            mcps.append(make_mcp(job_urn, "dataJobInfo", info))

        run = event.get("run") or {}
        state = str(event.get("eventType") or "").upper()
        run_urn = None
        if run.get("runId"):
            run_urn = "urn:li:dataProcessInstance:" + hashlib.sha256(f"{job_urn}|{run['runId']}".encode()).hexdigest()[:32]
            if state in ("START", "") or self.graph.get_aspect(run_urn, "dataProcessInstanceProperties") is None:
                parent = ((run.get("facets") or {}).get("parent") or {}).get("run", {}).get("runId")
                mcps.append(make_mcp(run_urn, "dataProcessInstanceProperties", {
                    "name": str(run["runId"])[:200], "type": "BATCH_SCHEDULED",
                    "created": {"time": when, "actor": actor},
                    "customProperties": {"run_id": str(run["runId"]), **({"parent_run_id": str(parent)} if parent else {})}}))
                mcps.append(make_mcp(run_urn, "dataProcessInstanceRelationships",
                                     {"parentTemplate": job_urn, "upstreamInstances": []}))
            if inputs:
                mcps.append(make_mcp(run_urn, "dataProcessInstanceInput", {"inputs": inputs}))
            if outputs:
                mcps.append(make_mcp(run_urn, "dataProcessInstanceOutput", {"outputs": outputs}))
            if state == "START":
                mcps.append(make_mcp(run_urn, "dataProcessInstanceRunEvent",
                                     {"timestampMillis": when, "status": "STARTED", "attempt": 1}))
            elif state in RESULTS:
                mcps.append(make_mcp(run_urn, "dataProcessInstanceRunEvent", {
                    "timestampMillis": when, "status": "COMPLETE", "attempt": 1,
                    "result": {"type": RESULTS[state], "nativeResultType": "openlineage"}}))
        self.graph.emit(mcps)
        return {"job": job_urn, "run": run_urn, "inputs": len(inputs), "outputs": len(outputs),
                "column_lineage": len(io["fineGrainedLineages"]), "mcps": len(mcps)}

    def handle_many(self, body: Any, actor: str = ACTOR) -> dict:
        events = body if isinstance(body, list) else [body]
        if not events or len(events) > 500:
            raise OpenLineageError("send between 1 and 500 events")
        results = [self.handle(e, actor) for e in events]
        if self.audit is not None:
            self.audit.record("lineage", "openlineage.received", actor, None, {
                "events": len(results), "jobs": sorted({r["job"] for r in results if r.get("job")})[:20]})
        return {"accepted": len(results), "results": results}
