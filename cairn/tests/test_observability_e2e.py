"""Observability end to end, on the bundled stack's real containers (OpenTelemetry Collector, Prometheus, Loki, Tempo,
with deploy/observability/stack's own configurations) and a real Trino that asks Cairn before every action:

* a query through Cairn's API (/v1/statements) runs on Trino; Cairn's spans, Trino's spans and the PDP decisions
  Trino asks Cairn for are one trace, read back through Cairn's journey API (spans from two processes);
* the request's log line is found by its trace id (Loki);
* the health map shows Cairn and Trino and the edge between them (span metrics and the service graph);
* an SLO on Cairn's error ratio burns, its multi-window alert fires, is delivered to a notification route (a real
  webhook receiver) and opens an incident whose timeline has the alert, a deploy and the changes around it;
* PromQL and LogQL alert rules fire; a silence holds one back;
* nothing exported (the raw spans in Tempo, the raw lines in Loki) carries a token, a key or a query's literal.

Telemetry is global to a process: CI runs this file in a pytest of its own (job trino). Needs docker and the
images below (about 1.3 GB)."""

import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid

import httpx
import pytest

pytest.importorskip("opentelemetry.sdk")

TRINO = os.environ.get("CAIRN_TRINO_IMAGE", "trinodb/trino:484")
IMAGES = {"collector": "otel/opentelemetry-collector-contrib:0.163.2", "prometheus": "prom/prometheus:v3.15.1",
          "loki": "grafana/loki:3.6.18", "tempo": "grafana/tempo:2.9.5"}
STACK = os.path.join(os.path.dirname(__file__), "..", "deploy", "observability", "stack")
KEY = "obs-e2e-trino-key-" + uuid.uuid4().hex          # Trino's key for Cairn: in the OPA URLs Trino calls
SERVICE_TOKEN = "svc-" + uuid.uuid4().hex
LITERAL = "SECRETNATION" + uuid.uuid4().hex[:6].upper()   # a query's literal: never in telemetry
ADMIN = "urn:li:corpuser:admin"
NATION = "urn:li:dataset:(urn:li:dataPlatform:tpch,tiny.nation,PROD)"
H = {"X-Cairn-Actor": ADMIN}


def _ready():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker")


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def host_ip() -> str:
    """This machine's address on its network: webhooks never reach loopback, so the receiver listens here."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]


def until(fn, timeout=120, every=1.0, what="condition"):
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
    raise AssertionError(f"timed out waiting for {what}" + (f" (last error: {last})" if last else ""))


def docker(*args, check=True, timeout=300):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=check)


@pytest.fixture(scope="module")
def stack():
    """The bundled stack on a network of its own, each backend published on a free local port."""
    tag = f"cairn-obs-{os.getpid()}"
    d = tempfile.mkdtemp(prefix="obs-")
    with open(f"{d}/prometheus.yml", "w") as fh:
        fh.write("global: {scrape_interval: 2s, evaluation_interval: 5s}\n"
                 "scrape_configs:\n  - job_name: otel-collector\n"
                 "    static_configs: [{targets: ['cairn-otel-collector:8889']}]\n")
    os.chmod(d, 0o755)
    os.chmod(f"{d}/prometheus.yml", 0o644)
    ports = {"otlp_http": port(), "otlp_grpc": port(), "prometheus": port(), "loki": port(), "tempo": port()}
    names = [f"{tag}-{n}" for n in ("tempo", "loki", "collector", "prometheus")]
    docker("rm", "-f", *names, check=False)
    docker("network", "rm", tag, check=False)
    docker("network", "create", tag)
    try:
        docker("run", "-d", "--name", names[0], "--network", tag, "--network-alias", "cairn-tempo",
               "-p", f"127.0.0.1:{ports['tempo']}:3200", "-v", f"{os.path.abspath(STACK)}/tempo.yaml:/etc/tempo.yaml:ro",
               IMAGES["tempo"], "-config.file=/etc/tempo.yaml")
        docker("run", "-d", "--name", names[1], "--network", tag, "--network-alias", "cairn-loki",
               "-p", f"127.0.0.1:{ports['loki']}:3100", "-v", f"{os.path.abspath(STACK)}/loki.yaml:/etc/loki.yaml:ro",
               IMAGES["loki"], "-config.file=/etc/loki.yaml")
        docker("run", "-d", "--name", names[2], "--network", tag, "--network-alias", "cairn-otel-collector",
               "-p", f"127.0.0.1:{ports['otlp_http']}:4318", "-p", f"127.0.0.1:{ports['otlp_grpc']}:4317",
               "-v", f"{os.path.abspath(STACK)}/otel-collector.yaml:/etc/otelcol.yaml:ro",
               IMAGES["collector"], "--config=/etc/otelcol.yaml")
        docker("run", "-d", "--name", names[3], "--network", tag, "-p", f"127.0.0.1:{ports['prometheus']}:9090",
               "-v", f"{d}/prometheus.yml:/etc/prometheus/prometheus.yml:ro", IMAGES["prometheus"],
               "--config.file=/etc/prometheus/prometheus.yml")
        until(lambda: httpx.get(f"http://127.0.0.1:{ports['tempo']}/ready", timeout=3).status_code == 200,
              180, what="Tempo")
        until(lambda: httpx.get(f"http://127.0.0.1:{ports['loki']}/ready", timeout=3).status_code == 200,
              180, what="Loki")
        until(lambda: httpx.get(f"http://127.0.0.1:{ports['prometheus']}/-/ready", timeout=3).status_code == 200,
              120, what="Prometheus")
        yield ports
    finally:
        for n in names:
            if os.environ.get("CAIRN_OBS_E2E_LOGS"):
                print(f"--- {n}\n" + docker("logs", "--tail", "60", n, check=False).stderr[-4000:])
        docker("rm", "-f", *names, check=False)
        docker("network", "rm", tag, check=False)
        shutil.rmtree(d, ignore_errors=True)


class Receiver(http.server.BaseHTTPRequestHandler):
    got: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        Receiver.got.append(json.loads(body))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def receiver():
    ip = host_ip()
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = os.environ.get("NO_PROXY", "") + f",{ip}"
    srv = http.server.ThreadingHTTPServer((ip, 0), Receiver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://{ip}:{srv.server_address[1]}/hook"
    srv.shutdown()


@pytest.fixture(scope="module")
def cairn(stack, tmp_path_factory):
    """Cairn's API in this process, exporting its traces, metrics and logs to the Collector."""
    import uvicorn

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.observability import telemetry
    from cairn.services import build_services

    tmp = tmp_path_factory.mktemp("cairn")
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{stack['otlp_http']}", "OTEL_TRACES_SAMPLER": "always_on",
           "OTEL_BSP_SCHEDULE_DELAY": "300", "OTEL_BLRP_SCHEDULE_DELAY": "300", "OTEL_METRIC_EXPORT_INTERVAL": "2000",
           "CAIRN_OBS_PROMETHEUS_URL": f"http://127.0.0.1:{stack['prometheus']}",
           "CAIRN_OBS_LOKI_URL": f"http://127.0.0.1:{stack['loki']}",
           "CAIRN_OBS_TEMPO_URL": f"http://127.0.0.1:{stack['tempo']}",
           "CAIRN_TRINO_ACCESS_KEY": KEY,
           "CAIRN_TRINO_CATALOGS": json.dumps({"tpch": {"platform": "tpch", "database": ""}})}
    os.environ.update(env)
    import logging

    logging.getLogger("cairn").setLevel(logging.INFO)          # as `cairn api` logs (CAIRN_LOG_LEVEL=INFO)
    settings = Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token=SERVICE_TOKEN,
                        dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")})
    graph = InMemoryGraph()
    graph.aspects[(NATION, "schemaMetadata")] = {"fields": [{"fieldPath": f} for f in ("nationkey", "name", "regionkey",
                                                                                         "comment")]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    p = port()
    app = create_api_app(s)
    assert telemetry.enabled(), "OpenTelemetry did not start"
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=p, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    until(lambda: socket.create_connection(("127.0.0.1", p), 0.3).close() or True, 30, 0.1, "Cairn's API")
    yield f"http://127.0.0.1:{p}", s
    server.should_exit = True
    t.join(30)


@pytest.fixture(scope="module")
def trino(stack, cairn):
    """Trino on the host network: it asks Cairn before every action and sends its spans to the Collector."""
    url, s = cairn
    d = tempfile.mkdtemp(prefix="trino-obs-")
    os.makedirs(f"{d}/catalog")
    tp = port()
    open(f"{d}/catalog/tpch.properties", "w").write("connector.name=tpch\n")
    base = f"{url}/v1/trino/{KEY}"
    open(f"{d}/access-control.properties", "w").write("\n".join([
        "access-control.name=opa", f"opa.policy.uri={base}/allow", f"opa.policy.batched-uri={base}/batch",
        f"opa.policy.row-filters-uri={base}/row-filters", f"opa.policy.column-masking-uri={base}/column-mask", ""]))
    open(f"{d}/config.properties", "w").write("\n".join([
        "coordinator=true", "node-scheduler.include-coordinator=true", f"http-server.http.port={tp}",
        f"discovery.uri=http://127.0.0.1:{tp}", "internal-communication.shared-secret=" + "s" * 32,
        "http-server.authentication.type=JWT", f"http-server.authentication.jwt.key-file={url}/v1/trino/jwks.json",
        "http-server.authentication.jwt.required-issuer=cairn", "http-server.authentication.jwt.required-audience=trino",
        "http-server.authentication.allow-insecure-over-http=true",
        "tracing.enabled=true", f"tracing.exporter.endpoint=http://127.0.0.1:{stack['otlp_grpc']}", ""]))
    os.chmod(d, 0o755)
    for f in ("config.properties", "access-control.properties", "catalog/tpch.properties"):
        os.chmod(f"{d}/{f}", 0o644)
    name = f"cairn-obs-trino-{os.getpid()}"
    docker("rm", "-f", name, check=False)
    docker("run", "-d", "--rm", "--name", name, "--network", "host",
           "-v", f"{d}/catalog:/etc/trino/catalog:ro", "-v", f"{d}/config.properties:/etc/trino/config.properties:ro",
           "-v", f"{d}/access-control.properties:/etc/trino/access-control.properties:ro", TRINO, timeout=600)
    try:
        until(lambda: "SERVER STARTED" in docker("logs", name, check=False).stderr, 240, what="Trino")
        from cairn.quality.collectors import ConnectionRegistry

        s.data_access.registry = ConnectionRegistry({"tpch": {
            "url": f"trino://cairn@127.0.0.1:{tp}/tpch/tiny?protocol=http", "run_as_user": True,
            "catalog": {"platform": "tpch", "database": "", "dialect": "trino"}}})
        yield {"port": tp, "name": name}
    finally:
        if os.environ.get("CAIRN_OBS_E2E_LOGS"):
            print(docker("logs", "--tail", "80", name, check=False).stderr[-6000:])
        docker("rm", "-f", name, check=False)
        shutil.rmtree(d, ignore_errors=True)


def api(cairn, method, path, **kw):
    url, _ = cairn
    r = httpx.request(method, url + path, headers=H, timeout=60, **kw)
    return r


def query(cairn, sql):
    r = api(cairn, "POST", "/v1/statements", json={"sql": sql, "connection": "tpch"})
    assert r.status_code == 200, r.text
    tid = r.headers.get("x-trace-id")
    assert tid and len(tid) == 32
    page = r.json()
    while page.get("next"):
        page = api(cairn, "POST", "/v1/statements/next", json={"cursor": page["next"]}).json()
    return tid, page


@pytest.fixture(scope="module")
def journey(cairn, trino):
    """One query through Cairn's API: its trace id and the journey Cairn reads back."""
    from cairn.observability import telemetry

    tid, _ = query(cairn, f"SELECT count(*) AS n FROM tiny.nation WHERE name <> '{LITERAL}'")
    telemetry.flush()

    def read():
        r = api(cairn, "GET", f"/v1/observability/journey/{tid}")
        if r.status_code != 200:
            return None
        j = r.json()
        return j if {"cairn-api", "trino"} <= set(j["services"]) else None

    return tid, until(read, 120, 2, "the trace in Tempo with Cairn's and Trino's spans")


def test_one_trace_across_cairn_and_trino(journey):
    tid, j = journey
    assert j["trace_id"] == tid and j["source"] == "tempo"
    assert j["processes"] >= 2 and {"cairn-api", "trino"} <= set(j["services"])
    by_service = {}
    for s in j["spans"]:
        by_service.setdefault(s["service"], []).append(s)
    # Cairn's request span, its call to Trino, Trino's query, and the PDP decisions Trino asked Cairn for.
    assert any(s["kind"] == "server" and s["name"].startswith("POST /v1/statements") for s in by_service["cairn-api"])
    assert any(s["kind"] == "client" for s in by_service["cairn-api"])
    assert any(re.search(r"/v1/trino/\{\w+\}/", s["name"])
               for s in by_service["cairn-api"]), \
        f"Trino's access checks are not in the trace: {[(s['service'], s['kind'], s['name']) for s in j['spans']]}"
    ids = {s["span_id"] for s in j["spans"]}
    assert any(s["parent_id"] in ids for s in by_service["trino"]), "Trino's spans do not hang under Cairn's"


def test_logs_of_the_trace(cairn, journey):
    tid, _ = journey

    def read():
        r = api(cairn, "GET", "/v1/observability/logs", params={"trace_id": tid})
        assert r.status_code == 200, r.text
        return [e for e in r.json()["items"] if "statement" in e["line"]]

    lines = until(read, 90, 2, "the request's log line in Loki")
    assert lines[0]["trace_id"] == tid and lines[0]["service"] == "cairn-api"


def test_health_map(cairn, journey):
    def read():
        h = api(cairn, "GET", "/v1/observability/health", params={"window": "5m"}).json()
        names = {c["name"]: c for c in h["components"]}
        edges = {(e["from"], e["to"]) for e in h["edges"]}
        if {"cairn-api", "trino"} <= set(names) and ("cairn-api", "trino") in edges:
            return names, edges
        return None

    names, _ = until(read, 120, 3, "Cairn, Trino and the edge between them in the health map")
    assert names["cairn-api"]["rate"] > 0 and names["cairn-api"]["state"] in ("up", "degraded")
    assert names["trino"]["p95_ms"] is not None
    assert names["otel-collector"]["up"] is True


def test_slo_burns_alert_routes_and_incident_opens(cairn, trino, receiver, journey):
    from cairn.observability import telemetry

    url, s = cairn
    r = api(cairn, "PUT", "/notify/routes/oncall-webhook", json={"id": "oncall-webhook", "channel": "webhook",
                                                                  "target": receiver, "event_types": ["job.failed"]})
    assert r.status_code in (200, 201), r.text
    sel = 'service_name="cairn-api",span_kind="SPAN_KIND_SERVER"'
    slo = {"id": "api-errors", "name": "API answers", "service": "cairn-api", "objective": 99, "window": "1d",
           "indicator": {"type": "ratio",
                         "errors": f'sum(rate(traces_span_metrics_calls_total{{{sel},status_code="STATUS_CODE_ERROR"}}[$window]))',
                         "total": f"sum(rate(traces_span_metrics_calls_total{{{sel}}}[$window]))"},
           "burn_alerts": [{"long": "2m", "short": "1m", "factor": 2, "severity": "critical"}],
           "routes": ["oncall-webhook"]}
    r = api(cairn, "POST", "/v1/observability/slos", json=slo)
    assert r.status_code == 201, r.text
    assert api(cairn, "POST", "/v1/observability/deploys", json={"component": "cairn-api", "version": "9.9.9"}).status_code == 201
    r = api(cairn, "PUT", "/governance/policies/analysts-read", json={      # a change around the incident
        "id": "analysts-read", "effect": "allow", "actions": ["VIEW_ENTITY_PAGE"],
        "condition": {"attr": "subject.groups", "op": "contains", "value": "analysts"}})
    assert r.status_code == 200, r.text
    # Errors: queries on a connection whose Trino is not there answer 5xx.
    from cairn.quality.collectors import ConnectionRegistry

    defs = dict(s.data_access.registry.definitions())
    defs["gone"] = {"url": f"trino://cairn@127.0.0.1:{port()}/tpch/tiny?protocol=http", "run_as_user": True,
                    "catalog": {"platform": "tpch", "database": "", "dialect": "trino"}}
    s.data_access.registry = ConnectionRegistry(defs)
    failed = 0
    for _ in range(25):
        r = api(cairn, "POST", "/v1/statements", json={"sql": "SELECT * FROM tiny.nation", "connection": "gone"})
        failed += r.status_code >= 500
    assert failed >= 20, f"the broken connection did not fail with 5xx: {r.status_code} {r.text[:300]}"
    telemetry.flush()

    def fired():
        api(cairn, "POST", "/v1/observability/evaluate")
        return [a for a in api(cairn, "GET", "/v1/observability/alerts", params={"state": "firing"}).json()["items"]
                if a["source"].startswith("slo:api-errors:")]

    alerts = until(fired, 180, 5, "the SLO's burn-rate alert")
    a = alerts[0]
    assert a["severity"] == "critical" and a["labels"]["service"] == "cairn-api" and a["incident_id"]
    status = api(cairn, "GET", "/v1/observability/slos/api-errors/status").json()
    assert status["burn"][0]["burning"] and status["budget_remaining"] < 1
    s.notify.dispatch_pending()                      # what the notifier does every few seconds
    got = until(lambda: [m for m in Receiver.got if m["type"] == "observability.alert_firing"], 30, 1, "the webhook")
    assert "API answers" in got[0]["title"]
    assert any(m["type"] == "observability.incident_opened" for m in Receiver.got)
    inc = api(cairn, "GET", f"/v1/observability/incidents/{a['incident_id']}").json()
    assert inc["status"] == "open" and inc["services"] == ["cairn-api"]
    kinds = {e["kind"] for e in inc["timeline"]}
    assert {"alert_firing", "deploy", "change"} <= kinds, kinds          # the route's change, the deploy, the alert
    ctx = api(cairn, "GET", f"/v1/observability/incidents/{a['incident_id']}/context").json()
    assert ctx["incident"]["id"] == a["incident_id"] and isinstance(ctx["health"], list) and ctx["slos"][0]["id"] == "api-errors"
    # The agent's (or a person's) part: a finding, acknowledge, resolve.
    r = api(cairn, "POST", f"/v1/observability/incidents/{a['incident_id']}/notes",
            json={"kind": "finding", "text": "Connection gone points at a Trino that is not running"})
    assert r.status_code == 200
    assert api(cairn, "POST", f"/v1/observability/incidents/{a['incident_id']}/acknowledge").json()["status"] == "acknowledged"
    done = api(cairn, "POST", f"/v1/observability/incidents/{a['incident_id']}/resolve", json={"summary": "removed it"}).json()
    assert done["status"] == "resolved" and any(e["kind"] == "finding" for e in done["timeline"])


def test_promql_and_logql_rules_fire_and_a_silence_holds_one_back(cairn, journey):
    r = api(cairn, "POST", "/v1/observability/alert-rules", json={
        "id": "trino-busy", "name": "Trino is answering", "severity": "info",
        "expr": 'sum by (service_name) (rate(traces_span_metrics_calls_total{service_name="trino"}[5m])) > 0',
        "summary": "{{ $labels.service_name }} at {{ $value }}/s"})
    assert r.status_code == 201, r.text
    r = api(cairn, "POST", "/v1/observability/alert-rules", json={
        "id": "statements-logged", "name": "Statements logged", "language": "logql", "severity": "warning",
        "expr": 'sum(count_over_time({service_name="cairn-api"} |= "statement" [10m])) > 0'})
    assert r.status_code == 201, r.text
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    r = api(cairn, "POST", "/v1/observability/silences", json={
        "id": "quiet-statements", "matchers": [{"name": "alertname", "value": "Statements logged"}],
        "ends_at": end, "comment": "expected in this test"})
    assert r.status_code == 201, r.text
    test = api(cairn, "POST", "/v1/observability/alert-rules/test", json={
        "expr": 'sum(rate(traces_span_metrics_calls_total{service_name="trino"}[5m])) > 0'}).json()
    assert test["count"] == 1

    def both():
        api(cairn, "POST", "/v1/observability/evaluate")
        firing = {a["source"]: a for a in api(cairn, "GET", "/v1/observability/alerts",
                                              params={"state": "firing"}).json()["items"]}
        return firing if {"rule:trino-busy", "rule:statements-logged"} <= set(firing) else None

    firing = until(both, 120, 3, "both rules to fire")
    assert firing["rule:trino-busy"]["summary"].startswith("trino at ")
    assert firing["rule:statements-logged"]["silenced_by"] == "quiet-statements"
    assert firing["rule:trino-busy"]["silenced_by"] is None
    ack = api(cairn, "POST", f"/v1/observability/alerts/{firing['rule:trino-busy']['id']}/acknowledge").json()
    assert ack["acknowledged_by"] == ADMIN


def test_nothing_exported_carries_a_secret(cairn, stack, journey):
    """Straight from Tempo and Loki, not through Cairn (which cleans again on the way out)."""
    from cairn.observability import telemetry

    tid, _ = journey
    url, s = cairn
    telemetry.flush()
    raw_trace = httpx.get(f"http://127.0.0.1:{stack['tempo']}/api/traces/{tid}", timeout=30).text
    now = time.time()
    raw_logs = httpx.get(f"http://127.0.0.1:{stack['loki']}/loki/api/v1/query_range", timeout=30, params={
        "query": '{service_name=~".+"}', "start": str(int((now - 3600) * 1e9)), "end": str(int((now + 60) * 1e9)),
        "limit": "5000"}).text
    assert '"cairn-api"' in raw_trace and "statement" in raw_logs
    for blob, what in ((raw_trace, "trace"), (raw_logs, "logs")):
        for secret in (KEY, SERVICE_TOKEN, LITERAL):
            assert secret not in blob, f"the exported {what} carries {secret[:12]}..."
        assert not re.search(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.", blob), f"a JWT in the exported {what}"
        assert "Bearer " not in blob
