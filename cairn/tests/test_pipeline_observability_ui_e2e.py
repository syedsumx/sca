"""Observability → Pipelines and Export in Chromium (L145), against a real Cairn API with its database and audit log:
an operator sees pipelines from OpenLineage (an Airflow DAG's runs, one slow, one failed) with their health; opens the
failed run's Gantt, what it reaches and the quality of what it wrote; asks why it failed; makes a data product and an
SLA with the generated forms (kept and audited); adds an OTLP destination whose header comes from a secret scope,
sees the Collector configuration (the variable's name, never its value) and applies it. axe finds no serious or
critical violation on the list, a run and the Export tab. Real Airflow and the Collector: the other e2e tests.

Needs Playwright (with Chromium), the built bundle (ui/dist/mfe, `npm run build:mfe`) and axe-core."""

from __future__ import annotations

import base64
import os
import re
import socket
import threading
import time
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "ui", "dist", "mfe")
AXE = os.path.join(ROOT, "ui", "node_modules", "axe-core", "axe.min.js")
OPS = "urn:li:corpuser:ops"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
DAILY = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.daily,PROD)"
REPORT = "urn:li:dataset:(urn:li:dataPlatform:iceberg,finance.report,PROD)"
TOKEN = "tok-" + uuid.uuid4().hex


def _ready():
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(os.path.join(BUNDLE, "remoteEntry.js")) and os.path.exists(AXE)


pytestmark = pytest.mark.skipif(not _ready(), reason="needs Playwright, axe-core and the built UI bundle")

HOST = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Cairn</title></head>
<body><div id="root"></div><script src="/ui/remoteEntry.js"></script><script>
(async () => {
  await cairnConsumer.init({});
  const mount = (await cairnConsumer.get('./mount'))().mount;
  mount(document.getElementById('root'), { apiBaseUrl: window.location.origin, catalogUrl: null });
})();
</script></body></html>"""


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ol(event_type, name, run_id, when, parent=None, inputs=(), outputs=(), error="", dag_run_id=""):
    facets: dict = {}
    if parent:
        facets["parent"] = {"job": {"namespace": "airflow://prod", "name": parent[0]}, "run": {"runId": parent[1]}}
        facets["airflow"] = {"dagRun": {"run_id": dag_run_id}}
    else:
        facets["airflowDagRun"] = {"dagRun": {"run_id": dag_run_id}}
    if error:
        facets["errorMessage"] = {"message": error, "programmingLanguage": "python"}
    ds = lambda u: {"namespace": "iceberg://", "name": u.split(",")[1]}  # noqa: E731
    return {"eventType": event_type, "eventTime": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when)) + ".000Z",
            "producer": "https://github.com/apache/airflow", "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json",
            "job": {"namespace": "airflow://prod", "name": name,
                    "facets": {"jobType": {"integration": "AIRFLOW", "jobType": "TASK" if parent else "DAG"}}},
            "run": {"runId": run_id, "facets": facets}, "inputs": [ds(u) for u in inputs], "outputs": [ds(u) for u in outputs]}


def dag_run(s, run, start, seconds, fail=""):
    olr = str(uuid.uuid4())
    events = [ol("START", "daily_sales", olr, start, dag_run_id=run)]
    for i, task in enumerate(("extract", "load")):
        tid = str(uuid.uuid4())
        t0 = start + i * seconds / 2
        failed = fail and task == "load"
        events += [ol("START", f"daily_sales.{task}", tid, t0, ("daily_sales", olr), dag_run_id=run),
                   ol("FAIL" if failed else "COMPLETE", f"daily_sales.{task}", tid, t0 + seconds / 2, ("daily_sales", olr),
                      inputs=(ORDERS,) if task == "extract" else (), outputs=(DAILY,) if task == "load" else (),
                      error=fail if failed else "", dag_run_id=run)]
    events.append(ol("FAIL" if fail else "COMPLETE", "daily_sales", olr, start + seconds, dag_run_id=run, error=fail))
    s.openlineage.handle_many(events)


@pytest.fixture
def cairn(settings, graph, monkeypatch, tmp_path):
    import uvicorn
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    from cairn.app import create_api_app
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.quality.store import evaluations, monitors
    from cairn.services import build_services

    for k in ("CAIRN_OBS_PROMETHEUS_URL", "CAIRN_OBS_LOKI_URL", "CAIRN_OBS_TEMPO_URL", "CAIRN_AIRFLOW_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CAIRN_SECRETS_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("CAIRN_OBS_EXPORTERS_DIR", str(tmp_path / "exporters"))
    graph.aspects[(REPORT, "upstreamLineage")] = {"upstreams": [{"dataset": DAILY, "type": "TRANSFORMED"}]}
    graph.aspects[(OPS, "corpUserInfo")] = {"active": True, "displayName": "Olu Ops"}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="ops", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": OPS})]))
    now = time.time()
    for i in range(6):
        dag_run(s, f"scheduled__{i}", now - 86_400 + i * 3600, 60 + i)
    dag_run(s, "scheduled__slow", now - 7200, 900)
    dag_run(s, "manual__1", now - 600, 40, fail="out of memory while loading")
    with s.engine.begin() as c:
        c.execute(monitors.insert().values(id="m-daily", name="daily not empty", dataset_urn=DAILY, type="volume", spec={},
                                           status="ACTIVE", enabled=True, next_run_at_ms=None, created_at="2026-01-01",
                                           updated_at="2026-01-01"))
        c.execute(evaluations.insert().values(id="e1", monitor_id="m-daily", ts_ms=int(now * 1000), run_id="q1",
                                              metric_value=0.0, expected_low=1.0, expected_high=None, status="FAIL",
                                              score=None, message="0 rows", details=None))
    s.secret_scopes.create(OPS, {"name": "otel"})
    s.secret_scopes.put(OPS, "otel", "token", TOKEN)
    app = create_api_app(s)
    app.mount("/ui", StaticFiles(directory=BUNDLE), name="ui")
    app.add_api_route("/host", lambda: HTMLResponse(HOST), methods=["GET"])
    p = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=p, timeout_keep_alive=75, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    end = time.time() + 30
    while True:
        try:
            socket.create_connection(("127.0.0.1", p), 0.3).close()
            break
        except OSError:
            assert time.time() < end
            time.sleep(0.1)
    yield s, f"http://127.0.0.1:{p}", tmp_path
    server.should_exit = True


def until(fn, timeout=20.0):
    end = time.time() + timeout
    while True:
        out = fn()
        if out or time.time() > end:
            return out
        time.sleep(0.2)


def axe(page) -> list[dict]:
    page.add_script_tag(path=AXE)
    result = page.evaluate("""async () => {
        const r = await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']},
                                           resultTypes: ['violations']});
        return r.violations.map(v => ({id: v.id, impact: v.impact, help: v.help,
                                       nodes: v.nodes.slice(0, 5).map(n => n.target.join(' ') + ' :: ' + n.failureSummary)}));
    }""")
    return [v for v in result if v["impact"] in ("serious", "critical")]


def test_pipelines_runs_definitions_and_export_in_the_browser(cairn):
    from playwright.sync_api import expect, sync_playwright

    s, url, tmp = cairn
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": OPS}, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(f"{url}/host#/discover", wait_until="domcontentloaded")
            role = page.get_by_role("dialog", name="Welcome to Cairn. What do you mostly do here?")
            expect(role).to_be_visible(timeout=30_000)
            role.get_by_role("radio", name=re.compile("^Admin")).check()
            role.get_by_role("button", name="Continue").click()
            expect(role).to_be_hidden(timeout=20_000)
            page.goto(f"{url}/host#/observability?tab=pipelines", wait_until="domcontentloaded")
            pipes = page.get_by_role("region", name="Pipelines")
            expect(pipes.get_by_text("Failing")).to_be_visible(timeout=30_000)
            expect(pipes.get_by_role("img", name=re.compile("Durations of the last 8 runs of airflow:daily_sales"))).to_be_visible()
            assert not axe(page), axe(page)

            # ---- the failed run: Gantt, impact, quality, why ---------------------------------------------------------
            pipes.get_by_role("button", name="Open airflow:daily_sales").click()
            drawer = page.get_by_role("dialog").filter(has=page.get_by_role("region", name="Runs of airflow:daily_sales"))
            expect(drawer).to_be_visible(timeout=20_000)
            gantt = drawer.get_by_role("list", name="Tasks of run manual__1")
            expect(gantt).to_be_visible(timeout=20_000)
            expect(gantt.get_by_role("listitem", name=re.compile(r"^load: failed"))).to_be_visible()
            impact = drawer.get_by_role("region", name="What it reaches")
            expect(impact.get_by_text("finance.report")).to_be_visible()
            expect(drawer.get_by_role("region", name="Quality of what it wrote").get_by_text("FAIL")).to_be_visible()
            drawer.get_by_role("button", name="Why did it fail?").click()
            causes = drawer.get_by_role("region", name="Likely causes")
            expect(causes.get_by_text("resource")).to_be_visible(timeout=20_000)
            drawer.get_by_role("button", name="scheduled__slow").click()
            expect(drawer.get_by_text(re.compile("^Slower than usual"))).to_be_visible(timeout=20_000)
            assert not axe(page), axe(page)
            page.keyboard.press("Escape")

            # ---- a data product and an SLA with the generated forms --------------------------------------------------
            page.get_by_role("button", name="New data product").click()
            d = page.get_by_role("dialog").last
            d.get_by_label(re.compile(r"^ID")).fill("finance")
            d.get_by_label(re.compile(r"^Name")).fill("Finance")
            pipes_field = d.get_by_role("combobox", name=re.compile(r"^Pipelines"))
            pipes_field.fill("airflow:daily_sales")
            pipes_field.press("Enter")
            d.get_by_role("button", name=re.compile(r"(^|loading )New data product$")).last.click()
            prod = until(lambda: next((x for x in s.observability.list("data_product") if x["id"] == "finance"), None))
            assert prod and prod["pipelines"] == ["airflow:daily_sales"], prod
            page.keyboard.press("Escape")
            page.get_by_role("button", name="New SLA").click()
            d = page.get_by_role("dialog").last
            d.get_by_label(re.compile(r"^ID")).fill("finance-by-six")
            d.get_by_label(re.compile(r"^Name")).fill("Finance by six")
            d.get_by_label(re.compile(r"^Pipeline$")).fill("airflow:daily_sales")
            d.get_by_label(re.compile(r"^Deadline")).fill("06:00")
            d.get_by_role("button", name=re.compile(r"(^|loading )New SLA$")).last.click()
            sla = until(lambda: next((x for x in s.observability.list("pipeline_sla") if x["id"] == "finance-by-six"), None))
            assert sla and sla["pipeline"] == "airflow:daily_sales" and sla["deadline"] == "06:00", sla
            assert any(e["action"] == "observability.pipeline_sla_created" and e["actor"] == OPS
                       for e in s.audit.entries("observability", 1000))
            page.keyboard.press("Escape")
            expect(page.get_by_role("cell", name="Finance by six", exact=True)).to_be_visible(timeout=20_000)   # L150: a menu names it

            # ---- an OTLP destination, its configuration and apply ----------------------------------------------------
            page.goto(f"{url}/host#/observability?tab=export", wait_until="domcontentloaded")   # Platform health (L149)
            page.get_by_role("button", name="New destination").click()
            d = page.get_by_role("dialog").last
            d.get_by_label(re.compile(r"^ID")).fill("customer")
            d.get_by_label(re.compile(r"^Endpoint")).fill("https://otel.customer.example")
            d.get_by_role("button", name=re.compile(r"(^|loading )New destination$")).last.click()
            ex = until(lambda: next((x for x in s.observability.list("exporter") if x["id"] == "customer"), None))
            assert ex, "the destination was not made"
            page.keyboard.press("Escape")
            # Its header from the secret scope (the form takes JSON for headers): through the API, as an operator does.
            r = s.observability.replace("exporter", OPS, "customer", {**{k: ex[k] for k in (
                "name", "protocol", "endpoint", "signals", "sampling_ratio", "tls", "enabled")}, "protocol": "http",
                "headers": [{"name": "authorization", "scope": "otel", "key": "token", "prefix": "Bearer "}]})
            assert r["headers"][0]["scope"] == "otel"
            page.reload(wait_until="domcontentloaded")
            region = page.get_by_role("region", name="OpenTelemetry export")
            expect(region.get_by_text("authorization ← otel/token")).to_be_visible(timeout=30_000)
            region.get_by_role("button", name="Collector configuration").click()
            cfg = page.get_by_role("region", name="Collector configuration")
            expect(cfg.get_by_text(re.compile("CAIRN_EXPORTER_CUSTOMER_0")).first).to_be_visible(timeout=20_000)
            assert TOKEN not in page.content()
            assert not axe(page), axe(page)
            region.get_by_role("button", name="Apply").click()
            assert until(lambda: (tmp / "exporters" / "exporters.env").exists())
            assert (tmp / "exporters" / "exporters.env").read_text() == f"CAIRN_EXPORTER_CUSTOMER_0={TOKEN}\n"
        finally:
            browser.close()
    assert not errors, errors
