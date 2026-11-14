"""Pipeline observability and OpenTelemetry export end to end (L145), on real components: Airflow 3.3.2 (its standalone
scheduler, DAG processor, triggerer and API), the bundled stack's Collector, Tempo, Loki and Prometheus (with
deploy/observability/stack's configurations), a second Collector as the customer's own backend, and Cairn's API with
its AI gateway in front of the deterministic stand-in model (tests/live_model_standin.py):

* **Airflow's own telemetry**, set up by ``airflow_otel_env``: a DAG run is a trace in Tempo (``dag_run.<dag>``) with a
  ``worker.<task>`` span per task; Airflow's metrics (scheduler heartbeat, DAG processing, task durations, pool slots)
  are in Prometheus; nothing secret (a connection's password, a token, a run's conf value) in any span;
* **Cairn's view**: Airflow's runs read through its API, each with its tasks, Airflow's own trace found, Cairn's run
  trace (service cairn-pipelines, a span per task) in Tempo, ``cairn_pipeline_runs_total`` in Prometheus, the failed
  task's log through Airflow's API with its password redacted;
* **an SLA** on the DAG fires into a real webhook receiver and opens an incident;
* **the SRE agent** joins: reads the incident and the run's diagnosis (the task's log: out of memory), writes a
  finding citing it, ranks the causes and proposes a rerun; the operator approves; Airflow reruns the failed task, it
  succeeds, Cairn reads it and the SLA's alert resolves;
* **OTLP export**: the destination is configured in Cairn (its bearer token in a secret scope), rendered and applied
  for the Collector; the customer's Collector, which accepts only that token, receives traces (Airflow's and Cairn's),
  metrics (Airflow's and cairn_pipeline_runs_total) and logs, none with a secret in them.

Telemetry is global to a process: CI runs this file in a pytest of its own (job trino). Needs docker and the images
below (the stack's, about 1.3 GB, and Airflow's slim image)."""

from __future__ import annotations

import base64
import glob
import http.server
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import httpx
import pytest

pytest.importorskip("opentelemetry.sdk")
sys.path.insert(0, os.path.dirname(__file__))
from live_model_standin import handler as standin_handler  # noqa: E402

AIRFLOW = os.environ.get("CAIRN_AIRFLOW_SLIM_IMAGE", "apache/airflow:slim-3.3.2-python3.12")
IMAGES = {"collector": "otel/opentelemetry-collector-contrib:0.163.2", "prometheus": "prom/prometheus:v3.15.1",
          "loki": "grafana/loki:3.6.18", "tempo": "grafana/tempo:2.9.5"}
STACK = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "deploy", "observability", "stack"))
ADMIN = "urn:li:corpuser:admin"
OPS = "urn:li:corpuser:ops"
MODEL = "sre-standin"
PROVIDER_KEY = "standin-" + uuid.uuid4().hex
WAREHOUSE_PASSWORD = "Wh-" + uuid.uuid4().hex[:12]       # in the connection Airflow's task raises with
TOKEN = "tok" + uuid.uuid4().hex                          # a token the task's error carries
CONF_SECRET = "conf-" + uuid.uuid4().hex                  # a value in a run's conf
CUSTOMER_TOKEN = "cust-" + uuid.uuid4().hex               # what the customer's Collector accepts (a secret scope's)
SECRETS = (WAREHOUSE_PASSWORD, TOKEN, CONF_SECRET, CUSTOMER_TOKEN)

DAGS = '''
import os
from datetime import datetime
from airflow.sdk import DAG, task

common = dict(start_date=datetime(2026, 1, 1), schedule=None, catchup=False, is_paused_upon_creation=False)

with DAG("etl_ok", **common):
    @task
    def extract():
        return [1, 2, 3]

    @task
    def transform(rows):
        return [r * 2 for r in rows]

    @task
    def load(rows):
        print(f"loaded {len(rows)} rows")

    load(transform(extract()))

with DAG("etl_flaky", **common):
    @task
    def stage():
        return 1

    @task
    def publish(x):
        if os.path.exists("/work/fail-flag"):
            raise MemoryError("out of memory while writing to " + os.environ["AIRFLOW_CONN_WAREHOUSE"]
                              + " token=" + os.environ["WAREHOUSE_TOKEN"])
        print("published")

    publish(stage())
'''


def _ready() -> bool:
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode:
        return False
    return all(subprocess.run(["docker", "image", "inspect", i], capture_output=True, timeout=30).returncode == 0
               for i in [*IMAGES.values(), AIRFLOW])


pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, " + ", ".join([*IMAGES.values(), AIRFLOW]))


def port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def host_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]


def until(fn, timeout=120, every=2.0, what="condition"):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            got = fn()
            if got:
                return got
        except Exception as e:  # noqa: BLE001 - keep trying until the deadline
            last = e
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {what}" + (f" (last error: {last!r})" if last else ""))


def docker(*args, check=True, timeout=300):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=check)


def private_file(path, text: str, mode: int = 0o600) -> str:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(path, mode)
    return str(path)


class Receiver(http.server.BaseHTTPRequestHandler):
    got: list = []

    def do_POST(self):  # noqa: N802
        Receiver.got.append(json.loads(self.rfile.read(int(self.headers.get("content-length", 0)))))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


CUSTOMER = """
extensions:
  bearertokenauth/in: {token: "${env:EXPECT_TOKEN}"}
receivers:
  otlp:
    protocols:
      http: {endpoint: "0.0.0.0:4318", auth: {authenticator: bearertokenauth/in}}
exporters:
  file/traces: {path: /out/traces.json}
  file/metrics: {path: /out/metrics.json}
  file/logs: {path: /out/logs.json}
service:
  extensions: [bearertokenauth/in]
  telemetry: {logs: {level: warn}}
  pipelines:
    traces: {receivers: [otlp], exporters: [file/traces]}
    metrics: {receivers: [otlp], exporters: [file/metrics]}
    logs: {receivers: [otlp], exporters: [file/logs]}
"""


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """Everything, in the order it needs: the backends and the customer's Collector; Cairn (its exporter configured
    and applied); the bundled Collector with Cairn's exporters; Airflow sending to it; the receiver and the model."""
    import uvicorn

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.observability import airflow_otel_env, telemetry
    from cairn.services import build_services

    tag = f"cairn-pobs-{os.getpid()}"
    work = tmp_path_factory.mktemp("pobs")
    d = tempfile.mkdtemp(prefix="pobs-")
    for sub in ("out", "exporters", "dags", "cfg"):
        os.makedirs(f"{d}/{sub}")
        os.chmod(f"{d}/{sub}", 0o777)
    os.chmod(d, 0o755)
    with open(f"{d}/cfg/prometheus.yml", "w") as fh:
        fh.write("global: {scrape_interval: 2s, evaluation_interval: 5s}\nscrape_configs:\n  - job_name: otel-collector\n"
                 "    static_configs: [{targets: ['cairn-otel-collector:8889']}]\n")
    with open(f"{d}/cfg/customer.yaml", "w") as fh:
        fh.write(CUSTOMER)
    for f in ("prometheus.yml", "customer.yaml"):
        os.chmod(f"{d}/cfg/{f}", 0o644)
    customer_env = private_file(f"{work}/customer.env", f"EXPECT_TOKEN={CUSTOMER_TOKEN}\n")
    ports = {k: port() for k in ("otlp_http", "otlp_grpc", "prometheus", "loki", "tempo", "airflow", "cairn",
                                 "customer")}
    names = {n: f"{tag}-{n}" for n in ("tempo", "loki", "prometheus", "customer", "collector", "airflow")}
    docker("rm", "-f", *names.values(), check=False)
    docker("network", "rm", tag, check=False)
    docker("network", "create", tag)
    srv = model = server = None
    stop = threading.Event()
    try:
        docker("run", "-d", "--name", names["tempo"], "--network", tag, "--network-alias", "cairn-tempo",
               "-p", f"127.0.0.1:{ports['tempo']}:3200", "-v", f"{STACK}/tempo.yaml:/etc/tempo.yaml:ro",
               IMAGES["tempo"], "-config.file=/etc/tempo.yaml")
        docker("run", "-d", "--name", names["loki"], "--network", tag, "--network-alias", "cairn-loki",
               "-p", f"127.0.0.1:{ports['loki']}:3100", "-v", f"{STACK}/loki.yaml:/etc/loki.yaml:ro",
               IMAGES["loki"], "-config.file=/etc/loki.yaml")
        docker("run", "-d", "--name", names["prometheus"], "--network", tag, "-p", f"127.0.0.1:{ports['prometheus']}:9090",
               "-v", f"{d}/cfg/prometheus.yml:/etc/prometheus/prometheus.yml:ro", IMAGES["prometheus"],
               "--config.file=/etc/prometheus/prometheus.yml")
        docker("run", "-d", "--name", names["customer"], "--network", tag, "--network-alias", "customer-collector",
               "--env-file", customer_env, "-p", f"127.0.0.1:{ports['customer']}:4318",
               "-v", f"{d}/cfg/customer.yaml:/etc/customer.yaml:ro", "-v", f"{d}/out:/out",
               IMAGES["collector"], "--config=/etc/customer.yaml")

        # Cairn, its telemetry to the bundled Collector's published port (up shortly).
        ip = host_ip()
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = os.environ.get("NO_PROXY", "") + f",{ip},127.0.0.1"
        rx = http.server.ThreadingHTTPServer((ip, 0), Receiver)
        threading.Thread(target=rx.serve_forever, daemon=True).start()
        srv = rx
        key_file = private_file(f"{work}/provider.key", PROVIDER_KEY)
        model = http.server.ThreadingHTTPServer(("127.0.0.1", 0), standin_handler(PROVIDER_KEY))
        threading.Thread(target=model.serve_forever, daemon=True).start()
        airflow_password = secrets.token_urlsafe(18)
        af_pw_file = private_file(f"{work}/airflow-password", airflow_password)
        env = {"OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{ports['otlp_http']}", "OTEL_TRACES_SAMPLER": "always_on",
               "OTEL_BSP_SCHEDULE_DELAY": "300", "OTEL_BLRP_SCHEDULE_DELAY": "300", "OTEL_METRIC_EXPORT_INTERVAL": "2000",
               "CAIRN_OBS_PROMETHEUS_URL": f"http://127.0.0.1:{ports['prometheus']}",
               "CAIRN_OBS_LOKI_URL": f"http://127.0.0.1:{ports['loki']}",
               "CAIRN_OBS_TEMPO_URL": f"http://127.0.0.1:{ports['tempo']}",
               "CAIRN_OBS_EXPORTERS_DIR": f"{d}/exporters", "CAIRN_OBS_PIPELINES_SYNC_SECONDS": "3600",
               "CAIRN_SECRETS_KEY": base64.b64encode(os.urandom(32)).decode(),
               "CAIRN_AIRFLOW_URL": f"http://127.0.0.1:{ports['airflow']}", "CAIRN_AIRFLOW_API": "v2",
               "CAIRN_AIRFLOW_USERNAME": "cairn", "CAIRN_AIRFLOW_PASSWORD_FILE": af_pw_file,
               "CAIRN_AI_GATEWAY_ANTHROPIC_URL": f"http://127.0.0.1:{model.server_address[1]}",
               "CAIRN_AI_GATEWAY_ANTHROPIC_KEY_FILE": key_file, "CAIRN_AI_GATEWAY_MODELS": MODEL,
               "CAIRN_SRE_AGENT_MODEL": MODEL}
        for k in ("ANTHROPIC_API_KEY", "CAIRN_AI_GATEWAY_ANTHROPIC_KEY", "CAIRN_AI_GATEWAY_UPSTREAMS"):
            os.environ.pop(k, None)
        os.environ.update(env)
        import logging

        logging.getLogger("cairn").setLevel(logging.INFO)
        s = build_services(Settings(database_url=f"sqlite:///{work}/cairn.db", service_token="svc-" + uuid.uuid4().hex,
                                    dev_trust_actor_header=True, gateway={"key_dir": str(work / "keys")}),
                           graph=InMemoryGraph(), with_ai=False, with_gateway=False)
        assert s.pipeline_obs is not None and s.sre is not None and s.ai_gateway is not None
        me = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
        s.pdp.set_bundle(PolicyBundle(policies=[
            Policy(id="admin", effect="allow", actions=["*"], condition=me(ADMIN)),
            Policy(id="ops", effect="allow", actions=["MANAGE_SYSTEM_OPERATIONS", "USE_AI_GATEWAY", "VIEW_OPERATIONS"],
                   condition=me(OPS))]))
        app = create_api_app(s)
        assert telemetry.enabled()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=ports["cairn"], log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        url = f"http://127.0.0.1:{ports['cairn']}"
        until(lambda: socket.create_connection(("127.0.0.1", ports["cairn"]), 0.3).close() or True, 30, 0.1, "Cairn")

        # The customer's backend, configured in Cairn: its token in a secret scope; rendered and applied.
        H = {"X-Cairn-Actor": ADMIN}
        assert httpx.post(f"{url}/v1/secret-scopes", headers=H, json={"name": "otel"}).status_code in (200, 201)
        assert httpx.put(f"{url}/v1/secret-scopes/otel/secrets/customer", headers=H,
                         json={"value": CUSTOMER_TOKEN}).status_code in (200, 201, 204)
        r = httpx.post(f"{url}/v1/observability/exporters", headers=H, json={
            "id": "customer", "name": "The customer's Collector", "protocol": "http",
            "endpoint": "http://customer-collector:4318", "tls": {"insecure": True},
            "headers": [{"name": "authorization", "scope": "otel", "key": "customer", "prefix": "Bearer "}]})
        assert r.status_code == 201, r.text
        r = httpx.post(f"{url}/v1/observability/exporters/apply", headers=H, timeout=30)
        assert r.status_code == 200, r.text
        assert CUSTOMER_TOKEN not in open(f"{d}/exporters/exporters.yaml").read()
        os.chmod(f"{d}/exporters/exporters.yaml", 0o644)
        docker("run", "-d", "--name", names["collector"], "--network", tag, "--network-alias", "cairn-otel-collector",
               "--env-file", f"{d}/exporters/exporters.env",
               "-p", f"127.0.0.1:{ports['otlp_http']}:4318", "-p", f"127.0.0.1:{ports['otlp_grpc']}:4317",
               "-v", f"{STACK}/otel-collector.yaml:/etc/otelcol/config.yaml:ro",
               "-v", f"{d}/exporters/exporters.yaml:/etc/otelcol/exporters.yaml:ro", IMAGES["collector"],
               "--config=/etc/otelcol/config.yaml", "--config=/etc/otelcol/exporters.yaml")
        for what, u in (("Tempo", f"http://127.0.0.1:{ports['tempo']}/ready"),
                        ("Loki", f"http://127.0.0.1:{ports['loki']}/ready"),
                        ("Prometheus", f"http://127.0.0.1:{ports['prometheus']}/-/ready")):
            until(lambda u=u: httpx.get(u, timeout=3).status_code == 200, 180, 2, what)

        # Airflow, its OpenTelemetry exactly as Cairn's helper sets it, to the bundled Collector.
        with open(f"{d}/dags/etl.py", "w") as fh:
            fh.write(DAGS)
        os.chmod(f"{d}/dags/etl.py", 0o644)
        (work / "af").mkdir()
        os.chmod(work / "af", 0o777)
        # Airflow's simple auth manager opens it for writing (as its own user): readable to it, in a test directory.
        private_file(work / "af" / "passwords.json", json.dumps({"cairn": airflow_password}), 0o666)
        open(work / "af" / "fail-flag", "w").close()
        aenv = {**airflow_otel_env({"CAIRN_OBS_AIRFLOW_OTLP_ENDPOINT": f"http://127.0.0.1:{ports['otlp_http']}"}),
                "OTEL_METRIC_EXPORT_INTERVAL": "5000",
                "AIRFLOW_CONN_WAREHOUSE": f"postgresql://etl:{WAREHOUSE_PASSWORD}@warehouse.internal:5432/wh",
                "WAREHOUSE_TOKEN": TOKEN,
                "AIRFLOW__CORE__AUTH_MANAGER": "airflow.api_fastapi.auth.managers.simple.simple_auth_manager."
                                               "SimpleAuthManager",
                "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS": "cairn:admin",
                "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE": "/work/passwords.json",
                "AIRFLOW__API__PORT": str(ports["airflow"]), "AIRFLOW__API__BASE_URL": f"http://127.0.0.1:{ports['airflow']}",
                "AIRFLOW__CORE__EXECUTION_API_SERVER_URL": f"http://127.0.0.1:{ports['airflow']}/execution/",
                "AIRFLOW__API_AUTH__JWT_SECRET": secrets.token_hex(32), "AIRFLOW__CORE__LOAD_EXAMPLES": "false",
                "AIRFLOW__CORE__DAGS_FOLDER": "/opt/airflow/dags", "AIRFLOW__DAG_PROCESSOR__REFRESH_INTERVAL": "5",
                "AIRFLOW__DAG_PROCESSOR__MIN_FILE_PROCESS_INTERVAL": "5"}
        af_env = private_file(work / "airflow.env", "".join(f"{k}={v}\n" for k, v in aenv.items()))
        docker("run", "-d", "--name", names["airflow"], "--network", "host", "--env-file", af_env,
               "-v", f"{work / 'af'}:/work", "-v", f"{d}/dags:/opt/airflow/dags:ro", AIRFLOW, "bash", "-c",
               "airflow standalone", timeout=120)
        base = f"http://127.0.0.1:{ports['airflow']}"

        def healthy():
            h = httpx.get(f"{base}/api/v2/monitor/health", timeout=5, trust_env=False).json()
            return all((h.get(k) or {}).get("status") == "healthy" for k in ("scheduler", "dag_processor", "metadatabase"))
        until(healthy, 300, 3, "Airflow")
        from cairn.flows.steps import Airflow

        client = Airflow({"url": base, "api": "v2", "username": "cairn", "password_file": af_pw_file})

        def tick():
            while not stop.wait(2):
                try:
                    s.notify.dispatch_pending()
                except Exception:  # noqa: BLE001 - shown by the assertions
                    pass
        threading.Thread(target=tick, daemon=True).start()
        yield {"url": url, "s": s, "ports": ports, "airflow": client, "af_dir": work / "af", "out": f"{d}/out",
               "names": names, "receiver": f"http://{ip}:{rx.server_address[1]}/hook"}
    finally:
        stop.set()
        if os.environ.get("CAIRN_PIPELINE_E2E_LOGS"):
            for n in names.values():
                print(f"--- {n}\n" + docker("logs", "--tail", "80", n, check=False).stderr[-5000:])
        if server is not None:
            server.should_exit = True
        for x in (srv, model):
            if x is not None:
                x.shutdown()
        docker("rm", "-f", *names.values(), check=False)
        docker("network", "rm", tag, check=False)
        shutil.rmtree(d, ignore_errors=True)


def api(w, method, path, who=ADMIN, **kw):
    return httpx.request(method, w["url"] + path, headers={"X-Cairn-Actor": who}, timeout=120, **kw)


def wait_dag(w, dag_id):
    return until(lambda: w["airflow"].call("GET", f"/dags/{dag_id}"), 180, 2, f"Airflow to load {dag_id}")


def trigger(w, dag_id, conf=None) -> str:
    wait_dag(w, dag_id)
    rid = f"t-{dag_id}-{uuid.uuid4().hex[:8]}"
    w["airflow"].call("POST", f"/dags/{dag_id}/dagRuns", {"dag_run_id": rid, "logical_date": None, "conf": conf or {}})
    return rid


def run_state(w, dag_id, rid) -> str:
    return w["airflow"].call("GET", f"/dags/{dag_id}/dagRuns/{rid}").get("state")


@pytest.fixture(scope="module")
def runs(world):
    """One good run and one failing run (out of memory, its error carrying a password and a token)."""
    ok = trigger(world, "etl_ok", {"api_token": CONF_SECRET})
    bad = trigger(world, "etl_flaky", {"api_token": CONF_SECRET})
    until(lambda: run_state(world, "etl_ok", ok) == "success", 300, 3, "etl_ok to succeed")
    until(lambda: run_state(world, "etl_flaky", bad) == "failed", 300, 3, "etl_flaky to fail")
    return {"ok": ok, "bad": bad}


def tempo_search(w, tags: dict) -> list[dict]:
    q = " ".join(f"{k}={v}" for k, v in tags.items())
    r = httpx.get(f"http://127.0.0.1:{w['ports']['tempo']}/api/search", params={"tags": q, "limit": "20",
                                                                              "start": str(int(time.time()) - 3600),
                                                                              "end": str(int(time.time()) + 60)},
                  timeout=10)
    return r.json().get("traces") or []


def tempo_trace(w, tid: str) -> dict:
    return httpx.get(f"http://127.0.0.1:{w['ports']['tempo']}/api/traces/{tid}", timeout=10).json()


def span_names(doc: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for b in doc.get("batches") or doc.get("resourceSpans") or []:
        svc = next((a["value"].get("stringValue") for a in b["resource"]["attributes"] if a["key"] == "service.name"), "")
        for ss in b.get("scopeSpans") or b.get("instrumentationLibrarySpans") or []:
            for sp in ss["spans"]:
                out.setdefault(svc, []).append(sp)
    return out


def prom(w, expr: str) -> list[dict]:
    r = httpx.get(f"http://127.0.0.1:{w['ports']['prometheus']}/api/v1/query", params={"query": expr}, timeout=10)
    return r.json()["data"]["result"]


def test_a_dag_run_is_a_trace_with_its_task_spans(world, runs):
    def found():
        ts = tempo_search(world, {"airflow.dag_run.run_id": runs["ok"]})
        for t in ts:
            spans = span_names(tempo_trace(world, t["traceID"]))
            names = {s["name"] for v in spans.values() for s in v}
            if {"dag_run.etl_ok", "worker.extract", "worker.transform", "worker.load"} <= names:
                return t["traceID"], spans
        return None
    tid, spans = until(found, 240, 4, "Airflow's trace of the DAG run in Tempo")
    allspans = [s for v in spans.values() for s in v]
    root = next(s for s in allspans if s["name"] == "dag_run.etl_ok")
    assert not root.get("parentSpanId") and "airflow" in spans
    by_id = {s["spanId"]: s for s in allspans}

    def ancestors(sp):
        out = []
        while sp.get("parentSpanId") and sp["parentSpanId"] in by_id and len(out) < 20:
            sp = by_id[sp["parentSpanId"]]
            out.append(sp["name"])
        return out
    for t in ("extract", "transform", "load"):
        sp = next(s for s in allspans if s["name"] == f"worker.{t}")
        assert ancestors(sp)[-1:] == ["dag_run.etl_ok"], (t, ancestors(sp), [(s["name"], s.get("parentSpanId"))
                                                                              for s in allspans])
    assert len(tid) >= 16


def test_airflows_metrics_are_in_prometheus(world, runs):
    def names():
        r = httpx.get(f"http://127.0.0.1:{world['ports']['prometheus']}/api/v1/label/__name__/values", timeout=10)
        got = [n for n in r.json()["data"] if n.startswith("airflow")]
        want = {"heartbeat": [n for n in got if "scheduler_heartbeat" in n],
                "dag processing": [n for n in got if "dag_processing" in n],
                "task duration": [n for n in got if "duration" in n and ("task" in n or "ti" in n or "dag_" in n)],
                "pool slots": [n for n in got if n.startswith("airflow_pool") and "slots" in n]}
        return want if all(want.values()) else None
    got = until(names, 240, 5, "Airflow's scheduler, DAG processing, task duration and pool metrics in Prometheus")
    heartbeat = got["heartbeat"][0]
    assert prom(world, f"sum({heartbeat})")[0]["value"][1] not in ("0", "NaN")


def test_no_secret_in_airflows_spans(world, runs):
    def failed_trace():
        for t in tempo_search(world, {"airflow.dag_run.run_id": runs["bad"]}):
            doc = tempo_trace(world, t["traceID"])
            names = {s["name"] for v in span_names(doc).values() for s in v}
            if "worker.publish" in names and "dag_run.etl_flaky" in names:
                return doc
        return None
    doc = until(failed_trace, 240, 4, "the failed run's trace")
    raw = json.dumps(doc)
    spans = {s["name"]: s for v in span_names(doc).values() for s in v}
    assert spans["task_run.publish"]["status"]["code"] == "STATUS_CODE_ERROR"      # the failure is in the trace
    assert spans["dag_run.etl_flaky"]["status"]["code"] == "STATUS_CODE_ERROR"
    for secret in SECRETS:
        assert secret not in raw, f"{secret[:6]}... is in Airflow's spans"


def test_the_collector_redacts_what_others_send(world):
    """Airflow 3.3 keeps a task's exception out of its spans; other senders (a deeper span detail level, any SDK)
    record it as a span event. Sent raw to the Collector, as such a process would: the password, token and key=value
    secret in the event, the status and a log line are gone in Tempo and in Loki."""
    now = time.time_ns()
    tid, sid = uuid.uuid4().hex, uuid.uuid4().hex[:16]
    msg = f"could not connect to postgresql://etl:{WAREHOUSE_PASSWORD}@db:5432/wh token={TOKEN}"
    attr = lambda k, v: {"key": k, "value": {"stringValue": v}}  # noqa: E731
    res = {"attributes": [attr("service.name", "raw-sender")]}
    trace = {"resourceSpans": [{"resource": res, "scopeSpans": [{"scope": {"name": "t"}, "spans": [{
        "traceId": tid, "spanId": sid, "name": "worker.raw", "kind": 1, "startTimeUnixNano": str(now - 10**9),
        "endTimeUnixNano": str(now), "status": {"code": 2, "message": msg},
        "events": [{"name": "exception", "timeUnixNano": str(now),
                    "attributes": [attr("exception.type", "OperationalError"), attr("exception.message", msg),
                                   attr("exception.stacktrace", "Traceback ...\n" + msg)]}]},
        {"traceId": tid, "spanId": uuid.uuid4().hex[:16], "parentSpanId": sid, "name": "POST", "kind": 3,   # no events
         "startTimeUnixNano": str(now - 10**8), "endTimeUnixNano": str(now - 10**7),
         "attributes": [attr("url.full", f"http://cairn:8800/v1/trino/{TOKEN}/allow?x=1")]}]}]}]}
    logs = {"resourceLogs": [{"resource": res, "scopeLogs": [{"scope": {"name": "t"}, "logRecords": [{
        "timeUnixNano": str(now), "severityText": "ERROR", "body": {"stringValue": "raw-sender failed: " + msg},
        "traceId": tid, "spanId": sid}]}]}]}
    base = f"http://127.0.0.1:{world['ports']['otlp_http']}"
    for path, doc in (("/v1/traces", trace), ("/v1/logs", logs)):
        r = httpx.post(base + path, json=doc, timeout=10, trust_env=False)
        assert r.status_code == 200, r.text
    got = until(lambda: (lambda r: r.json() if r.status_code == 200 else None)(
        httpx.get(f"http://127.0.0.1:{world['ports']['tempo']}/api/traces/{tid}", timeout=5)), 90, 2, "the raw trace")
    raw = json.dumps(got)
    assert "OperationalError" in raw and "[redacted]" in raw
    assert WAREHOUSE_PASSWORD not in raw and TOKEN not in raw, raw

    def line():
        r = httpx.get(f"http://127.0.0.1:{world['ports']['loki']}/loki/api/v1/query_range", timeout=5, params={
            "query": '{service_name="raw-sender"}', "start": str(now - 600 * 10**9), "end": str(time.time_ns())})
        vals = [v[1] for s in r.json()["data"]["result"] for v in s["values"]]
        return vals or None
    lines = until(line, 90, 2, "the raw log line in Loki")
    assert "raw-sender failed" in lines[0] and WAREHOUSE_PASSWORD not in lines[0] and TOKEN not in lines[0]


def test_cairn_reads_airflow_and_emits_each_run(world, runs):
    out = api(world, "POST", "/v1/observability/pipelines/sync").json()
    assert out["airflows"] == 1 and out["runs"] >= 2 and not out["errors"], out
    items = {p["key"]: p for p in api(world, "GET", "/v1/observability/pipelines").json()["items"]}
    assert items["airflow:etl_ok"]["health"] == "healthy" and items["airflow:etl_flaky"]["health"] == "failing"
    run = api(world, "GET", f"/v1/observability/pipelines/airflow:etl_ok/runs/{runs['ok']}").json()
    assert run["state"] == "success" and [t["task"] for t in run["tasks"]] == ["extract", "transform", "load"]
    assert all(t["state"] == "success" and t["duration_ms"] is not None for t in run["tasks"])
    assert run["airflow_trace_id"], "Airflow's own trace of the run was not found in Tempo"
    from cairn.observability import pipelines, telemetry

    pipelines.flush()
    telemetry.flush()

    def journey():
        j = api(world, "GET", f"/v1/observability/journey/{run['trace_id']}")
        return j.json() if j.status_code == 200 and j.json().get("spans") else None
    j = until(journey, 120, 3, "Cairn's trace of the run")
    assert j["services"] == ["cairn-pipelines"]
    assert {s["name"] for s in j["spans"]} == {"pipeline airflow:etl_ok", "task extract", "task transform", "task load"}
    bad = api(world, "GET", f"/v1/observability/pipelines/airflow:etl_flaky/runs/{runs['bad']}").json()
    assert bad["state"] == "failed" and {t["task"]: t["state"] for t in bad["tasks"]} == {"stage": "success",
                                                                                          "publish": "failed"}
    until(lambda: prom(world, 'cairn_pipeline_runs_total{cairn_pipeline="airflow:etl_ok",cairn_pipeline_status="success"}'),
          120, 3, "cairn_pipeline_runs_total in Prometheus")
    # The failed task's log, through Airflow's API: the error is there, its password and token are not.
    log = api(world, "GET", f"/v1/observability/pipelines/airflow:etl_flaky/runs/{runs['bad']}/tasks/publish/log").json()
    text = "\n".join(log["lines"])
    assert log["source"] == "airflow" and "out of memory" in text
    assert WAREHOUSE_PASSWORD not in text and TOKEN not in text


def test_an_sla_fires_and_the_sre_agent_finds_why_and_proposes_a_rerun(world, runs):
    assert api(world, "PUT", "/notify/routes/pipes", json={"id": "pipes", "channel": "webhook",
                                                           "target": world["receiver"],
                                                           "event_types": ["job.failed"]}).status_code in (200, 201)
    r = api(world, "POST", "/v1/observability/pipeline-slas", json={
        "id": "flaky-ok", "name": "Flaky publishes", "pipeline": "airflow:etl_flaky", "severity": "critical",
        "routes": ["pipes"]})
    assert r.status_code == 201, r.text
    api(world, "POST", "/v1/observability/evaluate")
    alerts = [a for a in api(world, "GET", "/v1/observability/alerts", params={"state": "firing"}).json()["items"]
              if a["source"] == "sla:flaky-ok"]
    [alert] = alerts
    assert alert["labels"]["condition"] == "failure" and alert["labels"]["run"] == runs["bad"] and alert["incident_id"]
    got = until(lambda: [m for m in Receiver.got if m["type"] == "observability.alert_firing"], 60, 1, "the webhook")
    assert "Flaky publishes" in got[0]["title"]
    iid = alert["incident_id"]
    # The agent, asked by the on-call person.
    r = api(world, "POST", f"/v1/sre/incidents/{iid}/investigate", who=OPS, params={"wait": "true"})
    assert r.status_code in (200, 202), r.text
    view = until(lambda: (lambda v: v if v["runs"] and v["runs"][0]["status"] in ("done", "failed") else None)(
        api(world, "GET", f"/v1/sre/incidents/{iid}", who=OPS).json()), 120, 2, "the agent's run")
    run = view["runs"][0]
    assert run["status"] == "done", run
    tools = [x["tool"] for x in run["steps"]]
    assert {"incident_context", "pipeline_diagnose", "write_note", "rank_causes", "propose_action"} <= set(tools), tools
    diag = json.loads(next(x for x in run["steps"] if x["tool"] == "pipeline_diagnose")["result"])
    assert diag["causes"][0]["kind"] == "resource" and "out of memory" in diag["causes"][0]["evidence"]["log"]
    inc = api(world, "GET", f"/v1/observability/incidents/{iid}", who=OPS).json()
    finding = next(e for e in inc["timeline"] if e.get("by_agent") and e["kind"] == "finding")
    assert "publish" in finding["text"] and finding["detail"]["evidence"][0]["tool"] == "pipeline_diagnose"
    [prop] = view["actions"]
    assert prop["action"] == "rerun_pipeline" and prop["status"] == "pending"
    assert prop["params"] == {"pipeline": "airflow:etl_flaky", "run_id": runs["bad"]}
    everything = json.dumps(inc) + json.dumps(view)
    for secret in SECRETS:
        assert secret not in everything
    # The cause is gone (memory raised); the operator approves; Airflow reruns the failed task as them.
    os.remove(world["af_dir"] / "fail-flag")
    r = api(world, "POST", f"/v1/sre/actions/{prop['id']}/approve", who=OPS)
    assert r.status_code == 200 and r.json()["status"] == "executed", r.text
    assert r.json()["decided_by"] == OPS and r.json()["result"]["cleared"] == ["publish"]
    until(lambda: run_state(world, "etl_flaky", runs["bad"]) == "success", 300, 3, "the rerun to succeed")
    api(world, "POST", "/v1/observability/pipelines/sync")
    rerun = api(world, "GET", f"/v1/observability/pipelines/airflow:etl_flaky/runs/{runs['bad']}").json()
    assert rerun["state"] == "success"
    assert next(t for t in rerun["tasks"] if t["task"] == "publish")["try"] >= 2
    api(world, "POST", "/v1/observability/evaluate")
    a = next(x for x in api(world, "GET", "/v1/observability/alerts").json()["items"] if x["id"] == alert["id"])
    assert a["state"] == "resolved"


def test_the_customers_backend_receives_redacted_traces_metrics_and_logs(world, runs):
    from cairn.observability import pipelines, telemetry

    pipelines.flush()
    telemetry.flush()

    def read(sig):
        p = os.path.join(world["out"], f"{sig}.json")
        return open(p).read() if os.path.exists(p) else ""

    def all_there():
        t, m, lg = read("traces"), read("metrics"), read("logs")
        ok = ("dag_run.etl_ok" in t and "worker.publish" in t and "cairn-pipelines" in t
              and "cairn_pipeline_runs_total" in m and "airflow" in m and "pipeline airflow:etl" in lg)
        return (t, m, lg) if ok else None
    t, m, lg = until(all_there, 180, 4, "Airflow's and Cairn's traces, metrics and logs at the customer's Collector")
    for secret in SECRETS:
        assert secret not in t + m + lg, f"{secret[:6]}... reached the customer's backend"
    # Without the token the customer's Collector refuses: what it holds came with the header Cairn configured.
    r = httpx.post(f"http://127.0.0.1:{world['ports']['customer']}/v1/traces", content=b"", timeout=5,
                   headers={"Content-Type": "application/x-protobuf"}, trust_env=False)
    assert r.status_code == 401, r.status_code
    assert CUSTOMER_TOKEN not in docker("logs", world["names"]["collector"], check=False).stderr
    assert sorted(os.path.basename(p) for p in glob.glob(os.path.join(world["out"], "*.json"))) == [
        "logs.json", "metrics.json", "traces.json"]
