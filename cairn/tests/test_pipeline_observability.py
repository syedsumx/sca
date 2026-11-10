"""Pipeline observability (L145) without its backends: Airflow's OpenTelemetry settings from Cairn's (the helper the
Compose overlay, the Helm values and managed Airflow deployments use), OTLP exporters as configuration (rendering,
redaction refused when missing, header values from secret scopes, the files written), and through Cairn's API: runs
from OpenLineage (Airflow's DAG and task events), flows, dbt and jobs; health, anomalies, SLAs firing and resolving
through the alert machinery and a real webhook receiver, data products, lineage impact, quality on a run, diagnosis
with the audit log's denials, privileges. Real Airflow, the Collector and the stack: test_pipeline_observability_e2e."""

from __future__ import annotations

import base64
import http.server
import json
import os
import socket
import stat
import threading
import time
import uuid

import pytest
import yaml
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.observability import airflow_otel_env
from cairn.observability import exporters as ex
from cairn.services import build_services

ROOT = os.path.join(os.path.dirname(__file__), "..")
ADMIN = "urn:li:corpuser:admin"
VIEWER = "urn:li:corpuser:vic"
NOBODY = "urn:li:corpuser:nobody"
H = {"X-Cairn-Actor": ADMIN}
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
DAILY = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.daily,PROD)"
REPORT = "urn:li:dataset:(urn:li:dataPlatform:iceberg,finance.report,PROD)"


# ---- Airflow's OpenTelemetry from Cairn's --------------------------------------------------------------------------
def test_airflow_otel_env_from_cairns_settings():
    env = airflow_otel_env({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://cairn-otel-collector:4318/"})
    assert env["AIRFLOW__TRACES__OTEL_ON"] == env["AIRFLOW__METRICS__OTEL_ON"] == "True"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://cairn-otel-collector:4318"
    assert env["OTEL_TRACES_EXPORTER"] == "otlp_proto_http" and env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    assert env["OTEL_SERVICE_NAME"] == "airflow" and env["OTEL_TRACES_SAMPLER_ARG"] == "1.0"
    assert not any(k.endswith(("_HOST", "_PORT")) for k in env)          # Airflow 3.2 deprecated those
    # A deployment of its own, Cairn's sampling, an explicit Collector for Airflow.
    env = airflow_otel_env({"CAIRN_OBS_AIRFLOW_OTLP_ENDPOINT": "http://col.cairn.svc:4318",
                            "OTEL_TRACES_SAMPLER": "parentbased_traceidratio", "OTEL_TRACES_SAMPLER_ARG": "0.25",
                            "OTEL_EXPORTER_OTLP_HEADERS": "x-key=secret"}, "airflow-team-a", deployment="team-a")
    assert env["OTEL_TRACES_SAMPLER_ARG"] == "0.25" and env["OTEL_SERVICE_NAME"] == "airflow-team-a"
    assert env["OTEL_RESOURCE_ATTRIBUTES"].endswith("cairn.airflow.deployment=team-a")
    assert "secret" not in json.dumps(env)                                # Cairn's own headers never go to Airflow
    assert airflow_otel_env({}) == {}                                     # telemetry off: nothing
    # Fail closed: Cairn sends straight to a vendor with credentials, Airflow would bypass the Collector's redaction.
    with pytest.raises(ValueError, match="Collector"):
        airflow_otel_env({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.vendor.example", "CAIRN_OTEL_HEADERS_FILE": "/x"})
    for bad in ({"CAIRN_OBS_AIRFLOW_OTLP_ENDPOINT": "cairn-otel-collector:4317"},
                {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318", "CAIRN_OTEL_SAMPLE_RATIO": "2"}):
        with pytest.raises(ValueError):
            airflow_otel_env(bad)
    with pytest.raises(ValueError):
        airflow_otel_env({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318"}, "bad name")


def test_the_compose_overlay_and_the_helm_values_are_the_helpers_output():
    with open(os.path.join(ROOT, "docker-compose.observability.yml")) as fh:
        overlay = yaml.safe_load(fh)
    assert overlay["x-airflow-otel"] == airflow_otel_env({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://cairn-otel-collector:4318"})
    for svc in ("airflow-api-server", "airflow-scheduler", "airflow-dag-processor", "airflow-triggerer"):
        assert overlay["services"][svc]["environment"] == overlay["x-airflow-otel"]
    col = overlay["services"]["cairn-otel-collector"]
    assert col["command"][-1] == "--config=/etc/otelcol/exporters/exporters.yaml"
    with open(os.path.join(ROOT, "deploy", "airflow", "values-cairn.yaml")) as fh:
        values = yaml.safe_load(fh)
    want = airflow_otel_env({"CAIRN_OBS_AIRFLOW_OTLP_ENDPOINT": "http://cairn-otel-collector.cairn.svc:4318"})
    got = {e["name"]: e["value"] for e in values["env"] if e["name"] in want}
    assert got == want


# ---- exporters as configuration --------------------------------------------------------------------------------------
def test_exporters_render_through_redaction_and_anything_else_is_refused():
    base = ex.bundled_config()
    ex.check_redaction(base)                                           # the bundled pipelines redact before exporting
    with open(os.path.join(ROOT, "helm", "cairn", "files", "observability", "otel-collector.yaml")) as fh:
        assert yaml.safe_load(fh) == base                              # the chart's copy is the same
    docs = [ex.validate({"id": "honeycomb", "endpoint": "https://api.honeycomb.io", "signals": ["traces", "logs"],
                         "headers": [{"name": "x-honeycomb-team", "scope": "otel", "key": "hc"}], "sampling_ratio": 0.2}),
            ex.validate({"id": "own", "protocol": "grpc", "endpoint": "otel.internal:4317", "tls": {"insecure": True}}),
            ex.validate({"id": "off", "endpoint": "https://x.example", "enabled": False})]
    cfg = ex.render(docs)
    pipes = cfg["service"]["pipelines"]
    assert sorted(pipes) == ["logs/export-honeycomb", "logs/export-own", "metrics/export-own", "traces/export-honeycomb",
                             "traces/export-own"]
    assert pipes["traces/export-honeycomb"]["processors"] == [
        "memory_limiter", "attributes/credentials", "transform/urls", "redaction/values",
        "probabilistic_sampler/honeycomb", "batch"]
    assert cfg["processors"]["probabilistic_sampler/honeycomb"] == {"sampling_percentage": 20.0}
    hc = cfg["exporters"]["otlp_http/honeycomb"]
    assert hc["headers"] == {"x-honeycomb-team": "${env:CAIRN_EXPORTER_HONEYCOMB_0}"}   # never the value
    assert cfg["exporters"]["otlp_grpc/own"]["tls"] == {"insecure": True}
    ex.check_redaction(ex.merge(base, cfg))
    bad = {"service": {"pipelines": {"traces/leak": {"receivers": ["otlp"], "processors": ["batch"],
                                                     "exporters": ["otlp_http/x"]}}}}
    with pytest.raises(ex.ExporterError, match="redaction"):
        ex.check_redaction(ex.merge(base, bad))
    with pytest.raises(ex.ExporterError, match="before the batch"):
        ex.check_redaction({"service": {"pipelines": {"logs/x": {"processors": [
            "batch", "attributes/credentials", "transform/urls", "redaction/values"], "exporters": ["y"]}}}})
    for body, why in (({"id": "a1", "endpoint": "http://plain:4318"}, "plaintext"),
                      ({"id": "a1", "endpoint": "https://u:p@x.example"}, "credentials"),
                      ({"id": "a1", "protocol": "grpc", "endpoint": "https://x:4317", "tls": {"insecure": True}}, "one or"),
                      ({"id": "a1", "endpoint": "https://x", "headers": [{"name": "host", "scope": "s", "key": "k"}]}, "host"),
                      ({"id": "a1", "endpoint": "https://x", "signals": ["traces", "traces"]}, "at most once")):
        with pytest.raises(ex.ExporterError, match=why):
            ex.validate(body)


def test_secret_values_go_to_an_env_file_of_their_own(tmp_path):
    docs = [ex.validate({"id": "dd", "endpoint": "https://otlp.datadoghq.com",
                         "headers": [{"name": "dd-api-key", "scope": "otel", "key": "dd"}]})]
    env = ex.secret_env(docs, lambda scope, key: f"value-of-{scope}-{key}")
    assert env == {"CAIRN_EXPORTER_DD_0": "value-of-otel-dd"}
    with pytest.raises(ex.ExporterError, match="one line"):
        ex.secret_env(docs, lambda s, k: "two\nlines")
    written = ex.write(str(tmp_path / "out"), ex.render(docs), env)
    yml, envf = written
    assert "value-of" not in open(yml).read() and "${env:CAIRN_EXPORTER_DD_0}" in open(yml).read()
    assert open(envf).read() == "CAIRN_EXPORTER_DD_0=value-of-otel-dd\n"
    assert stat.S_IMODE(os.stat(envf).st_mode) == 0o600
    assert ex.to_yaml(ex.render([])).rstrip().endswith("{}")
    with pytest.raises(ex.ExporterError, match="not configured"):
        ex.write("", {}, {})


# ---- through the API ---------------------------------------------------------------------------------------------------
class Receiver(http.server.BaseHTTPRequestHandler):
    got: list = []

    def do_POST(self):  # noqa: N802
        Receiver.got.append(json.loads(self.rfile.read(int(self.headers.get("content-length", 0)))))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


def host_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]


@pytest.fixture(scope="module")
def receiver():
    ip = host_ip()
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = os.environ.get("NO_PROXY", "") + f",{ip}"
    srv = http.server.ThreadingHTTPServer((ip, 0), Receiver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://{ip}:{srv.server_address[1]}/hook"
    srv.shutdown()


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph

    tmp = tmp_path_factory.mktemp("pipeobs")
    old = {k: os.environ.get(k) for k in ("CAIRN_SECRETS_KEY", "CAIRN_OBS_EXPORTERS_DIR", "CAIRN_AIRFLOW_URL")}
    os.environ["CAIRN_SECRETS_KEY"] = base64.b64encode(os.urandom(32)).decode()
    os.environ["CAIRN_OBS_EXPORTERS_DIR"] = str(tmp / "exporters")
    os.environ.pop("CAIRN_AIRFLOW_URL", None)
    graph = InMemoryGraph()
    graph.aspects[(REPORT, "upstreamLineage")] = {"upstreams": [{"dataset": DAILY, "type": "TRANSFORMED"}]}
    s = build_services(Settings(database_url=f"sqlite:///{tmp}/c.db", service_token="svc-" + uuid.uuid4().hex,
                                dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")}),
                       graph=graph, with_ai=False, with_gateway=False)
    me = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="admin", effect="allow", actions=["*"], condition=me(ADMIN)),
                                            Policy(id="vic", effect="allow", actions=["VIEW_OPERATIONS"],
                                                   condition=me(VIEWER))]))
    c = TestClient(create_api_app(s))
    yield c, s, tmp
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def ol(event_type: str, name: str, run_id: str, when: float, parent: tuple[str, str] = None, inputs=(), outputs=(),
       error: str = "", dag_run_id: str = "") -> dict:
    facets: dict = {}
    if parent:
        facets["parent"] = {"job": {"namespace": "airflow://prod", "name": parent[0]}, "run": {"runId": parent[1]}}
        if dag_run_id:
            facets["airflow"] = {"dagRun": {"run_id": dag_run_id}, "taskInstance": {"try_number": 1}}
    elif dag_run_id:
        facets["airflowDagRun"] = {"dagRun": {"run_id": dag_run_id}}
    if error:
        facets["errorMessage"] = {"message": error, "programmingLanguage": "python"}
    ds = lambda u: {"namespace": "iceberg://", "name": u.split(",")[1]}  # noqa: E731
    return {"eventType": event_type, "eventTime": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when)) + ".000Z",
            "producer": "https://github.com/apache/airflow", "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json",
            "job": {"namespace": "airflow://prod", "name": name,
                    "facets": {"jobType": {"integration": "AIRFLOW", "jobType": "TASK" if parent else "DAG",
                                           "processingType": "BATCH"}}},
            "run": {"runId": run_id, "facets": facets}, "inputs": [ds(u) for u in inputs],
            "outputs": [ds(u) for u in outputs]}


def send(c, events):
    r = c.post("/openlineage/api/v1/lineage", json=events, headers=H)
    assert r.status_code in (200, 201), r.text


def dag_run(c, dag: str, run: str, start: float, seconds: float, fail: str = "", inputs=(ORDERS,), outputs=(DAILY,)):
    ol_run = str(uuid.uuid4())
    events = [ol("START", dag, ol_run, start, dag_run_id=run)]
    for i, task in enumerate(("extract", "load")):
        tid = str(uuid.uuid4())
        t0 = start + i * seconds / 2
        failed = fail and task == "load"
        events += [ol("START", f"{dag}.{task}", tid, t0, (dag, ol_run), dag_run_id=run),
                   ol("FAIL" if failed else "COMPLETE", f"{dag}.{task}", tid, t0 + seconds / 2, (dag, ol_run),
                      inputs=inputs if task == "extract" else (), outputs=outputs if task == "load" else (),
                      error=fail if failed else "", dag_run_id=run)]
    events.append(ol("FAIL" if fail else "COMPLETE", dag, ol_run, start + seconds, dag_run_id=run,
                     error=fail))
    send(c, events)
    return ol_run


def test_runs_from_openlineage_health_anomaly_and_privileges(world):
    c, s, _ = world
    now = time.time()
    for i in range(8):                                      # a steady history: about 60 s each
        dag_run(c, "daily_sales", f"scheduled__{i}", now - 86_400 * (9 - i) / 9, 60 + i)
    slow_ol = dag_run(c, "daily_sales", "scheduled__slow", now - 3000, 900)
    items = c.get("/v1/observability/pipelines", headers=H).json()["items"]
    p = next(x for x in items if x["key"] == "airflow:daily_sales")
    assert p["runs"] == 9 and p["success_rate"] == 1.0 and p["source"] == "airflow"
    assert p["health"] == "anomalous" and p["anomalies"] >= 1 and len(p["trend"]) == 9
    assert p["p95_ms"] >= 800_000 and DAILY in p["outputs"]
    run = c.get("/v1/observability/pipelines/airflow:daily_sales/runs/scheduled__slow", headers=H).json()
    assert run["state"] == "success" and run["ol_run_id"] == slow_ol and run["trace_id"] == uuid.UUID(slow_ol).hex
    assert [t["task"] for t in run["tasks"]] == ["extract", "load"] and run["tasks"][1]["offset_ms"] == 450_000
    assert run["anomaly"]["runs"] >= 5 and run["inputs"] == [ORDERS] and run["outputs"] == [DAILY]
    assert run["impact"]["tables"][0]["urn"] == DAILY and {t["urn"] for t in run["impact"]["tables"]} == {DAILY, REPORT}
    # Read-only telemetry may look, not change; anyone else may not look.
    v = {"X-Cairn-Actor": VIEWER}
    assert c.get("/v1/observability/pipelines", headers=v).status_code == 200
    assert c.post("/v1/observability/pipeline-slas", headers=v, json={}).status_code == 403
    assert c.get("/v1/observability/pipelines", headers={"X-Cairn-Actor": NOBODY}).status_code == 403
    assert c.get("/v1/observability/pipelines/airflow:nothing", headers=H).status_code == 404
    assert c.get("/v1/observability/pipelines/not a key", headers=H).status_code == 422


def test_flows_dbt_and_jobs_are_pipelines_too(world):
    c, s, _ = world
    po = s.pipeline_obs
    now = int(time.time() * 1000)
    po.on_flow_run({"id": "fr1", "status": "failed", "started_ms": now - 5000, "finished_ms": now, "trigger": "manual",
                    "run_as": ADMIN, "error": None},
                   "nightly", [{"key": "load", "status": "ok", "started_ms": now - 5000, "finished_ms": now - 3000,
                                "attempt": 1, "error": None},
                               {"key": "publish", "status": "failed", "started_ms": now - 3000, "finished_ms": now,
                                "attempt": 2, "error": "postgres://etl:hunter2@db/x refused"}])
    r = c.get("/v1/observability/pipelines/flow:nightly/runs/fr1", headers=H).json()
    assert r["state"] == "failed" and r["error"].startswith("publish:") and "hunter2" not in json.dumps(r)
    # dbt through its real ingestion endpoint.
    manifest = {"metadata": {"project_name": "shop", "adapter_type": "trino", "invocation_id": "inv-1"},
                "nodes": {"model.shop.daily": {"resource_type": "model", "name": "daily", "database": "iceberg",
                                               "schema": "sales", "alias": "daily", "depends_on": {"nodes": []}}}}
    results = {"results": [{"unique_id": "model.shop.daily", "status": "success", "execution_time": 2.5}],
               "elapsed_time": 3}
    assert c.post("/pipelines/dbt/runs", headers=H, json={"manifest": manifest, "run_results": results}).status_code \
        in (200, 201)
    d = c.get("/v1/observability/pipelines/dbt:shop", headers=H).json()
    assert d["items"][0]["run_id"] == "inv-1" and d["items"][0]["state"] == "success"
    assert d["items"][0]["tasks"][0]["task"] == "model.shop.daily"
    po.on_job_run({"name": "spark-agg", "type": "spark", "owner": ADMIN}, "jr1", "succeeded", now - 10_000, now, None)
    assert c.get("/v1/observability/pipelines/job:spark-agg", headers=H).json()["items"][0]["duration_ms"] == 10_000


def test_slas_fire_through_routes_and_incidents_then_resolve(world, receiver):
    c, s, _ = world
    assert c.put("/notify/routes/pipes-hook", headers=H, json={
        "id": "pipes-hook", "channel": "webhook", "target": receiver, "event_types": ["job.failed"]}).status_code \
        in (200, 201)
    r = c.post("/v1/observability/data-products", headers=H, json={
        "id": "finance", "name": "Finance report", "tables": [DAILY, "lake.finance.ledger"],
        "pipelines": ["airflow:fin_etl"]})
    assert r.status_code == 201, r.text
    assert c.post("/v1/observability/data-products", headers=H, json={"id": "empty", "name": "x"}).status_code == 422
    sla = {"id": "finance-by-six", "name": "Finance by six", "data_product": "finance", "deadline": "00:00",
           "freshness": "1h", "max_duration": "10m", "severity": "critical", "routes": ["pipes-hook"]}
    assert c.post("/v1/observability/pipeline-slas", headers=H, json=sla).status_code == 201
    bad = [{**sla, "id": "x1", "pipeline": "airflow:a"}, {**sla, "id": "x2", "timezone": "Mars/Base"},
           {**sla, "id": "x3", "routes": ["nope"]}, {"id": "x4", "name": "n", "pipeline": "airflow:a",
                                                    "on_failure": False}]
    for b in bad:
        assert c.post("/v1/observability/pipeline-slas", headers=H, json=b).status_code == 422, b
    now = time.time()
    dag_run(c, "fin_etl", "manual__1", now - 90, 30, fail="Access Denied: postgres://etl:hunter2@db/x",
            inputs=(ORDERS,), outputs=(DAILY,))
    s.audit.record("authz", "deny", "urn:li:corpuser:svc-airflow", ORDERS,
                   {"privilege": "SELECT", "reason": "no policy allows it"})
    out = s.observability.evaluate()
    assert out["fired"] >= 2, out
    firing = [a for a in c.get("/v1/observability/alerts", params={"state": "firing"}, headers=H).json()["items"]
              if a["source"] == "sla:finance-by-six"]
    conds = {a["labels"]["condition"] for a in firing}
    assert {"failure", "freshness", "deadline"} <= conds, conds
    fail = next(a for a in firing if a["labels"]["condition"] == "failure")
    assert fail["labels"]["pipeline"] == "airflow:fin_etl" and fail["labels"]["run"] == "manual__1"
    assert fail["labels"]["data_product"] == "finance" and fail["incident_id"]
    stale = next(a for a in firing if a["labels"]["condition"] == "freshness")
    assert stale["labels"]["table"] == "lake.finance.ledger"     # never written: catalog.schema.table resolved
    s.notify.dispatch_pending()
    deadline = time.time() + 20
    while time.time() < deadline and not any(m["type"] == "observability.alert_firing" for m in Receiver.got):
        time.sleep(0.5)
    got = [m for m in Receiver.got if m["type"] == "observability.alert_firing"]
    assert got and "Finance by six" in got[0]["title"] and "hunter2" not in json.dumps(Receiver.got)
    # The product's status, the pipeline's health, the incident's context names the run.
    st = c.get("/v1/observability/data-products/finance/status", headers=H).json()
    # sales.daily was written by daily_sales half an hour ago (fresh); the ledger never.
    assert st["health"] == "failing" and {t["name"]: t["fresh"] for t in st["table_status"]} == {
        "sales.daily": True, "lake.finance.ledger": False}
    p = c.get("/v1/observability/pipelines/airflow:fin_etl", headers=H).json()
    assert p["health"] == "failing" and p["slas"] == ["finance-by-six"] and p["data_products"] == ["finance"]
    ctx = c.get(f"/v1/observability/incidents/{fail['incident_id']}/context", headers=H).json()
    assert ctx["pipelines"][0]["pipeline"] == "airflow:fin_etl" and ctx["pipelines"][0]["failed_tasks"] == ["load"]
    # Diagnosis: Cairn's audit log says who was denied what; the log says Access Denied; a rerun once fixed.
    d = c.get("/v1/observability/pipelines/airflow:fin_etl/runs/manual__1/diagnose", headers=H).json()
    assert d["causes"][0]["kind"] == "permission" and d["causes"][0]["evidence"]["audit"][0]["resource"] == ORDERS
    assert any(x["kind"] == "fix" for x in d["suggestions"]) and "hunter2" not in json.dumps(d)
    # An Airflow run is rerun through Airflow: here there is none, said so; a run that did not fail is refused.
    assert c.post("/v1/observability/pipelines/airflow:fin_etl/runs/manual__1/rerun", headers=H).status_code == 503
    assert c.post("/v1/observability/pipelines/airflow:daily_sales/runs/scheduled__1/rerun", headers=H).status_code == 409
    # A successful run on time resolves failure, duration and the product's freshness for what it wrote.
    dag_run(c, "fin_etl", "manual__2", time.time() - 60, 30, inputs=(ORDERS,), outputs=(DAILY,))
    s.observability.evaluate()
    left = {a["labels"]["condition"] + ":" + a["labels"].get("table", "")
            for a in c.get("/v1/observability/alerts", params={"state": "firing"}, headers=H).json()["items"]
            if a["source"] == "sla:finance-by-six"}
    assert left == {"freshness:lake.finance.ledger"}, left
    # Deleting the SLA ends its alerts.
    assert c.delete("/v1/observability/pipeline-slas/finance-by-six", headers=H).status_code == 204
    assert not [a for a in c.get("/v1/observability/alerts", params={"state": "firing"}, headers=H).json()["items"]
                if a["source"] == "sla:finance-by-six"]


def test_quality_on_a_run_and_bi_impact(world):
    c, s, _ = world
    from cairn.quality.store import evaluations, monitors

    now = int(time.time() * 1000)
    with s.engine.begin() as conn:
        conn.execute(monitors.insert().values(id="m-daily", name="daily not empty", dataset_urn=DAILY, type="volume",
                                              spec={}, status="ACTIVE", enabled=True, next_run_at_ms=None,
                                              created_at="2026-01-01", updated_at="2026-01-01"))
        conn.execute(evaluations.insert().values(id="e1", monitor_id="m-daily", ts_ms=now, run_id="q1",
                                                 metric_value=0.0, expected_low=1.0, expected_high=None,
                                                 status="FAIL", score=None, message="0 rows", details=None))
    r = c.get("/v1/observability/pipelines/airflow:fin_etl/runs/manual__2", headers=H).json()
    assert r["quality"] == [{"table": DAILY, "monitor": "m-daily", "name": "daily not empty", "type": "volume",
                             "status": "FAIL", "message": "0 rows", "at": r["quality"][0]["at"]}]
    assert {x["id"] for x in r["impact"]["data_products"]} == {"finance"}
    assert {t["urn"] for t in r["impact"]["tables"]} == {DAILY, REPORT}


def test_exporters_api_scopes_and_apply(world):
    c, s, tmp = world
    assert c.post("/v1/secret-scopes", headers=H, json={"name": "otel"}).status_code in (200, 201)
    assert c.put("/v1/secret-scopes/otel/secrets/token", headers=H, json={"value": "tok-" + "x" * 20}).status_code \
        in (200, 201, 204)
    body = {"id": "customer", "protocol": "http", "endpoint": "https://otlp.customer.example",
            "headers": [{"name": "authorization", "scope": "otel", "key": "token", "prefix": "Bearer "}]}
    # An operator who cannot read the scope may not point an exporter at its secret.
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"],
               condition={"attr": "subject.urn", "op": "in", "value": [ADMIN, "urn:li:corpuser:ops2"]})]))
    r = c.post("/v1/observability/exporters", headers={"X-Cairn-Actor": "urn:li:corpuser:ops2"}, json=body)
    assert r.status_code == 403 and "scope" in r.text
    assert c.post("/v1/observability/exporters", headers=H, json=body).status_code == 201
    cfg = c.get("/v1/observability/exporters/collector-config", headers=H).json()
    assert cfg["env"] == ["CAIRN_EXPORTER_CUSTOMER_0"] and "tok-" not in json.dumps(cfg)
    assert "traces/export-customer" in cfg["pipelines"]
    out = c.post("/v1/observability/exporters/apply", headers=H).json()
    assert out["written"] == ["exporters.yaml", "exporters.env"]
    d = tmp / "exporters"
    assert (d / "exporters.env").read_text() == "CAIRN_EXPORTER_CUSTOMER_0=tok-" + "x" * 20 + "\n"
    assert "tok-" not in (d / "exporters.yaml").read_text()
    from sqlalchemy import select

    from cairn.common.audit import audit_log

    with s.engine.connect() as conn:
        acts = [r.action for r in conn.execute(select(audit_log.c.action).where(audit_log.c.category.in_(
            ("secrets", "secret_scopes", "observability", "identity"))))]
    assert "observability.exporters_applied" in acts and any("read" in a for a in acts), acts
    assert c.delete("/v1/observability/exporters/customer", headers=H).status_code == 204
