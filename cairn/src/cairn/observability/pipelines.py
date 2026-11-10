"""Pipeline observability: every pipeline run Cairn hears of, its tasks, health, SLAs and the data products it builds.

**What is a pipeline.** An Airflow DAG (``airflow:<dag>``; ``airflow:<dag>@<deployment>`` for an Airflow other than
the default one, such as Cairn's managed deployments), a Cairn flow (``flow:<name>``), a dbt project
(``dbt:<project>``), a Cairn job (``job:<name>``, Spark jobs included) and anything else that reports through
OpenLineage (``spark:<app>``, ``dagster:<job>``, ``openlineage:<namespace>.<job>``). Runs arrive:

* from **OpenLineage** (``POST /openlineage/api/v1/lineage``: Airflow's OpenLineage provider, Spark's listener,
  dbt-ol, Dagster...): an event with a ``parent`` facet is a task of its parent's run; Airflow's DAG run id is taken
  from its ``airflow``/``airflowDagRun`` facets, so both arrival paths name a run the same way;
* from **Airflow's REST API** (the default Airflow ``CAIRN_AIRFLOW_URL`` and every source in ``airflow_sources``),
  read every ``CAIRN_OBS_PIPELINES_SYNC_SECONDS`` by the notifier: DAG runs changed since the last read and their
  task instances (state, tries, start, end);
* from Cairn itself: **flow** runs and their steps, **dbt** invocations (``/v1/dbt/runs``), **job** runs.

**Each run** keeps its state, start, end, duration, tasks (for a Gantt), inputs and outputs, error, and the trace
id Cairn gave it. When it ends Cairn emits it as OpenTelemetry (:meth:`PipelineObservability.emit`): one trace per
run (service ``cairn-pipelines``; the trace id *is* the OpenLineage run id when that is a UUID, else derived from
the run) with a span per task, the metrics ``cairn_pipeline_runs_total``, ``cairn_pipeline_run_duration_seconds`` and
``cairn_pipeline_sla_misses_total``, and a log record in the run's trace; all through Cairn's redaction and the
Collector's, to the bundled stack and every configured OTLP destination.

**Health.** Per pipeline: run history, success rate, duration percentiles and trend, anomalies (a run more than
three standard deviations and half again slower than its last twenty successful runs), whether it is failing,
late or anomalous.

**Data products** (kind ``observability_data_products``): a named set of tables and the pipelines that build them.
**SLAs** (kind ``observability_pipeline_slas``) on a pipeline or a data product: alert on failure, a daily
deadline (a successful run by HH:MM in a time zone), a maximum duration, the freshness of its tables, anomalies.
They are evaluated with the alert rules and SLOs (:meth:`ObservabilityService.evaluate`) and fire through the same
alerts: notification routes, silences, incidents (labels ``pipeline``, ``condition``, ``sla``).

**Impact and quality.** For a run: the tables it wrote, their downstream tables (Cairn's lineage, up to three
hops), the dashboards, saved queries and workbooks that read any of them, the data products that hold them and the
pipelines that read them; and the quality checks on the tables it wrote evaluated during or after it.

**Diagnosis** (for the SRE agent, L142): a failed run's failed tasks, the task's log (Airflow's API, a flow step's
log, else the run's trace in Loki), and ranked likely causes: an upstream failure, a schema change, a permission
denied (Cairn's audit log), resource limits, a deploy or a policy change around it; a rerun or a fix proposed.
"""

from __future__ import annotations

import contextvars
import json
import logging
import math
import os
import random
import re
import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Optional
from urllib.parse import quote

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import BigInteger, Boolean, Column, Index, String, Table, Text, UniqueConstraint, and_, insert, or_, \
    select, update

from cairn.common.db import JSONText, metadata
from cairn.observability import redact
from cairn.observability.exporters import Exporter
from cairn.observability.service import ID, MODELS, ObservabilityError, _duration, _fingerprint, _iso, seconds

logger = logging.getLogger(__name__)
ACTOR = "urn:li:corpuser:cairn-observability"
KEY = re.compile(r"^[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9_.@-]{1,250}$")
TERMINAL = ("success", "failed", "skipped")
STATES = ("queued", "running", *TERMINAL)
# Airflow's run and task instance states as Cairn's.
AIRFLOW_STATE = {"success": "success", "failed": "failed", "upstream_failed": "skipped", "skipped": "skipped",
                 "removed": "skipped", "running": "running", "queued": "queued", "scheduled": "queued",
                 "up_for_retry": "running", "up_for_reschedule": "running", "deferred": "running",
                 "restarting": "running", None: "queued", "none": "queued"}
FLOW_STATE = {"succeeded": "success", "failed": "failed", "cancelled": "failed", "skipped": "skipped",
              "queued": "queued", "running": "running", "waiting": "running", "ok": "success", "retrying": "running",
              "pending": "queued"}
OL_STATE = {"START": "running", "RUNNING": "running", "COMPLETE": "success", "FAIL": "failed", "ABORT": "failed"}
CAUSES = {
    "schema_change": re.compile(r"(?i)(column\s+\S+\s+(cannot be resolved|does not exist|not found)|no such column|"
                                r"undefinedcolumn|schema mismatch|table\s+\S+\s+does not exist|unknown column|"
                                r"missing column|type mismatch|cannot cast)"),
    "permission": re.compile(r"(?i)(access denied|permission denied|not authori[sz]ed|forbidden|\b403\b|"
                             r"insufficient privileges|accessdenied|denied on)"),
    "resource": re.compile(r"(?i)(oomkilled|out of memory|memoryerror|exit code 137|\bkilled\b|exceeded.{0,40}memory|"
                           r"no space left|diskpressure|timed out|timeout|resource exhausted|too many connections|"
                           r"exceeded.{0,30}limit)"),
}

pipeline_runs = Table(
    "cairn_obs_pipeline_runs", metadata,
    Column("id", String(40), primary_key=True),
    Column("pipeline", String(300), nullable=False),
    Column("source", String(32), nullable=False),               # airflow | flow | dbt | job | spark | <orchestrator>
    Column("instance", String(64), nullable=False),             # the Airflow (default or a deployment); "" else
    Column("run_id", String(250), nullable=False),
    Column("ol_run_id", String(64), nullable=True, index=True),
    Column("state", String(16), nullable=False, index=True),   # queued | running | success | failed | skipped
    Column("start_ms", BigInteger, nullable=True),
    Column("end_ms", BigInteger, nullable=True, index=True),
    Column("duration_ms", BigInteger, nullable=True),
    Column("trace_id", String(32), nullable=True),
    Column("error", Text, nullable=True),
    Column("inputs", JSONText, nullable=False),
    Column("outputs", JSONText, nullable=False),
    Column("tasks", JSONText, nullable=False),
    Column("detail", JSONText, nullable=True),
    Column("emitted", Boolean, nullable=False),
    Column("updated_ms", BigInteger, nullable=False, index=True),
    UniqueConstraint("pipeline", "run_id", name="uq_cairn_obs_pipeline_runs_run"),
    Index("ix_cairn_obs_pipeline_runs_pipeline_start", "pipeline", "start_ms"),
)


def _now() -> int:
    return int(time.time() * 1000)


def _ms(v: Any) -> Optional[int]:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _clean(text: Any, n: int = 4000) -> Optional[str]:
    if text in (None, ""):
        return None
    return redact.scrub_error(str(text))[:n]


def pipeline_key(source: str, name: str, instance: str = "") -> str:
    src = re.sub(r"[^a-z0-9_-]", "", str(source).lower())[:32] or "openlineage"
    nm = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name).replace("/", "."))[:200].strip("_") or "unnamed"
    inst = re.sub(r"[^A-Za-z0-9_.-]", "_", instance)[:40] if instance and instance != "default" else ""
    return f"{src}:{nm}" + (f"@{inst}" if inst else "")


def check_key(key: str) -> str:
    if not isinstance(key, str) or not KEY.match(key):
        raise ObservabilityError("a pipeline: <source>:<name>, e.g. airflow:daily_sales or flow:nightly", 422)
    return key


def run_trace_id(pipeline: str, run_id: str, ol_run_id: Optional[str]) -> str:
    """A run's trace id: its OpenLineage run id when that is a UUID (so a backend finds the run's trace by it),
    else derived from the pipeline and run."""
    if ol_run_id:
        try:
            return uuid.UUID(str(ol_run_id)).hex
        except ValueError:
            pass
    return uuid.uuid5(uuid.NAMESPACE_URL, f"cairn-pipeline:{pipeline}|{run_id}").hex


# ---- definitions -------------------------------------------------------------------------------------------------
class DataProduct(BaseModel):
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    owners: list[str] = Field(default_factory=list, max_length=20, description="people or groups (URNs)")
    tables: list[str] = Field(default_factory=list, max_length=200,
                              description="its tables: dataset URNs or catalog.schema.table")
    pipelines: list[str] = Field(default_factory=list, max_length=100,
                                 description="the pipelines that build it, e.g. airflow:daily_sales, flow:nightly")
    catalog_urn: str = Field("", max_length=400, pattern=r"^(urn:li:dataProduct:.+)?$",
                             description="a catalog data product whose assets are its tables too")
    labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _shape(self):
        for p in self.pipelines:
            check_key(p)
        for t in self.tables:
            if not (t.startswith("urn:li:dataset:") or re.match(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$", t)):
                raise ValueError(f"tables: {t!r} is neither a dataset URN nor catalog.schema.table")
        if not (self.tables or self.pipelines or self.catalog_urn):
            raise ValueError("a data product names its tables, its pipelines or a catalog data product")
        return self


class PipelineSla(BaseModel):
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    pipeline: str = Field("", max_length=300, description="the pipeline it is about (or data_product)")
    data_product: str = Field("", max_length=64, description="the data product it is about (or pipeline)")
    on_failure: bool = Field(True, description="alert when a run fails (until one succeeds)")
    deadline: str = Field("", pattern=r"^([01]\d|2[0-3]):[0-5]\d$|^$",
                          description="HH:MM: a run must have succeeded by then each day (in timezone)")
    timezone: str = Field("UTC", max_length=64)
    days: list[int] = Field(default_factory=lambda: list(range(7)), description="0 Monday ... 6 Sunday")
    max_duration: str = Field("", description="a run must take at most this long, e.g. 45m")
    freshness: str = Field("", description="its tables must have been updated within this, e.g. 6h")
    anomalies: bool = Field(False, description="alert when a run is anomalously slow")
    severity: Literal["critical", "warning", "info"] = "warning"
    routes: list[str] = Field(default_factory=list, description="notification routes that receive its alerts")
    recipients: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    runbook_url: str = Field("", max_length=1000)
    open_incident: bool = True
    enabled: bool = True

    _d = field_validator("max_duration", "freshness")(classmethod(lambda cls, v: _duration(v) if v else v))

    @model_validator(mode="after")
    def _shape(self):
        if bool(self.pipeline) == bool(self.data_product):
            raise ValueError("an SLA is about one pipeline or one data product (set pipeline or data_product)")
        if self.pipeline:
            check_key(self.pipeline)
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(self.timezone)
        except Exception:  # noqa: BLE001 - an unknown zone
            raise ValueError(f"timezone: {self.timezone!r} is not a time zone (e.g. Europe/Berlin)")
        if not self.days or any(d not in range(7) for d in self.days):
            raise ValueError("days: 0 (Monday) to 6 (Sunday)")
        if not (self.on_failure or self.deadline or self.max_duration or self.freshness or self.anomalies):
            raise ValueError("an SLA checks something: on_failure, deadline, max_duration, freshness or anomalies")
        return self


MODELS.update({"pipeline_sla": PipelineSla, "data_product": DataProduct, "exporter": Exporter})


def last_deadline(now_ms: int, hhmm: str, tz: str, days: list[int]) -> Optional[int]:
    """The most recent deadline at or before now (on one of ``days``), in ms."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    local = datetime.fromtimestamp(now_ms / 1000, tz=zone)
    h, m = (int(x) for x in hhmm.split(":"))
    for back in range(8):
        d = (local - timedelta(days=back)).replace(hour=h, minute=m, second=0, microsecond=0)
        if d.weekday() in days and d <= local:
            return int(d.timestamp() * 1000)
    return None


# ---- OpenTelemetry for pipeline runs ---------------------------------------------------------------------------------
_next_trace: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("cairn_pipeline_trace", default=None)
_otel: dict[str, Any] = {}


def _tracer():
    """A tracer of its own (service ``cairn-pipelines``, every run sampled unless CAIRN_OBS_PIPELINE_SAMPLE_RATIO
    says otherwise) on Cairn's exporter path: the same redaction, the same endpoint."""
    from cairn.observability import telemetry

    if not telemetry.enabled() or telemetry._off("traces"):
        return None
    if "tracer" in _otel:
        return _otel["tracer"]
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.id_generator import IdGenerator
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

    from cairn import __version__

    class RunIds(IdGenerator):
        def generate_span_id(self) -> int:
            return random.getrandbits(64) or 1

        def generate_trace_id(self) -> int:
            return _next_trace.get() or random.getrandbits(128) or 1

    ratio = float(os.environ.get("CAIRN_OBS_PIPELINE_SAMPLE_RATIO", "1.0"))
    tp = TracerProvider(resource=Resource.create({"service.name": "cairn-pipelines", "service.version": __version__,
                                                  "service.namespace": "cairn"}),
                        sampler=TraceIdRatioBased(ratio), id_generator=RunIds())
    tp.add_span_processor(telemetry.RedactingSpanProcessor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=telemetry._signal_url("traces"), headers=telemetry._headers()))))
    _otel["provider"] = tp
    _otel["tracer"] = tp.get_tracer("cairn.pipelines")
    return _otel["tracer"]


def _instruments():
    if "runs" not in _otel:
        from opentelemetry import metrics

        m = metrics.get_meter("cairn.pipelines")
        _otel["runs"] = m.create_counter("cairn_pipeline_runs_total", unit="1", description="Pipeline runs that ended")
        _otel["duration"] = m.create_histogram("cairn_pipeline_run_duration_seconds", unit="s",
                                               description="How long pipeline runs took")
        _otel["sla"] = m.create_counter("cairn_pipeline_sla_misses_total", unit="1",
                                        description="Pipeline SLA misses (an SLA's alert that fired)")
    return _otel


def flush(timeout_ms: int = 10_000) -> None:
    if "provider" in _otel:
        _otel["provider"].force_flush(timeout_ms)


# ---- the service --------------------------------------------------------------------------------------------------
class PipelineObservability:
    def __init__(self, engine, obs, graph=None, audit=None, bi_catalog=None, flows=None,
                 airflow: Optional[dict] = None, resolve: Optional[Callable[[str], str]] = None):
        """``obs``: the ObservabilityService (alerts, incidents, Tempo and Loki); ``airflow``: the default Airflow's
        REST API settings (``cairn.flows.steps.airflow_settings``); ``resolve``: names catalog.schema.table as a
        dataset URN."""
        self.engine, self.obs, self.graph, self.audit = engine, obs, graph, audit
        self.bi_catalog, self.flows = bi_catalog, flows
        self.resolve = resolve or (lambda t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{t},PROD)")
        # Every Airflow to read: callables returning [(name, settings)]. The default one (CAIRN_AIRFLOW_URL) is
        # "default"; managed Airflow deployments add theirs.
        self.airflow_sources: list[Callable[[], list[tuple[str, dict]]]] = []
        if airflow and airflow.get("url"):
            self.airflow_sources.append(lambda: [("default", airflow)])
        self.sync_every_s = int(os.environ.get("CAIRN_OBS_PIPELINES_SYNC_SECONDS", "60"))
        self._cursor: dict[str, int] = {}
        self._last_sync = 0.0
        obs.evaluators.append(self.tick)
        obs.incident_extras.append(self._incident_pipelines)

    # ---- recording runs ------------------------------------------------------------------------------------------
    def record(self, source: str, name: str, run_id: str, state: str, start_ms: Optional[int] = None,
               end_ms: Optional[int] = None, tasks: Optional[list[dict]] = None, inputs: Optional[list[str]] = None,
               outputs: Optional[list[str]] = None, error: Optional[str] = None, ol_run_id: Optional[str] = None,
               instance: str = "", detail: Optional[dict] = None, key: Optional[str] = None) -> dict:
        """Adds or updates one run (tasks merge by task id; inputs and outputs accumulate). Emits it as telemetry
        once it has ended."""
        key = key or pipeline_key(source, name, instance)
        state = state if state in STATES else "running"
        run_id = str(run_id)[:250]
        now = _now()
        with self.engine.begin() as c:
            r = c.execute(select(pipeline_runs).where(and_(pipeline_runs.c.pipeline == key,
                                                           pipeline_runs.c.run_id == run_id))).first()
            if r is None:
                rid = "prn_" + uuid.uuid4().hex[:20]
                cur: dict = {"tasks": [], "inputs": [], "outputs": [], "state": None, "start_ms": None,
                             "end_ms": None, "error": None, "ol_run_id": None, "detail": {}, "emitted": False}
            else:
                rid = r.id
                cur = dict(r._mapping)
            merged = {t["task"]: t for t in cur["tasks"] or []}
            for t in tasks or []:
                old = merged.get(t["task"], {})
                merged[t["task"]] = {**old, **{k: v for k, v in t.items() if v is not None}}
                if merged[t["task"]].get("error"):
                    merged[t["task"]]["error"] = _clean(merged[t["task"]]["error"], 2000)
            # A run that ended stays ended (a late START event does not reopen it).
            new_state = cur["state"] if cur["state"] in TERMINAL and state not in TERMINAL else state
            start = min(x for x in (cur["start_ms"], start_ms) if x) if (cur["start_ms"] or start_ms) else None
            end = end_ms or cur["end_ms"] if new_state in TERMINAL else None
            values = dict(
                pipeline=key, source=source[:32], instance=(instance or "")[:64], run_id=run_id,
                ol_run_id=(ol_run_id or cur["ol_run_id"] or None) and str(ol_run_id or cur["ol_run_id"])[:64],
                state=new_state, start_ms=start, end_ms=end,
                duration_ms=(end - start) if (end and start) else None,
                error=_clean(error) or cur["error"],
                inputs=sorted(set(cur["inputs"] or []) | set(inputs or []))[:200],
                outputs=sorted(set(cur["outputs"] or []) | set(outputs or []))[:200],
                tasks=sorted(merged.values(), key=lambda t: (t.get("start_ms") or 0, t["task"]))[:500],
                detail={**(cur["detail"] or {}), **(detail or {})}, updated_ms=now)
            values["trace_id"] = run_trace_id(key, run_id, values["ol_run_id"])
            if r is None:
                c.execute(insert(pipeline_runs).values(id=rid, emitted=False, **values))
            else:
                c.execute(update(pipeline_runs).where(pipeline_runs.c.id == rid).values(**values))
        run = self._run(rid)
        if run["state"] in TERMINAL and not cur["emitted"] and not self._has_open_tasks(run):
            self.emit(run)
        return run

    @staticmethod
    def _has_open_tasks(run: dict) -> bool:
        return any(t.get("state") in ("running", "queued") for t in run["tasks"])

    def _run(self, rid: str) -> dict:
        with self.engine.connect() as c:
            r = c.execute(select(pipeline_runs).where(pipeline_runs.c.id == rid)).first()
        if r is None:
            raise ObservabilityError("no such run", 404)
        return dict(r._mapping)

    # ---- from OpenLineage ---------------------------------------------------------------------------------------
    def on_openlineage(self, event: dict, job_urn: str, inputs: list[str], outputs: list[str]) -> None:
        """One OpenLineage RunEvent (the receiver calls this after it recorded the lineage)."""
        state = OL_STATE.get(str(event.get("eventType") or "").upper())
        job = event.get("job") or {}
        run = event.get("run") or {}
        ns, name = str(job.get("namespace") or ""), str(job.get("name") or "")
        if state is None or not run.get("runId") or ns.startswith("cairn-flows"):
            return                                   # Cairn's own flows are recorded as they run
        facets = run.get("facets") or {}
        parent = (facets.get("parent") or {})
        when = _ms(event.get("eventTime")) or _now()
        orchestrator = _orchestrator(ns, job, facets)
        af = facets.get("airflow") or facets.get("airflowDagRun") or {}
        dag_run_id = ((af.get("dagRun") or {}).get("run_id") or (af.get("dag_run") or {}).get("run_id")) \
            if isinstance(af, dict) else None
        err = ((facets.get("errorMessage") or {}).get("message")) if state == "failed" else None
        job_type = str(((job.get("facets") or {}).get("jobType") or {}).get("jobType") or "").upper()
        is_task = bool(parent.get("run", {}).get("runId")) and job_type != "DAG"
        if is_task:
            pjob = parent.get("job") or {}
            pname = str(pjob.get("name") or name)
            task = name[len(pname) + 1:] if name.startswith(pname + ".") else name
            run_id = dag_run_id or str(parent["run"]["runId"])
            t = {"task": task[:200], "state": state, "try": _try_number(facets),
                 ("start_ms" if state == "running" else "end_ms"): when, "error": err}
            self.record(orchestrator, pname, run_id, "running", start_ms=when, tasks=[t], inputs=inputs,
                        outputs=outputs, detail={"job_urn": job_urn})
            return
        run_id = dag_run_id or str(run["runId"])
        self.record(orchestrator, name, run_id, state, start_ms=when if state == "running" else None,
                    end_ms=when if state in TERMINAL else None, inputs=inputs, outputs=outputs, error=err,
                    ol_run_id=str(run["runId"]), detail={"job_urn": job_urn, "openlineage_namespace": ns[:200]})

    # ---- from Cairn's flows, dbt and jobs ------------------------------------------------------------------------
    def on_flow_run(self, run: dict, flow_name: str, steps: list[dict]) -> None:
        tasks = [{"task": s["key"], "state": FLOW_STATE.get(s["status"], "running"), "start_ms": s.get("started_ms"),
                  "end_ms": s.get("finished_ms"), "try": s.get("attempt") or 1, "error": s.get("error")}
                 for s in steps]
        failed = next((s for s in steps if s["status"] == "failed"), None)
        self.record("flow", flow_name, run["id"], FLOW_STATE.get(run["status"], "running"),
                    start_ms=run.get("started_ms") or run.get("created_ms"), end_ms=run.get("finished_ms"),
                    tasks=tasks, error=run.get("error") or (f"{failed['key']}: {failed.get('error') or 'failed'}"
                                                            if failed else None),
                    detail={"flow_run": run["id"], "trigger": run.get("trigger"), "run_as": run.get("run_as")})

    def on_dbt_run(self, invocation: str, project: str, ok: bool, started_ms: Optional[int], finished_ms: int,
                   results: list[dict], built: list[str], actor: str) -> None:
        tasks = []
        for r in results[:500]:
            st = str(r.get("status") or "")
            secs = float(r.get("execution_time") or 0)
            timing = {t.get("name"): t for t in r.get("timing") or [] if isinstance(t, dict)}
            ex = timing.get("execute") or {}
            tasks.append({"task": str(r.get("unique_id") or "")[:200],
                          "state": "success" if st in ("success", "pass") else "skipped" if st == "skipped"
                          else "failed" if st in ("error", "fail", "runtime error") else "success",
                          "start_ms": _ms(ex.get("started_at")), "end_ms": _ms(ex.get("completed_at")),
                          "seconds": round(secs, 2), "error": r.get("message") if st in ("error", "fail") else None})
        failed = [t for t in tasks if t["state"] == "failed"]
        start = started_ms or (finished_ms - int(sum(t.get("seconds") or 0 for t in tasks) * 1000))
        self.record("dbt", project, invocation, "success" if ok else "failed", start_ms=start, end_ms=finished_ms,
                    tasks=tasks, outputs=built, error=(f"{failed[0]['task']}: {failed[0].get('error') or 'failed'}"
                                                       if failed else None), detail={"actor": actor})

    def on_job_run(self, job: dict, rid: str, status: str, started_ms: Optional[int], finished_ms: Optional[int],
                   error: Optional[str]) -> None:
        state = {"succeeded": "success", "success": "success", "failed": "failed", "cancelled": "failed",
                 "timed_out": "failed"}.get(status, "running")
        self.record("job", job.get("name") or job.get("id") or "job", rid, state, start_ms=started_ms,
                    end_ms=finished_ms, error=error, detail={"job_type": job.get("type"), "owner": job.get("owner")})

    # ---- from Airflow's REST API ---------------------------------------------------------------------------------
    def _airflows(self) -> list[tuple[str, dict]]:
        out = []
        for src in list(self.airflow_sources):
            try:
                out += list(src() or [])
            except Exception:  # noqa: BLE001 - one source failing never hides the others
                logger.exception("an Airflow source for pipeline observability failed")
        return out

    def sync_airflow(self, since_ms: Optional[int] = None) -> dict:
        """Reads the DAG runs that changed since the last read (7 days the first time) and their task instances."""
        from cairn.flows.steps import Airflow, StepFailed

        out: dict[str, Any] = {"airflows": 0, "runs": 0, "errors": []}
        for name, cfg in self._airflows():
            out["airflows"] += 1
            try:
                af = Airflow(cfg)
                cursor = since_ms or self._cursor.get(name) or (_now() - 7 * 86_400_000)
                started = _now()
                seen: dict[tuple[str, str], dict] = {}
                iso = datetime.fromtimestamp((cursor - 600_000) / 1000, tz=timezone.utc).isoformat()
                for q in (f"end_date_gte={quote(iso)}", "state=running&state=queued"):
                    offset = 0
                    while offset < 1000:
                        page = af.call("GET", f"/dags/~/dagRuns?limit=100&offset={offset}&{q}")
                        runs = page.get("dag_runs") or []
                        for r in runs:
                            seen[(r["dag_id"], r["dag_run_id"])] = r
                        offset += len(runs)
                        if not runs or offset >= int(page.get("total_entries") or 0):
                            break
                for (dag, rid), r in seen.items():
                    tis = af.call("GET", f"/dags/{quote(dag, safe='')}/dagRuns/{quote(rid, safe='')}/taskInstances"
                                         "?limit=1000").get("task_instances") or []
                    tasks = [{"task": ti["task_id"] + (f"[{ti['map_index']}]" if (ti.get("map_index") or -1) >= 0
                                                       else ""),
                              "state": AIRFLOW_STATE.get(ti.get("state"), "running"),
                              "airflow_state": ti.get("state"), "try": ti.get("try_number") or 0,
                              "start_ms": _ms(ti.get("start_date")), "end_ms": _ms(ti.get("end_date")),
                              "operator": ti.get("operator") or ti.get("operator_name"),
                              "pool": ti.get("pool"), "queue": ti.get("queue")} for ti in tis]
                    state = AIRFLOW_STATE.get(r.get("state"), "running")
                    failed = [t["task"] for t in tasks if t["airflow_state"] == "failed"]
                    self.record("airflow", dag, rid, state, start_ms=_ms(r.get("start_date") or r.get("queued_at")),
                                end_ms=_ms(r.get("end_date")), tasks=tasks, instance=name,
                                error=f"failed tasks: {', '.join(failed)}" if failed else None,
                                detail={"run_type": r.get("run_type"), "airflow": name,
                                        "logical_date": r.get("logical_date")})
                    out["runs"] += 1
                self._cursor[name] = started
            except (StepFailed, KeyError, ValueError) as e:
                out["errors"].append(f"{name}: {redact.scrub_text(str(e))[:300]}")
        self._last_sync = time.time()
        return out

    # ---- telemetry ------------------------------------------------------------------------------------------------
    def emit(self, run: dict) -> bool:
        """The run as OpenTelemetry: a trace (its tasks as spans), metrics and a log record. Once per run."""
        with self.engine.begin() as c:
            n = c.execute(update(pipeline_runs).where(and_(pipeline_runs.c.id == run["id"],
                                                           pipeline_runs.c.emitted.is_(False))).values(emitted=True)).rowcount
        if not n:
            return False
        status = run["state"]
        attrs = {"cairn.pipeline": run["pipeline"], "cairn.pipeline.source": run["source"],
                 "cairn.pipeline.status": status}
        try:
            inst = _instruments()
            inst["runs"].add(1, attrs)
            if run["duration_ms"] is not None:
                inst["duration"].record(run["duration_ms"] / 1000, attrs)
        except Exception:  # noqa: BLE001 - telemetry never fails a run's recording
            logger.exception("pipeline metrics for %s failed", run["pipeline"])
        tracer = _tracer()
        if tracer is None:
            return True
        try:
            from opentelemetry import trace
            from opentelemetry.trace import SpanKind, Status, StatusCode

            start_ms = run["start_ms"] or run["end_ms"] or _now()
            end_ms = run["end_ms"] or start_ms
            root_attrs = {**attrs, "cairn.pipeline.run_id": run["run_id"],
                          "cairn.pipeline.instance": run["instance"] or "", "cairn.pipeline.tasks": len(run["tasks"]),
                          "cairn.pipeline.outputs": ",".join(run["outputs"][:10])[:1000]}
            if run["ol_run_id"]:
                root_attrs["openlineage.run_id"] = run["ol_run_id"]
            if run["source"] == "airflow":
                root_attrs["airflow.dag_id"] = run["pipeline"].split(":", 1)[1].split("@")[0]
                root_attrs["airflow.dag_run.run_id"] = run["run_id"]
            token = _next_trace.set(int(run["trace_id"], 16))
            try:
                root = tracer.start_span(f"pipeline {run['pipeline']}", kind=SpanKind.INTERNAL,
                                         start_time=start_ms * 1_000_000, attributes=root_attrs)
            finally:
                _next_trace.reset(token)
            ctx = trace.set_span_in_context(root)
            for t in run["tasks"]:
                ts = t.get("start_ms") or start_ms
                te = t.get("end_ms") or ts
                s = tracer.start_span(f"task {t['task']}", context=ctx, start_time=ts * 1_000_000,
                                      attributes={"cairn.pipeline": run["pipeline"], "cairn.task": t["task"],
                                                  "cairn.task.status": t.get("state") or "",
                                                  "cairn.task.try": int(t.get("try") or 0)})
                if t.get("state") == "failed":
                    s.set_status(Status(StatusCode.ERROR, (t.get("error") or "failed")[:500]))
                s.end(end_time=max(te, ts) * 1_000_000)
            if status == "failed":
                root.set_status(Status(StatusCode.ERROR, (run["error"] or "failed")[:500]))
            with trace.use_span(root, end_on_exit=False):
                (logger.error if status == "failed" else logger.info)(
                    "pipeline %s run %s %s in %ss%s", run["pipeline"], run["run_id"], status,
                    round((run["duration_ms"] or 0) / 1000, 1), f": {run['error']}" if run["error"] else "")
            root.end(end_time=max(end_ms, start_ms) * 1_000_000)
        except Exception:  # noqa: BLE001
            logger.exception("pipeline trace for %s failed", run["pipeline"])
        return True

    # ---- reading ---------------------------------------------------------------------------------------------------
    @staticmethod
    def _public(r: dict, anomaly: Optional[dict] = None) -> dict:
        start = r["start_ms"]
        tasks = []
        for t in r["tasks"] or []:
            ts, te = t.get("start_ms"), t.get("end_ms")
            tasks.append({**t, "start": _iso(ts), "end": _iso(te),
                          "offset_ms": (ts - start) if (ts and start) else None,
                          "duration_ms": (te - ts) if (te and ts) else None})
        return {"id": r["id"], "pipeline": r["pipeline"], "source": r["source"], "instance": r["instance"] or None,
                "run_id": r["run_id"], "ol_run_id": r["ol_run_id"], "state": r["state"], "start": _iso(start),
                "end": _iso(r["end_ms"]), "start_ms": start, "end_ms": r["end_ms"], "duration_ms": r["duration_ms"],
                "trace_id": r["trace_id"], "error": r["error"], "inputs": r["inputs"] or [],
                "outputs": r["outputs"] or [], "tasks": tasks, "detail": r["detail"] or {}, "anomaly": anomaly}

    def _rows(self, pipeline: Optional[str] = None, since_ms: Optional[int] = None, limit: int = 20000) -> list[dict]:
        q = select(pipeline_runs)
        if pipeline:
            q = q.where(pipeline_runs.c.pipeline == pipeline)
        if since_ms:
            q = q.where(or_(pipeline_runs.c.start_ms >= since_ms, pipeline_runs.c.end_ms >= since_ms,
                            pipeline_runs.c.state.in_(("running", "queued"))))
        q = q.order_by(pipeline_runs.c.start_ms.desc()).limit(limit)
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(q)]

    @staticmethod
    def anomaly(run: dict, history: list[dict]) -> Optional[dict]:
        """Whether a run is anomalously slow against the pipeline's earlier successful runs."""
        d = run.get("duration_ms")
        if d is None:
            if run.get("state") == "running" and run.get("start_ms"):
                d = _now() - run["start_ms"]
            else:
                return None
        prior = [h["duration_ms"] for h in history if h["state"] == "success" and h["duration_ms"] is not None
                 and h["id"] != run["id"] and (h["start_ms"] or 0) < (run["start_ms"] or _now())][:20]
        if len(prior) < 5:
            return None
        mean = statistics.fmean(prior)
        sd = statistics.pstdev(prior)
        threshold = max(mean + 3 * sd, mean * 1.5, mean + 30_000)
        if d <= threshold:
            return None
        return {"duration_ms": d, "mean_ms": round(mean), "stdev_ms": round(sd), "threshold_ms": round(threshold),
                "z": round((d - mean) / sd, 1) if sd else None, "runs": len(prior)}

    def pipelines(self, q: str = "", source: str = "", days: int = 30) -> list[dict]:
        rows = self._rows(since_ms=_now() - days * 86_400_000)
        by: dict[str, list[dict]] = {}
        for r in rows:
            by.setdefault(r["pipeline"], []).append(r)
        firing = self._firing_by_pipeline()
        slas, products = self.obs._docs("pipeline_sla"), self.obs._docs("data_product")
        out = []
        for key, runs in sorted(by.items()):
            if source and runs[0]["source"] != source:
                continue
            if q and q.lower() not in key.lower():
                continue
            out.append(self._summary(key, runs, firing.get(key, []), slas, products))
        return out

    def _summary(self, key: str, runs: list[dict], firing: list[dict], slas: list[dict], products: list[dict]) -> dict:
        done = [r for r in runs if r["state"] in TERMINAL][:30]
        durations = [r["duration_ms"] for r in done if r["duration_ms"] is not None]
        last = runs[0] if runs else None
        last_done = done[0] if done else None
        anomalies = sum(1 for r in done[:10] if self.anomaly(r, runs))
        rate = (sum(1 for r in done if r["state"] == "success") / len(done)) if done else None
        product_ids = [p["id"] for p in products if key in (p.get("pipelines") or [])]
        if last_done and last_done["state"] == "failed":
            health = "failing"
        elif firing:
            health = "sla_missed"
        elif last_done and self.anomaly(last_done, runs):
            health = "anomalous"
        elif last_done:
            health = "healthy"
        else:
            health = "unknown"
        return {"key": key, "source": runs[0]["source"] if runs else key.split(":", 1)[0],
                "name": key.split(":", 1)[1], "instance": runs[0]["instance"] or None if runs else None,
                "health": health, "runs": len(runs), "success_rate": None if rate is None else round(rate, 4),
                "p50_ms": int(statistics.median(durations)) if durations else None,
                "p95_ms": int(_pct(durations, 0.95)) if durations else None,
                "trend": [{"run_id": r["run_id"], "state": r["state"], "duration_ms": r["duration_ms"],
                           "start": _iso(r["start_ms"])} for r in reversed(done)],
                "anomalies": anomalies,
                "last_run": self._public(last) if last else None,
                "running": sum(1 for r in runs if r["state"] in ("running", "queued")),
                "outputs": sorted({o for r in runs[:10] for o in r["outputs"] or []})[:50],
                "slas": [s["id"] for s in slas if s.get("pipeline") == key
                         or s.get("data_product") in product_ids],
                "data_products": product_ids, "firing": firing}

    def pipeline(self, key: str, limit: int = 50) -> dict:
        check_key(key)
        runs = self._rows(pipeline=key, limit=500)
        if not runs:
            raise ObservabilityError(f"no runs of {key} recorded", 404)
        summary = self._summary(key, runs, self._firing_by_pipeline().get(key, []), self.obs._docs("pipeline_sla"),
                                self.obs._docs("data_product"))
        summary["items"] = [self._public(r, self.anomaly(r, runs)) for r in runs[:limit]]
        return summary

    def _row(self, key: str, run_id: str) -> dict:
        check_key(key)
        with self.engine.connect() as c:
            r = c.execute(select(pipeline_runs).where(and_(pipeline_runs.c.pipeline == key,
                                                           pipeline_runs.c.run_id == run_id))).first()
        if r is None:
            raise ObservabilityError(f"no run {run_id} of {key}", 404)
        return dict(r._mapping)

    def run(self, key: str, run_id: str, actor: str = ACTOR) -> dict:
        r = self._row(key, run_id)
        out = self._public(r, self.anomaly(r, self._rows(pipeline=key, limit=60)))
        out["impact"] = self.impact(r, actor)
        out["quality"] = self.quality(r)
        out["airflow_trace_id"] = self._airflow_trace(r) if r["source"] == "airflow" else None
        out["links"] = self.obs.backends.links(r["trace_id"], "cairn-pipelines", r["start_ms"] or 0,
                                               r["end_ms"] or _now()) if r["trace_id"] else {}
        return out

    def _airflow_trace(self, r: dict) -> Optional[str]:
        """Airflow's own trace of the DAG run (Airflow 3.3's OpenTelemetry), found in Tempo by its run id."""
        tempo = self.obs.backends.tempo
        if tempo is None:
            return None
        try:
            since = max(3600, int(time.time() - (r["start_ms"] or _now()) / 1000) + 3600)
            found = tempo.search({"airflow.dag_run.run_id": r["run_id"]}, 10, None, min(since, 30 * 86400))
        except Exception:  # noqa: BLE001 - optional: the run's own trace is still there
            return None
        for t in found or []:
            if str(t.get("root_name") or "").startswith("dag_run.") or (
                    t.get("root_service") and str(t.get("root_service")) != "cairn-pipelines"):
                return t.get("trace_id")
        return None

    # ---- logs ------------------------------------------------------------------------------------------------------
    def task_log(self, key: str, run_id: str, task: str, try_number: Optional[int] = None, limit: int = 400) -> dict:
        """A task's log (redacted): Airflow's API for Airflow runs, the step's log for a flow, else the lines in
        Loki that carry the run's trace id."""
        r = self._row(key, run_id)
        t = next((x for x in r["tasks"] or [] if x["task"] == task), None)
        if t is None:
            raise ObservabilityError(f"no task {task} in run {run_id}", 404)
        lines: list[str] = []
        source = ""
        if r["source"] == "airflow":
            cfg = dict(self._airflows()).get(r["instance"] or "default")
            if cfg is None:
                raise ObservabilityError(f"the Airflow {r['instance'] or 'default'} is not configured here", 503)
            from cairn.flows.steps import Airflow, StepFailed

            dag = key.split(":", 1)[1].split("@")[0]
            n = int(try_number or t.get("try") or 1)
            base = re.sub(r"\[\d+\]$", "", task)
            mi = re.search(r"\[(\d+)\]$", task)
            path = (f"/dags/{quote(dag, safe='')}/dagRuns/{quote(run_id, safe='')}/taskInstances/"
                    f"{quote(base, safe='')}/logs/{max(1, n)}?full_content=true" + (f"&map_index={mi.group(1)}" if mi else ""))
            try:
                doc = Airflow(cfg).call("GET", path)
            except StepFailed as e:
                raise ObservabilityError(f"Airflow: {redact.scrub_text(str(e))}", 502)
            lines = _airflow_log_lines(doc)
            source = "airflow"
        elif r["source"] == "flow":
            from cairn.flows.engine import flow_step_runs

            with self.engine.connect() as c:
                row = c.execute(select(flow_step_runs.c.log, flow_step_runs.c.error).where(and_(
                    flow_step_runs.c.run_id == run_id, flow_step_runs.c.key == task))).first()
            lines = ((row.log or "") + ("\n" + row.error if row.error else "")).splitlines() if row else []
            source = "flow"
        if not lines and r["trace_id"] and self.obs.backends.loki is not None:
            try:
                got = self.obs.logs(trace_id=r["trace_id"], since_minutes=10080, limit=limit)
                lines = [x["line"] for x in got.get("items") or []]
                source = "loki"
            except ObservabilityError:
                pass
        clean = [redact.scrub_text(x) for x in lines[-limit:]]
        return {"pipeline": key, "run_id": run_id, "task": task, "try": t.get("try"), "source": source or None,
                "lines": clean, "truncated": len(lines) > limit}

    # ---- impact and quality ---------------------------------------------------------------------------------------
    def _job_outputs(self, r: dict) -> list[str]:
        outs = list(r["outputs"] or [])
        ju = (r["detail"] or {}).get("job_urn")
        if not outs and ju and self.graph is not None:
            outs = list((self.graph.get_aspect(ju, "dataJobInputOutput") or {}).get("outputDatasets") or [])
        return outs

    def _downstream_of(self, urn: str) -> set[str]:
        out: set[str] = set()
        g = self.graph
        if g is None:
            return out
        try:
            data = g.graphql(_DOWNSTREAMS, {"urn": urn}) or {}
            ent = data.get("entity") or {}
            for key in ("downstream", "consumers"):
                for rel in ((ent.get(key) or {}).get("relationships") or []):
                    u = (rel.get("entity") or {}).get("urn")
                    if not u:
                        continue
                    if u.startswith("urn:li:dataJob:"):
                        out |= set((g.get_aspect(u, "dataJobInputOutput") or {}).get("outputDatasets") or [])
                    else:
                        out.add(u)
        except Exception:  # noqa: BLE001 - a graph without relationships (the in-memory one): the aspects below
            pass
        aspects = getattr(g, "aspects", None)
        if isinstance(aspects, dict):
            for (u, a), v in list(aspects.items()):
                if a == "upstreamLineage" and any((x or {}).get("dataset") == urn for x in (v or {}).get("upstreams") or []):
                    out.add(u)
                elif a == "dataJobInputOutput" and urn in ((v or {}).get("inputDatasets") or []):
                    out |= set((v or {}).get("outputDatasets") or [])
        out.discard(urn)
        return out

    def impact(self, r: dict, actor: str = ACTOR) -> dict:
        """What a run's outputs reach: downstream tables, BI assets, data products and pipelines."""
        direct = self._job_outputs(r)
        seen = {u: 0 for u in direct}
        frontier = list(direct)
        for hop in range(1, 4):
            nxt = []
            for u in frontier:
                for d in sorted(self._downstream_of(u)):
                    if d not in seen and len(seen) < 300:
                        seen[d] = hop
                        nxt.append(d)
            frontier = nxt
        tables = [{"urn": u, "name": _name(u), "hops": h} for u, h in sorted(seen.items(), key=lambda x: (x[1], x[0]))]
        assets: dict[tuple[str, str], dict] = {}
        if self.bi_catalog is not None:
            for u in list(seen)[:100]:
                try:
                    for a in self.bi_catalog.impact(actor, u).get("assets") or []:
                        assets.setdefault((a["kind"], a["id"]), {**a, "tables": []})["tables"].append(u)
                except Exception:  # noqa: BLE001 - BI lineage is optional
                    logger.debug("bi impact of %s failed", u, exc_info=True)
        affected = set(seen)
        products = []
        for p in self.obs._docs("data_product"):
            hit = sorted(affected & set(self._product_tables(p)))
            if hit or r["pipeline"] in (p.get("pipelines") or []):
                products.append({"id": p["id"], "name": p["name"], "tables": hit})
        readers: dict[str, list[str]] = {}
        if affected:
            for o in self._rows(since_ms=_now() - 7 * 86_400_000, limit=5000):
                if o["pipeline"] != r["pipeline"]:
                    hit = affected & set(o["inputs"] or [])
                    if hit:
                        readers.setdefault(o["pipeline"], sorted(hit))
        return {"outputs": direct, "tables": tables, "assets": list(assets.values()), "data_products": products,
                "pipelines": [{"pipeline": k, "tables": v} for k, v in sorted(readers.items())]}

    def quality(self, r: dict) -> list[dict]:
        """Cairn's quality checks on the tables the run wrote, evaluated from its start to six hours after it."""
        outs = self._job_outputs(r)
        if not outs:
            return []
        try:
            from cairn.quality.store import evaluations, monitors
        except ImportError:
            return []
        start = (r["start_ms"] or _now()) - 60_000
        end = (r["end_ms"] or _now()) + 6 * 3_600_000
        out = []
        with self.engine.connect() as c:
            mons = {m.id: m for m in c.execute(select(monitors.c.id, monitors.c.name, monitors.c.type,
                                                      monitors.c.dataset_urn).where(monitors.c.dataset_urn.in_(outs)))}
            if not mons:
                return []
            for e in c.execute(select(evaluations).where(and_(evaluations.c.monitor_id.in_(list(mons)),
                                                               evaluations.c.ts_ms >= start, evaluations.c.ts_ms <= end))
                               .order_by(evaluations.c.ts_ms.desc()).limit(500)):
                m = mons[e.monitor_id]
                out.append({"table": m.dataset_urn, "monitor": m.id, "name": m.name, "type": m.type,
                            "status": e.status, "message": _clean(e.message, 500), "at": _iso(e.ts_ms)})
        latest: dict[str, dict] = {}
        for x in out:
            latest.setdefault(x["monitor"], x)
        return list(latest.values())

    # ---- data products ---------------------------------------------------------------------------------------------
    def _product_tables(self, p: dict) -> list[str]:
        out = [t if t.startswith("urn:li:") else self.resolve(t) for t in p.get("tables") or []]
        if p.get("catalog_urn") and self.graph is not None:
            try:
                props = self.graph.get_aspect(p["catalog_urn"], "dataProductProperties") or {}
                out += [a.get("destinationUrn") for a in props.get("assets") or []
                        if str(a.get("destinationUrn") or "").startswith("urn:li:dataset:")]
            except Exception:  # noqa: BLE001
                pass
        return sorted(set(out))

    def last_update(self, urns: list[str]) -> dict[str, Optional[int]]:
        """When each table was last written: Cairn's dataset changes, or the end of a successful run writing it."""
        out: dict[str, Optional[int]] = {u: None for u in urns}
        if not urns:
            return out
        try:
            from sqlalchemy import func

            from cairn.pipelines.changes import dataset_changes

            with self.engine.connect() as c:
                for urn, ts in c.execute(select(dataset_changes.c.urn, func.max(dataset_changes.c.ts_ms))
                                         .where(dataset_changes.c.urn.in_(urns)).group_by(dataset_changes.c.urn)):
                    out[urn] = int(ts)
        except Exception:  # noqa: BLE001 - no dataset changes here
            pass
        for r in self._rows(since_ms=_now() - 30 * 86_400_000, limit=5000):
            if r["state"] == "success" and r["end_ms"]:
                for o in set(r["outputs"] or []) & set(urns):
                    out[o] = max(out[o] or 0, r["end_ms"])
        return out

    def data_product(self, pid: str) -> dict:
        p = self.obs.get("data_product", pid)
        tables = self._product_tables(p)
        upd = self.last_update(tables)
        now = _now()
        slas = [s for s in self.obs._docs("pipeline_sla") if s.get("data_product") == pid]
        fresh = min((seconds(s["freshness"]) for s in slas if s.get("freshness")), default=None)
        rows = self._rows(since_ms=now - 30 * 86_400_000)
        firing = self._firing_by_pipeline()
        allp = self.obs._docs("pipeline_sla"), self.obs._docs("data_product")
        pipes = []
        for key in p.get("pipelines") or []:
            runs = [r for r in rows if r["pipeline"] == key]
            pipes.append(self._summary(key, runs, firing.get(key, []), *allp) if runs
                         else {"key": key, "health": "unknown", "runs": 0})
        tlist = [{"urn": u, "name": _name(u), "last_update": _iso(upd[u]),
                  "age_s": (now - upd[u]) // 1000 if upd[u] else None,
                  "fresh": None if fresh is None else bool(upd[u] and now - upd[u] <= fresh * 1000)} for u in tables]
        alerts = [a for a in self.obs.alerts(state="firing") if a["labels"].get("data_product") == pid]
        health = ("failing" if any(x.get("health") == "failing" for x in pipes) else
                  "sla_missed" if alerts or any(t["fresh"] is False for t in tlist) else
                  "healthy" if pipes or tlist else "unknown")
        return {**p, "health": health, "table_status": tlist, "pipeline_status": pipes,
                "slas": [s["id"] for s in slas], "firing": alerts}

    # ---- SLAs ------------------------------------------------------------------------------------------------------
    def _firing_by_pipeline(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for a in self.obs.alerts(state="firing"):
            p = a["labels"].get("pipeline")
            if p and a["source"].startswith("sla:"):
                out.setdefault(p, []).append({"id": a["id"], "name": a["name"], "condition": a["labels"].get("condition"),
                                              "severity": a["severity"], "incident": a.get("incident_id")})
        return out

    def evaluate(self, now: Optional[int] = None) -> dict:
        now = now or _now()
        out = {"slas": 0, "fired": 0, "resolved": 0, "errors": 0}
        rows = self._rows(since_ms=now - 8 * 86_400_000)
        by: dict[str, list[dict]] = {}
        for r in rows:
            by.setdefault(r["pipeline"], []).append(r)
        products = {p["id"]: p for p in self.obs._docs("data_product")}
        for sla in self.obs._docs("pipeline_sla"):
            src = f"sla:{sla['id']}"
            if not sla.get("enabled", True):
                self.obs._advance(src, self._spec(sla), {}, now, "the SLA is turned off")
                continue
            out["slas"] += 1
            try:
                active = self._check(sla, by, products, now)
            except Exception as e:  # noqa: BLE001 - never resolve what could not be checked
                out["errors"] += 1
                self.obs._set_status("pipeline_sla", sla["id"], {"at": _iso(now), "error": str(e)[:300]})
                continue
            n = self.obs._advance(src, self._spec(sla), active, now)
            if n["fired"]:
                try:
                    _instruments()["sla"].add(n["fired"], {"sla": sla["id"]})
                except Exception:  # noqa: BLE001
                    pass
            out["fired"] += n["fired"]
            out["resolved"] += n["resolved"]
            self.obs._set_status("pipeline_sla", sla["id"], {"at": _iso(now), "missed": len(active),
                                                             "conditions": sorted({m["labels"]["condition"]
                                                                                   for m in active.values()})})
        return out

    @staticmethod
    def _spec(sla: dict) -> dict:
        about = sla.get("pipeline") or f"data product {sla.get('data_product')}"
        return {"name": f"SLA {sla['name']} missed", "severity": sla.get("severity", "warning"),
                "summary": ("{{ $labels.condition }} of {{ $labels.pipeline }}: {{ $labels.detail }} (SLA "
                            + sla["name"] + f" on {about})"),
                "routes": sla.get("routes") or [], "recipients": sla.get("recipients") or [],
                "open_incident": sla.get("open_incident", True), "pending_for": "0s", "repeat_interval": "4h",
                "group": f"sla:{sla['id']}", "runbook_url": sla.get("runbook_url") or "",
                "labels": {**(sla.get("labels") or {}), "sla": sla["id"]}}

    def _check(self, sla: dict, by: dict[str, list[dict]], products: dict, now: int) -> dict:
        src = f"sla:{sla['id']}"
        active: dict[str, dict] = {}
        base = {"sla": sla["id"]}
        if sla.get("data_product"):
            p = products.get(sla["data_product"])
            if p is None:
                raise ValueError(f"no data product {sla['data_product']}")
            pipelines = list(p.get("pipelines") or [])
            tables = self._product_tables(p)
            base["data_product"] = p["id"]
        else:
            p = None
            pipelines = [sla["pipeline"]]
            tables = sorted({o for r in by.get(sla["pipeline"], [])[:10] for o in r["outputs"] or []})

        def add(labels: dict, value: Optional[float] = None) -> None:
            lab = {**base, **{k: str(v)[:200] for k, v in labels.items()}}
            lab["service"] = lab.get("pipeline", "")
            active[_fingerprint(src, {k: v for k, v in lab.items() if k != "detail"})] = {"labels": lab, "value": value}

        for key in pipelines:
            runs = by.get(key, [])
            done = [r for r in runs if r["state"] in TERMINAL]
            if sla.get("on_failure", True) and done and done[0]["state"] == "failed":
                r = done[0]
                failed = [t["task"] for t in r["tasks"] or [] if t.get("state") == "failed"]
                add({"pipeline": key, "condition": "failure", "run": r["run_id"],
                     "task": failed[0] if failed else "",
                     "detail": f"run {r['run_id']} failed" + (f" in {', '.join(failed[:3])}" if failed else "")})
            if sla.get("deadline"):
                due = last_deadline(now, sla["deadline"], sla.get("timezone") or "UTC", sla.get("days") or list(range(7)))
                if due is not None:
                    ok = [r for r in done if r["state"] == "success" and r["end_ms"] and r["end_ms"] > due - 86_400_000]
                    if not ok:
                        add({"pipeline": key, "condition": "deadline", "due": _iso(due),
                             "detail": f"no successful run by {sla['deadline']} {sla.get('timezone') or 'UTC'}"},
                            (now - due) / 60_000)
            if sla.get("max_duration"):
                limit = seconds(sla["max_duration"]) * 1000
                running = [r for r in runs if r["state"] in ("running", "queued") and r["start_ms"]
                           and now - r["start_ms"] > limit]
                late = running[0] if running else (done[0] if done and (done[0]["duration_ms"] or 0) > limit else None)
                if late:
                    took = (late["duration_ms"] or now - late["start_ms"]) / 1000
                    add({"pipeline": key, "condition": "duration", "run": late["run_id"],
                         "detail": f"run {late['run_id']} {'has run' if late in running else 'took'} {int(took)}s, "
                                   f"over {sla['max_duration']}"}, took)
            if sla.get("anomalies") and done:
                a = self.anomaly(done[0], runs)
                if a:
                    add({"pipeline": key, "condition": "anomaly", "run": done[0]["run_id"],
                         "detail": f"run {done[0]['run_id']} took {a['duration_ms'] // 1000}s, usually "
                                   f"{a['mean_ms'] // 1000}s"}, a["duration_ms"] / 1000)
        if sla.get("freshness"):
            limit = seconds(sla["freshness"]) * 1000
            upd = self.last_update(tables)
            for t in tables:
                if upd[t] is None or now - upd[t] > limit:
                    add({"pipeline": sla.get("pipeline") or (pipelines[0] if len(pipelines) == 1 else
                                                             f"data-product:{sla.get('data_product')}"),
                         "condition": "freshness", "table": _name(t),
                         "detail": f"{_name(t)} " + (f"last updated {int((now - upd[t]) / 60_000)} min ago"
                                                     if upd[t] else "never updated") + f", over {sla['freshness']}"},
                        None if upd[t] is None else (now - upd[t]) / 1000)
        return active

    def tick(self, now: Optional[int] = None) -> dict:
        """The notifier's pass: read Airflow when due, then evaluate the SLAs."""
        out: dict[str, Any] = {}
        if self.airflow_sources and time.time() - self._last_sync >= self.sync_every_s:
            try:
                out["airflow"] = self.sync_airflow()
            except Exception as e:  # noqa: BLE001
                logger.exception("reading Airflow for pipeline observability failed")
                out["airflow"] = {"error": str(e)[:300]}
        out["slas"] = self.evaluate(now)
        return out

    # ---- incidents and the SRE agent --------------------------------------------------------------------------------
    def _incident_pipelines(self, inc: dict) -> dict:
        """For an incident's pipeline alerts: the runs they are about (the SRE agent's context)."""
        out = []
        for a in inc.get("alerts") or []:
            key, run = a["labels"].get("pipeline"), a["labels"].get("run")
            if not key or not KEY.match(key):
                continue
            try:
                r = self._row(key, run) if run else self._rows(pipeline=key, limit=1)[0]
            except (ObservabilityError, IndexError):
                continue
            p = self._public(r)
            out.append({"pipeline": key, "run_id": p["run_id"], "state": p["state"], "error": p["error"],
                        "failed_tasks": [t["task"] for t in p["tasks"] if t.get("state") == "failed"],
                        "start": p["start"], "end": p["end"], "trace_id": p["trace_id"]})
        return {"pipelines": out} if out else {}

    def diagnose(self, key: str, run_id: str) -> dict:
        """Why a run failed: its failed tasks, the first one's log, and the likely causes with their evidence."""
        r = self._row(key, run_id)
        pub = self._public(r)
        failed = [t for t in pub["tasks"] if t.get("state") == "failed"]
        upstream_failed = [t["task"] for t in pub["tasks"] if t.get("airflow_state") == "upstream_failed"]
        log: dict = {}
        if failed:
            try:
                log = self.task_log(key, run_id, failed[0]["task"], limit=200)
            except ObservabilityError as e:
                log = {"error": str(e)}
        text = "\n".join([pub["error"] or "", *[t.get("error") or "" for t in failed], *(log.get("lines") or [])])
        causes: list[dict] = []
        start, end = (r["start_ms"] or _now()), (r["end_ms"] or _now())
        # Upstream: a pipeline that writes one of this run's inputs failed last.
        for o in self._rows(since_ms=start - 2 * 86_400_000, limit=5000):
            if o["pipeline"] == key or o["state"] not in TERMINAL:
                continue
            hit = set(o["outputs"] or []) & set(r["inputs"] or [])
            if hit and o["state"] == "failed" and (o["end_ms"] or 0) <= end:
                causes.append({"kind": "upstream_failure", "likelihood": 0.8,
                               "text": f"the upstream pipeline {o['pipeline']} (run {o['run_id']}) that writes "
                                       f"{', '.join(_name(h) for h in sorted(hit))} failed",
                               "evidence": {"pipeline": o["pipeline"], "run_id": o["run_id"]}})
                break
        for kind, rx in CAUSES.items():
            m = rx.search(text)
            if m:
                line = next((x for x in text.splitlines() if rx.search(x)), m.group(0))
                causes.append({"kind": kind, "likelihood": {"schema_change": 0.7, "permission": 0.75,
                                                            "resource": 0.6}[kind],
                               "text": {"schema_change": "a table or column it uses changed or is missing",
                                        "permission": "it was refused access", "resource": "it ran out of a resource "
                                        "(memory, disk, time or connections)"}[kind],
                               "evidence": {"log": redact.scrub_text(line.strip())[:400]}})
        denials = self._denials(start - 600_000, end + 300_000, set(r["inputs"] or []) | set(r["outputs"] or []))
        if denials:
            perm = next((c for c in causes if c["kind"] == "permission"), None)
            if perm is None:
                perm = {"kind": "permission", "likelihood": 0.6, "text": "Cairn refused it access", "evidence": {}}
                causes.append(perm)
            perm["likelihood"] = max(perm["likelihood"], 0.85)
            perm["evidence"]["audit"] = denials[:5]
            perm["text"] = (f"Cairn denied {denials[0]['privilege']} on {denials[0]['resource'] or 'the platform'} to "
                            f"{denials[0]['actor']}")
        changes = self.obs.changes(start - 2 * 3_600_000, end, limit=20)
        if changes:
            causes.append({"kind": "recent_change", "likelihood": 0.4,
                           "text": "something changed shortly before: " + "; ".join(c["text"] for c in changes[:3]),
                           "evidence": {"changes": changes[:5]}})
        if upstream_failed and not failed:
            causes.append({"kind": "upstream_task", "likelihood": 0.5,
                           "text": f"tasks were skipped because an upstream task failed: {', '.join(upstream_failed)}",
                           "evidence": {"tasks": upstream_failed}})
        causes.sort(key=lambda c: -c["likelihood"])
        suggestions = []
        kinds = {c["kind"] for c in causes}
        if r["state"] == "failed":
            if not kinds or kinds & {"resource", "upstream_failure", "recent_change"} or not kinds - {"recent_change"}:
                suggestions.append({"kind": "runbook_action", "action": "rerun_pipeline",
                                    "params": {"pipeline": key, "run_id": run_id},
                                    "rationale": "rerun the failed tasks once the cause is gone"
                                    + (" (the upstream pipeline has to succeed first)" if "upstream_failure" in kinds
                                       else "")})
            if "permission" in kinds:
                suggestions.append({"kind": "fix", "text": "grant the pipeline's identity the privilege it was "
                                    "refused (an access request or a policy change a person approves), then rerun"})
            if "schema_change" in kinds:
                suggestions.append({"kind": "fix", "text": "update the task's query to the table's current schema "
                                    "(or restore the column), then rerun"})
        return {"pipeline": key, "run_id": run_id, "state": r["state"], "error": pub["error"],
                "failed_tasks": [{"task": t["task"], "try": t.get("try"), "error": t.get("error")} for t in failed],
                "log": {"task": failed[0]["task"] if failed else None, "source": log.get("source"),
                        "error": log.get("error"), "tail": (log.get("lines") or [])[-40:]},
                "causes": causes, "suggestions": suggestions, "trace_id": r["trace_id"],
                "inputs": r["inputs"] or [], "outputs": r["outputs"] or []}

    def _denials(self, start_ms: int, end_ms: int, urns: set[str]) -> list[dict]:
        from cairn.common.audit import audit_log

        out = []
        with self.engine.connect() as c:
            for row in c.execute(select(audit_log.c.ts, audit_log.c.actor, audit_log.c.subject, audit_log.c.details)
                                 .where(and_(audit_log.c.category == "authz", audit_log.c.action == "deny",
                                             audit_log.c.ts >= _iso(start_ms)[:19], audit_log.c.ts <= _iso(end_ms)))
                                 .order_by(audit_log.c.seq.desc()).limit(200)):
                d = row.details if isinstance(row.details, dict) else {}
                if urns and row.subject and row.subject not in urns and not d.get("privilege"):
                    continue
                out.append({"at": row.ts, "actor": row.actor, "resource": row.subject,
                            "privilege": d.get("privilege") or d.get("action") or "access",
                            "reason": redact.scrub_text(str(d.get("reason") or ""))[:300]})
        rel = [x for x in out if x["resource"] in urns]
        return rel or out

    def rerun(self, actor: str, key: str, run_id: str) -> dict:
        """Runs a failed run's failed tasks again: Airflow clears them (and their downstream), a flow reruns."""
        r = self._row(key, run_id)
        if r["state"] != "failed":
            raise ObservabilityError(f"run {run_id} of {key} did not fail ({r['state']})", 409)
        if r["source"] == "airflow":
            from cairn.flows.steps import Airflow, StepFailed

            cfg = dict(self._airflows()).get(r["instance"] or "default")
            if cfg is None:
                raise ObservabilityError(f"the Airflow {r['instance'] or 'default'} is not configured here", 503)
            dag = key.split(":", 1)[1].split("@")[0]
            body = {"dry_run": False, "dag_run_id": run_id, "only_failed": True, "reset_dag_runs": True,
                    "include_downstream": True}
            try:
                got = Airflow(cfg).call("POST", f"/dags/{quote(dag, safe='')}/clearTaskInstances", body)
            except StepFailed as e:
                raise ObservabilityError(f"Airflow: {redact.scrub_text(str(e))}", 502)
            cleared = [t.get("task_id") for t in (got.get("task_instances") or [])]
            result = {"rerun": "airflow", "cleared": cleared}
        elif r["source"] == "flow":
            if self.flows is None:
                raise ObservabilityError("flows are not available here", 503)
            from cairn.flows.model import FlowError

            try:
                result = {"rerun": "flow", "run": self.flows.rerun(actor, run_id)}
            except FlowError as e:
                raise ObservabilityError(str(e), getattr(e, "status", 400))
        else:
            raise ObservabilityError(f"{r['source']} runs are rerun where they run (Cairn cannot start them)", 409)
        with self.engine.begin() as c:
            c.execute(update(pipeline_runs).where(pipeline_runs.c.id == r["id"]).values(
                state="running", end_ms=None, duration_ms=None, emitted=False, updated_ms=_now()))
        if self.audit is not None:
            self.audit.record("observability", "observability.pipeline_rerun", actor, key,
                              {"run": run_id, **{k: v for k, v in result.items() if k != "run"}})
        return {"pipeline": key, "run_id": run_id, **result}


_DOWNSTREAMS = """
query cairnPipelineImpact($urn: String!) {
  entity(urn: $urn) {
    ... on Dataset {
      downstream: relationships(input: {types: ["DownstreamOf"], direction: INCOMING, start: 0, count: 500}) {
        relationships { entity { urn } }
      }
      consumers: relationships(input: {types: ["Consumes"], direction: INCOMING, start: 0, count: 200}) {
        relationships { entity { urn } }
      }
    }
  }
}
"""


def _name(urn: str) -> str:
    m = re.match(r"^urn:li:dataset:\(urn:li:dataPlatform:[^,]+,(.+),[A-Z]+\)$", urn or "")
    return m.group(1) if m else urn


def _pct(xs: list[int], q: float) -> float:
    s = sorted(xs)
    if not s:
        return math.nan
    i = min(len(s) - 1, max(0, int(math.ceil(q * len(s))) - 1))
    return s[i]


def _try_number(facets: dict) -> Optional[int]:
    ti = ((facets.get("airflow") or {}).get("taskInstance") or {}) if isinstance(facets.get("airflow"), dict) else {}
    v = ti.get("try_number")
    return int(v) if isinstance(v, (int, float)) else None


def _orchestrator(ns: str, job: dict, facets: dict) -> str:
    integ = str(((job.get("facets") or {}).get("jobType") or {}).get("integration") or "").lower()
    if integ in ("airflow", "spark", "dbt", "dagster", "flink"):
        return integ
    if facets.get("spark_properties") or facets.get("spark_version") or "spark" in ns.lower():
        return "spark"
    from cairn.sources.openlineage import ORCHESTRATORS

    return next((v for k, v in ORCHESTRATORS.items() if k in ns.lower()), "openlineage")


def _airflow_log_lines(doc: Any) -> list[str]:
    """Airflow 3's structured log (``content``: events) or Airflow 2's text, as lines."""
    content = doc.get("content") if isinstance(doc, dict) else doc
    if isinstance(content, str):
        return content.splitlines()
    lines = []
    for e in content or []:
        if isinstance(e, dict):
            ev = str(e.get("event") or "")
            lvl = str(e.get("level") or "")
            ts = str(e.get("timestamp") or "")
            extra = " ".join(f"{k}={v}" for k, v in e.items() if k in ("error_detail",) and v)
            lines.append(" ".join(x for x in (ts, lvl.upper() if lvl else "", ev, extra) if x))
        elif isinstance(e, (list, tuple)) and len(e) == 2:
            lines += str(e[1]).splitlines()
        else:
            lines.append(str(e))
    return lines


def json_safe(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))
