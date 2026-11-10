"""Ops dashboards end to end (L143), on the bundled stack's real containers (OpenTelemetry Collector, Prometheus, Loki,
Tempo, with deploy/observability/stack's own configurations; Prometheus also scrapes Cairn's /metrics) and the data
Cairn's own API makes as it is used:

* a dashboard with a tile of every ops source (PromQL range and instant, LogQL lines and metric, TraceQL, an SLO,
  firing alerts, a PostgreSQL instance) renders real series, lines and traces through Cairn, with its $service
  variable (its choices from Prometheus) and time range; the traces link to journeys Cairn reads back from Tempo;
* who: someone with only VIEW_OPERATIONS sees it (shared with them); someone without is refused every ops tile, the
  preview, saving one and the built-in dashboards; a decision that cannot be had refuses;
* the limits: range, points per series, series, a variable's value;
* the built-in dashboards are seeded once (idempotently), render without errors, and copy into one's own;
* Grafana: the export is a schema-valid Grafana dashboard (and, when the pinned grafana/grafana image is here, loads in
  a real Grafana whose panels query the same Prometheus and Loki), imports back, and the shipped Grafana dashboards
  (deploy/observability/*.json) import as tiles that render.

Telemetry is global to a process: CI runs this file in a pytest of its own (job trino). Needs docker and the stack's
images (tests/test_observability_e2e.py lists them)."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
pytest.importorskip("opentelemetry.sdk")

from test_observability_e2e import IMAGES, STACK, _ready, docker, host_ip, port, until  # noqa: E402

GRAFANA = os.environ.get("CAIRN_GRAFANA_IMAGE", "grafana/grafana:12.2.1")
ADMIN, OLU, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:olu", "urn:li:corpuser:bob"
SERVICE_TOKEN = "svc-" + uuid.uuid4().hex
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker and the stack's images (" + ", ".join(IMAGES.values()) + ")")


def _has(image: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=30).returncode == 0


@pytest.fixture(scope="module")
def cairn_port():
    return port()


@pytest.fixture(scope="module")
def stack(cairn_port):
    """The bundled stack; Prometheus scrapes the Collector and Cairn's own /metrics (on this machine's address)."""
    import shutil
    import tempfile

    tag = f"cairn-opsd-{os.getpid()}"
    d = tempfile.mkdtemp(prefix="opsd-")
    with open(f"{d}/prometheus.yml", "w") as fh:
        fh.write("global: {scrape_interval: 2s, evaluation_interval: 5s}\n"
                 "scrape_configs:\n"
                 "  - job_name: otel-collector\n    static_configs: [{targets: ['cairn-otel-collector:8889']}]\n"
                 f"  - job_name: cairn-api\n    static_configs: [{{targets: ['{host_ip()}:{cairn_port}']}}]\n")
    os.chmod(d, 0o755)
    os.chmod(f"{d}/prometheus.yml", 0o644)
    ports = {"otlp_http": port(), "otlp_grpc": port(), "prometheus": port(), "loki": port(), "tempo": port()}
    names = [f"{tag}-{n}" for n in ("tempo", "loki", "collector", "prometheus")]
    docker("rm", "-f", *names, check=False)
    docker("network", "rm", tag, check=False)
    docker("network", "create", tag)
    st = os.path.abspath(STACK)
    try:
        docker("run", "-d", "--name", names[0], "--network", tag, "--network-alias", "cairn-tempo",
               "-p", f"127.0.0.1:{ports['tempo']}:3200", "-v", f"{st}/tempo.yaml:/etc/tempo.yaml:ro",
               IMAGES["tempo"], "-config.file=/etc/tempo.yaml")
        docker("run", "-d", "--name", names[1], "--network", tag, "--network-alias", "cairn-loki",
               "-p", f"127.0.0.1:{ports['loki']}:3100", "-v", f"{st}/loki.yaml:/etc/loki.yaml:ro",
               IMAGES["loki"], "-config.file=/etc/loki.yaml")
        docker("run", "-d", "--name", names[2], "--network", tag, "--network-alias", "cairn-otel-collector",
               "-p", f"127.0.0.1:{ports['otlp_http']}:4318", "-p", f"127.0.0.1:{ports['otlp_grpc']}:4317",
               "-v", f"{st}/otel-collector.yaml:/etc/otelcol.yaml:ro", IMAGES["collector"], "--config=/etc/otelcol.yaml")
        docker("run", "-d", "--name", names[3], "--network", tag, "--network-alias", "cairn-prometheus",
               "-p", f"127.0.0.1:{ports['prometheus']}:9090",
               "-v", f"{d}/prometheus.yml:/etc/prometheus/prometheus.yml:ro", IMAGES["prometheus"],
               "--config.file=/etc/prometheus/prometheus.yml")
        for name, path in (("tempo", "/ready"), ("loki", "/ready"), ("prometheus", "/-/ready")):
            until(lambda n=name, p=path: httpx.get(f"http://127.0.0.1:{ports[n]}{p}", timeout=3).status_code == 200, 180,
                  what=name)
        yield {**ports, "network": tag}
    finally:
        if os.environ.get("CAIRN_OBS_E2E_LOGS"):
            for n in names:
                print(f"--- {n}\n" + docker("logs", "--tail", "60", n, check=False).stderr[-4000:])
        docker("rm", "-f", *names, check=False)
        docker("network", "rm", tag, check=False)
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module")
def cairn(stack, cairn_port, tmp_path_factory):
    """Cairn's API in this process (on every address, for Prometheus to scrape), exporting to the Collector."""
    import logging

    import uvicorn

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.observability import telemetry
    from cairn.quality.collectors import ConnectionRegistry
    from cairn.services import build_services

    tmp = tmp_path_factory.mktemp("cairn")
    os.environ.update({
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{stack['otlp_http']}", "OTEL_TRACES_SAMPLER": "always_on",
        "OTEL_BSP_SCHEDULE_DELAY": "300", "OTEL_BLRP_SCHEDULE_DELAY": "300", "OTEL_METRIC_EXPORT_INTERVAL": "2000",
        "CAIRN_OBS_PROMETHEUS_URL": f"http://127.0.0.1:{stack['prometheus']}",
        "CAIRN_OBS_LOKI_URL": f"http://127.0.0.1:{stack['loki']}",
        "CAIRN_OBS_TEMPO_URL": f"http://127.0.0.1:{stack['tempo']}"})
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = os.environ.get("NO_PROXY", "") + f",{host_ip()},127.0.0.1"
    logging.getLogger("cairn").setLevel(logging.INFO)
    settings = Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token=SERVICE_TOKEN,
                        dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")})
    s = build_services(settings, graph=InMemoryGraph(), with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="olu-watches", effect="allow", actions=["VIEW_OPERATIONS"],
               condition={"attr": "subject.urn", "op": "eq", "value": OLU})]))
    # A connection whose Trino is not there: statements on it fail (errors in the spans and the logs).
    s.data_access.registry = ConnectionRegistry({"gone": {
        "url": f"trino://cairn@127.0.0.1:{port()}/tpch/tiny?protocol=http", "run_as_user": True,
        "catalog": {"platform": "tpch", "database": "", "dialect": "trino"}}})
    app = create_api_app(s)
    assert telemetry.enabled(), "OpenTelemetry did not start"
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=cairn_port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    until(lambda: socket.create_connection(("127.0.0.1", cairn_port), 0.3).close() or True, 30, 0.1, "Cairn's API")
    yield f"http://127.0.0.1:{cairn_port}", s
    server.should_exit = True
    t.join(30)


def api(cairn, method, path, who=ADMIN, **kw):
    url, _ = cairn
    return httpx.request(method, url + path, headers={"X-Cairn-Actor": who}, timeout=90, **kw)


SEL = 'service_name="$service",span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"'
TILES = [
    {"title": "Requests / s", "width": 2, "view": "timeseries", "display": {"unit": "reqps"}, "promql": {"queries": [
        {"expr": f"sum by (service_name) (rate(traces_span_metrics_calls_total{{{SEL}}}[$__rate_interval]))",
         "legend": "{{service_name}}"}]}},
    {"title": "Requests now", "view": "stat", "display": {"unit": "reqps", "thresholds": [
        {"value": None, "color": "green"}, {"value": 1000, "color": "red"}]},
     "promql": {"queries": [{"expr": f"sum(rate(traces_span_metrics_calls_total{{{SEL}}}[5m]))"}], "instant": True}},
    {"title": "Policy decisions", "view": "bars", "promql": {"queries": [
        {"expr": "sum by (decision) (increase(cairn_pdp_decisions_total[$__interval]))", "legend": "{{decision}}"}],
        "step": "15s"}},
    {"title": "Logs", "view": "logs", "logql": {"queries": [{"expr": '{service_name="$service"}'}], "limit": 50}},
    {"title": "Log lines", "view": "bars", "logql": {"queries": [
        {"expr": 'sum(count_over_time({service_name="$service"}[$__interval]))', "legend": "lines"}]}},
    {"title": "Failed requests", "traces": {"query": '{ resource.service.name = "$service" && status = error }',
                                            "limit": 10}},
    {"title": "API answers", "slo": {"id": "api-answers"}},
    {"title": "Firing", "alerts": {"state": "firing", "labels": {"service": "$service"}}},
    {"title": "Database", "postgres": {"instance": "default"}},
]
CONTROLS = [{"id": "time", "kind": "time", "label": "Time range", "default": "15m", "refresh": 30},
            {"id": "service", "kind": "variable", "label": "Service", "default": "cairn-api",
             "query": {"source": "label_values", "label": "service_name", "match": "traces_span_metrics_calls_total"}}]


@pytest.fixture(scope="module")
def traffic(cairn):
    """Requests through Cairn's API (some answering 5xx), an SLO and a firing alert rule."""
    from cairn.observability import telemetry

    sel = 'service_name="cairn-api",span_kind="SPAN_KIND_SERVER"'
    r = api(cairn, "POST", "/v1/observability/slos", json={
        "id": "api-answers", "name": "API answers", "service": "cairn-api", "objective": 99, "window": "1d",
        "indicator": {"type": "ratio",
                      "errors": f'sum(rate(traces_span_metrics_calls_total{{{sel},status_code="STATUS_CODE_ERROR"}}[$window]))',
                      "total": f"sum(rate(traces_span_metrics_calls_total{{{sel}}}[$window]))"}})
    assert r.status_code == 201, r.text
    r = api(cairn, "POST", "/v1/observability/alert-rules", json={
        "id": "api-is-up", "name": "API is answering", "severity": "info", "labels": {"service": "cairn-api"},
        "expr": 'sum(rate(traces_span_metrics_calls_total{service_name="cairn-api"}[5m])) > 0'})
    assert r.status_code == 201, r.text
    for i in range(30):
        api(cairn, "GET", "/v1/observability/overview")
        api(cairn, "GET", "/v1/observability/tiles/limits", who=OLU if i % 2 else BOB)   # decided by the PDP
        if i % 3 == 0:
            r = api(cairn, "POST", "/v1/statements", json={"sql": "SELECT 1", "connection": "gone"})
            assert r.status_code >= 500, r.text
    telemetry.flush()
    until(lambda: api(cairn, "POST", "/v1/observability/tiles/preview", json={
        "tile": {"promql": {"queries": [{"expr": 'sum(traces_span_metrics_calls_total{service_name="cairn-api"})'}],
                            "instant": True}}, "values": {"time": "15m"}}).json()["ops"]["series"], 120, 3,
          "span metrics in Prometheus")

    def fired():
        api(cairn, "POST", "/v1/observability/evaluate")
        return [a for a in api(cairn, "GET", "/v1/observability/alerts", params={"state": "firing"}).json()["items"]
                if a["source"] == "rule:api-is-up"]

    until(fired, 120, 5, "the rule to fire")
    return True


@pytest.fixture(scope="module")
def board(cairn, traffic):
    r = api(cairn, "POST", "/sql/dashboards", json={"name": "API on call", "tiles": TILES, "controls": CONTROLS})
    assert r.status_code == 201, r.text
    return r.json()


def render(cairn, did, who=ADMIN, **controls):
    r = api(cairn, "POST", f"/sql/dashboards/{did}/render", who=who,
            json={"controls": {"time": "15m", "service": "cairn-api", **controls}})
    assert r.status_code == 200, r.text
    return r.json()


def test_every_ops_tile_shows_real_data(cairn, board, stack):
    from cairn.observability import telemetry

    telemetry.flush()

    seen: dict = {}

    def complete():
        out = render(cairn, board["id"])
        t = {x["title"]: x for x in out["tiles"]}
        seen["tiles"] = {k: v.get("error") or {kk: (len(vv) if isinstance(vv, list) else vv) for kk, vv in
                                                v.get("ops", {}).items()} for k, v in t.items()}
        ok = (t["Requests / s"].get("ops", {}).get("series") and t["Policy decisions"].get("ops", {}).get("series")
              and t["Logs"].get("ops", {}).get("lines") and t["Log lines"].get("ops", {}).get("series")
              and t["Failed requests"].get("ops", {}).get("traces"))
        return out if ok else None

    try:
        out = until(complete, 180, 5, "every tile's data")
    except AssertionError as e:
        targets = httpx.get(f"http://127.0.0.1:{stack['prometheus']}/api/v1/targets", timeout=10).json()
        seen["targets"] = [(t["labels"].get("job"), t["health"], t.get("lastError")) for t in
                           targets["data"]["activeTargets"]]
        raise AssertionError(f"{e}: {json.dumps(seen, default=str)[:4000]}")
    t = {x["title"]: x for x in out["tiles"]}
    assert out["window"]["end_ms"] - out["window"]["start_ms"] == 15 * 60 * 1000
    assert out["window"]["variables"] == {"service": "cairn-api"}
    s = t["Requests / s"]["ops"]
    assert s["kind"] == "series" and s["series"][0]["name"] == "cairn-api"
    assert any(v and v > 0 for _, v in s["series"][0]["points"]) and s["step_s"] >= 5
    stat = t["Requests now"]["ops"]
    assert stat["instant"] and stat["series"][0]["points"][0][1] > 0
    assert {x["name"] for x in t["Policy decisions"]["ops"]["series"]} <= {"ALLOW", "DENY", "NOT_APPLICABLE"}
    assert t["Policy decisions"]["ops"]["step_s"] == 15
    lines = t["Logs"]["ops"]["lines"]
    assert all(x["service"] == "cairn-api" for x in lines) and SERVICE_TOKEN not in json.dumps(lines)
    assert t["Log lines"]["ops"]["series"][0]["name"] == "lines"
    traces = t["Failed requests"]["ops"]["traces"]
    assert traces[0]["root_service"] == "cairn-api"
    assert traces[0]["journey"] == f"/observability?tab=journey&trace={traces[0]['trace_id']}"
    j = api(cairn, "GET", f"/v1/observability/journey/{traces[0]['trace_id']}").json()      # the link leads somewhere
    assert j["errors"] >= 1 and "cairn-api" in j["services"]
    slo = t["API answers"]["ops"]
    assert slo["kind"] == "slo" and slo["status"]["objective"] == 99 and slo["status"]["sli"] is not None
    assert slo["status"]["budget_remaining"] is not None
    firing = t["Firing"]["ops"]
    assert "API is answering" in [a["name"] for a in firing["alerts"]]
    assert all(a["labels"]["service"] == "cairn-api" for a in firing["alerts"])
    pg = t["Database"]
    if getattr(cairn[1], "pg_instances", None) is None:            # degrades with a clear message
        assert pg["status"] == 501 and "/v1/databases/instances/{name}/metrics" in pg["error"]
    # Another value of the variable: nothing from a service that does not exist, and no error.
    other = {x["title"]: x for x in render(cairn, board["id"], service="no-such-service")["tiles"]}
    assert other["Requests / s"]["ops"]["series"] == [] and other["Firing"]["ops"]["alerts"] == []
    # The variable's choices come from Prometheus.
    r = api(cairn, "GET", f"/sql/dashboards/{board['id']}/controls/service/values")
    assert r.status_code == 200 and "cairn-api" in r.json(), r.text


def test_privileges_view_operations_shared_and_refused(cairn, board):
    url, s = cairn
    assert api(cairn, "PUT", f"/sql/shares/dashboard/{board['id']}", json={"principal": OLU, "role": "VIEWER"}).status_code == 200
    assert api(cairn, "PUT", f"/sql/shares/dashboard/{board['id']}", json={"principal": BOB, "role": "VIEWER"}).status_code == 200
    olu = {x["title"]: x for x in render(cairn, board["id"], who=OLU)["tiles"]}
    assert "error" not in olu["Requests / s"] and olu["Requests / s"]["ops"]["series"]
    bob = render(cairn, board["id"], who=BOB)
    for t in bob["tiles"]:
        if t["title"] != "Database":
            assert t["status"] == 403 and "VIEW_OPERATIONS" in t["error"] and "ops" not in t, t["title"]
    r = api(cairn, "POST", "/v1/observability/tiles/preview", who=BOB, json={"tile": TILES[0]})
    assert r.status_code == 403
    r = api(cairn, "POST", "/sql/dashboards", who=BOB, json={"name": "mine", "tiles": [TILES[0]]})
    assert r.status_code == 403 and "VIEW_OPERATIONS" in r.text
    assert not [d for d in api(cairn, "GET", "/sql/dashboards", who=BOB).json() if d["builtin"]]
    assert api(cairn, "GET", "/sql/dashboards/ops-platform-overview", who=BOB).status_code == 404
    assert api(cairn, "GET", f"/sql/dashboards/{board['id']}/controls/service/values", who=BOB).status_code == 403
    # A decision that cannot be had refuses.
    keep = s.ops_tiles._authorize

    def broken(actor, privilege):
        raise RuntimeError("the policy engine is not answering")

    s.ops_tiles._authorize = broken
    try:
        down = render(cairn, board["id"])
        assert all(t["status"] == 403 for t in down["tiles"] if t["title"] != "Database")
    finally:
        s.ops_tiles._authorize = keep


def test_limits_protect_the_backends(cairn, board):
    url, s = cairn
    now = int(time.time() * 1000)
    out = api(cairn, "POST", f"/sql/dashboards/{board['id']}/render",
              json={"controls": {"time": [now - 8 * 86400_000, now]}}).json()
    assert all(t["status"] == 422 and "at most 7d" in t["error"] for t in out["tiles"] if t["title"] not in
               ("Database", "Firing", "API answers")), [t.get("error") for t in out["tiles"]]
    r = api(cairn, "POST", "/v1/observability/tiles/preview", json={
        "tile": {"promql": {"queries": [{"expr": "up"}], "step": "5s"}}, "values": {"time": "7d"}})
    assert r.status_code == 422 and "points per series" in r.json()["detail"]
    keep = s.ops_tiles.limits.max_series
    s.ops_tiles.limits.max_series = 2
    try:
        r = api(cairn, "POST", "/v1/observability/tiles/preview", json={
            "tile": {"promql": {"queries": [{"expr": "traces_span_metrics_calls_total"}]}}, "values": {"time": "15m"}})
        assert r.status_code == 422 and "at most 2" in r.json()["detail"], r.text
    finally:
        s.ops_tiles.limits.max_series = keep
    # A variable's value never leaves its label matcher.
    r = api(cairn, "POST", f"/sql/dashboards/{board['id']}/render",
            json={"controls": {"service": 'x"} or vector(1) #'}})
    assert all(t["status"] == 422 for t in r.json()["tiles"])
    lim = api(cairn, "GET", "/v1/observability/tiles/limits").json()
    assert lim["max_range"] == "7d" and lim["allowed"] is True


def test_builtin_dashboards_render_and_copy(cairn, traffic):
    from cairn.observability import builtin

    url, s = cairn
    mine = {d["id"]: d for d in api(cairn, "GET", "/sql/dashboards").json() if d["builtin"]}
    assert set(mine) == {b["id"] for b in builtin.BUILTINS}
    before = mine["ops-platform-overview"]["updated_ms"]
    assert builtin.seed(s.sql_boards) == []                                         # idempotent
    assert api(cairn, "GET", "/sql/dashboards/ops-platform-overview").json()["updated_ms"] == before
    for did in mine:
        out = api(cairn, "POST", f"/sql/dashboards/{did}/render", json={"controls": {"time": "15m"}}).json()
        for t in out["tiles"]:
            if t.get("postgres") is not None or (t.get("slo") is not None and did == "ops-slos-alerts"):
                assert t.get("status") in (None, 422, 501), (did, t["title"], t.get("error"))   # no instance, no SLO picked
            else:
                assert "error" not in t, (did, t["title"], t.get("error"))
    overview = {t["title"]: t for t in api(cairn, "POST", "/sql/dashboards/ops-platform-overview/render",
                                           json={"controls": {"time": "15m"}}).json()["tiles"]}
    assert any(x["name"] == "cairn-api" for x in overview["Requests / s by service"]["ops"]["series"])
    assert overview["Policy decisions / s"]["ops"]["series"]
    assert overview["Decision cache hit rate"]["ops"]["series"]
    # Not changed in place: copied, then the copy is one's own.
    assert api(cairn, "PUT", "/sql/dashboards/ops-platform-overview", json={"name": "x", "tiles": TILES[:1]}).status_code == 403
    r = api(cairn, "POST", "/sql/dashboards/ops-platform-overview/copy", json={"name": "My overview"})
    assert r.status_code == 201, r.text
    copy = r.json()
    assert copy["owner"] == ADMIN and copy["role"] == "OWNER" and not copy["builtin"]
    assert len(copy["tiles"]) == len(builtin.PLATFORM["tiles"])
    r = api(cairn, "PUT", f"/sql/dashboards/{copy['id']}", json={**copy, "tiles": copy["tiles"][:3]})
    assert r.status_code == 200 and len(r.json()["tiles"]) == 3


def _valid_grafana(doc: dict) -> None:
    """Schema-level checks of a Grafana dashboard (what Grafana's import needs)."""
    assert doc["schemaVersion"] >= 36 and isinstance(doc["title"], str) and len(doc["uid"]) <= 40
    assert isinstance(doc["panels"], list) and isinstance(doc["templating"]["list"], list)
    names = {v["name"] for v in doc["templating"]["list"]}
    ids = set()
    for p in doc["panels"]:
        assert p["id"] not in ids
        ids.add(p["id"])
        g = p["gridPos"]
        assert 0 <= g["x"] < 24 and 0 < g["w"] <= 24 and g["x"] + g["w"] <= 24 and g["h"] > 0
        if p["type"] == "text":
            assert p["options"]["mode"] == "markdown"
            continue
        assert p["type"] in ("timeseries", "stat", "gauge", "table", "logs", "heatmap")
        uid = p["datasource"]["uid"]
        assert uid.startswith("${") and uid[2:-1] in names
        for t in p["targets"]:
            assert t["refId"] and (t.get("expr") or t.get("query"))
        steps = (p["fieldConfig"]["defaults"].get("thresholds") or {}).get("steps")
        if steps:
            assert steps[0]["value"] is None


def test_grafana_export_import_and_the_shipped_dashboards(cairn, board, stack):
    from cairn.iac.bundle import _same

    r = api(cairn, "GET", f"/sql/dashboards/{board['id']}/grafana")
    assert r.status_code == 200, r.text
    doc = r.json()
    _valid_grafana(doc)
    by = {p["title"]: p for p in doc["panels"]}
    assert by["Requests / s"]["type"] == "timeseries" and by["Requests / s"]["gridPos"]["w"] == 24
    assert by["Requests / s"]["targets"][0]["expr"] == TILES[0]["promql"]["queries"][0]["expr"]
    assert by["Requests / s"]["fieldConfig"]["defaults"]["unit"] == "reqps"
    assert by["Requests now"]["type"] == "stat" and by["Requests now"]["targets"][0]["instant"] is True
    assert by["Logs"]["type"] == "logs" and by["Logs"]["datasource"]["type"] == "loki"
    assert by["Failed requests"]["targets"][0]["queryType"] == "traceql"
    for title in ("API answers", "Firing", "Database"):
        assert by[title]["type"] == "text" and "Not exportable" in by[title]["options"]["content"]
    assert {s["title"] for s in doc["cairn"]["skipped"]} == {"API answers", "Firing", "Database"}
    variables = {v["name"]: v for v in doc["templating"]["list"]}
    assert variables["service"]["type"] == "query" and "label_values(" in variables["service"]["definition"]
    assert {"ds_prometheus", "ds_loki", "ds_tempo"} <= set(variables)
    assert doc["time"] == {"from": "now-15m", "to": "now"} and doc["refresh"] == "30s"
    # Back into Cairn: the PromQL, LogQL and TraceQL tiles, the variable and the time range; text panels skipped.
    r = api(cairn, "POST", "/sql/dashboards/import/grafana", json={"dashboard": doc})
    assert r.status_code == 201, r.text
    back = r.json()
    assert len(back["skipped"]) == 3
    tiles = {t["title"]: t for t in back["dashboard"]["tiles"]}
    for t in TILES[:6]:
        src = next(k for k in ("promql", "logql", "traces") if t.get(k))
        assert _same(t[src], tiles[t["title"]][src]), t["title"]
        assert tiles[t["title"]]["view"] == (t.get("view") or {"traces": "table"}.get(src))
    controls = {c["id"]: c for c in back["dashboard"]["controls"]}
    assert controls["time"]["default"] == "15m" and controls["time"]["refresh"] == 30
    assert controls["service"]["query"] == CONTROLS[1]["query"]
    out = {t["title"]: t for t in render(cairn, back["dashboard"]["id"])["tiles"]}
    assert out["Requests / s"]["ops"]["series"]
    # The Grafana dashboards Cairn ships (deploy/observability) import as tiles that run.
    for f, panels in (("cairn-overview.json", 15), ("cairn-store.json", 21)):
        raw = json.load(open(os.path.join(ROOT, "deploy", "observability", f)))
        r = api(cairn, "POST", "/sql/dashboards/import/grafana", json=raw)
        assert r.status_code == 201, r.text
        got = r.json()
        assert len(got["dashboard"]["tiles"]) == panels, got["skipped"]
        assert not [x for x in got["skipped"] if "panel" in x and "row" not in x["reason"]], got["skipped"]
        out = render(cairn, got["dashboard"]["id"])
        assert not [t for t in out["tiles"] if t.get("error")], [t.get("error") for t in out["tiles"]]
    if _has(GRAFANA):
        _into_real_grafana(doc, stack)


def _into_real_grafana(doc: dict, stack: dict) -> None:
    """The export loaded into a real Grafana (pinned image), its data sources the same Prometheus and Loki: every
    exported PromQL and LogQL panel's query answers there."""
    name = f"cairn-opsd-grafana-{os.getpid()}"
    gp = port()
    docker("rm", "-f", name, check=False)
    docker("run", "-d", "--name", name, "--network", stack["network"], "-p", f"127.0.0.1:{gp}:3000",
           "-e", "GF_AUTH_ANONYMOUS_ENABLED=true", "-e", "GF_AUTH_ANONYMOUS_ORG_ROLE=Admin",
           "-e", "GF_AUTH_DISABLE_LOGIN_FORM=true", "-e", "GF_ANALYTICS_REPORTING_ENABLED=false",
           "-e", "GF_ANALYTICS_CHECK_FOR_UPDATES=false", "-e", "GF_PLUGINS_PREINSTALL_DISABLED=true", GRAFANA)
    g = f"http://127.0.0.1:{gp}"
    try:
        until(lambda: httpx.get(f"{g}/api/health", timeout=3).status_code == 200, 180, 2, "Grafana")
        for kind, uid, target in (("prometheus", "prom", "http://cairn-prometheus:9090"),
                                  ("loki", "loki", "http://cairn-loki:3100"), ("tempo", "tempo", "http://cairn-tempo:3200")):
            r = httpx.post(f"{g}/api/datasources", timeout=30, json={"name": kind, "type": kind, "uid": uid,
                                                                       "url": target, "access": "proxy"})
            assert r.status_code == 200, r.text
        uids = {"ds_prometheus": "prom", "ds_loki": "loki", "ds_tempo": "tempo"}
        sent = json.loads(json.dumps(doc))
        for v in sent["templating"]["list"]:
            if v["name"] in uids:
                v["current"] = {"text": v["query"], "value": uids[v["name"]]}
        r = httpx.post(f"{g}/api/dashboards/db", timeout=30, json={"dashboard": sent, "overwrite": True})
        assert r.status_code == 200, r.text
        stored = httpx.get(f"{g}/api/dashboards/uid/{doc['uid']}", timeout=30).json()["dashboard"]
        assert [p["title"] for p in stored["panels"]] == [p["title"] for p in doc["panels"]]
        now = int(time.time() * 1000)
        checked = []
        for p in stored["panels"]:
            if p["type"] == "text" or p["datasource"]["type"] == "tempo":
                continue
            checked.append(p["title"])
            ds = uids[p["datasource"]["uid"][2:-1]]
            queries = [{**t, "datasource": {"type": p["datasource"]["type"], "uid": ds},
                        "expr": t["expr"].replace("$service", "cairn-api").replace("$__rate_interval", "1m")
                        .replace("$__interval", "30s"), "intervalMs": 30000, "maxDataPoints": 100}
                       for t in p["targets"]]
            r = httpx.post(f"{g}/api/ds/query", timeout=60, json={"queries": queries, "from": str(now - 900_000),
                                                                   "to": str(now)})
            assert r.status_code == 200, (p["title"], r.text[:500])
            frames = [f for res in r.json()["results"].values() for f in res.get("frames", [])]
            assert all(res.get("status", 200) == 200 and not res.get("error") for res in r.json()["results"].values()), \
                (p["title"], r.text[:500])
            if p["title"] in ("Requests / s", "Logs"):
                assert frames and any(f["data"]["values"] for f in frames), (p["title"], r.text[:500])
        assert len(checked) == 5, checked
        print(f"real Grafana ({GRAFANA}) loaded the export; panels queried there: {', '.join(checked)}")
    finally:
        docker("rm", "-f", name, check=False)
