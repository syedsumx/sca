"""Ops dashboards in Chromium (L143), against a real Cairn API and a real Prometheus (the bundled stack's image,
scraping itself): an operator opens "New ops dashboard" from the command palette, defines a $job variable whose
choices come from Prometheus, makes a PromQL tile with the form and previews it through Cairn, saves the dashboard
and sees real series, changes the time range, exports it for Grafana, and opens the built-in Platform overview and
copies it; someone without VIEW_OPERATIONS is not shown the built-in dashboards and sees a shared ops dashboard's
tiles refused. axe checks the dashboard, the editor and the tile form with real data.

Needs Playwright (with Chromium), the built bundle (ui/dist/mfe, `npm run build:mfe`), axe-core and docker with the
Prometheus image (tests/test_observability_e2e.py)."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time

import httpx
import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "ui", "dist", "mfe")
AXE = os.path.join(ROOT, "ui", "node_modules", "axe-core", "axe.min.js")
PROMETHEUS = "prom/prometheus:v3.15.1"
OPS, BOB = "urn:li:corpuser:ops", "urn:li:corpuser:bob"
EXPR = 'sum by (job) (rate(prometheus_http_requests_total{job="$job"}[$__rate_interval]))'


def _ready():
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    if not (os.path.exists(os.path.join(BUNDLE, "remoteEntry.js")) and os.path.exists(AXE) and shutil.which("docker")):
        return False
    return subprocess.run(["docker", "image", "inspect", PROMETHEUS], capture_output=True, timeout=30).returncode == 0


pytestmark = pytest.mark.skipif(not _ready(), reason="needs Playwright, the built UI bundle, axe-core and the Prometheus image")

HOST = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Cairn</title></head>
<body><div id="root"></div><script src="/ui/remoteEntry.js"></script><script>
(async () => {
  await cairnConsumer.init({});
  const mount = (await cairnConsumer.get('./mount'))().mount;
  mount(document.getElementById('root'), { apiBaseUrl: window.location.origin, catalogUrl: null });
})();
</script></body></html>"""


def until(fn, timeout=30.0):
    end = time.time() + timeout
    while True:
        out = fn()
        if out or time.time() > end:
            return out
        time.sleep(0.3)


@pytest.fixture(scope="module")
def prometheus():

    p = port()
    d = tempfile.mkdtemp(prefix="opsui-")
    with open(f"{d}/prometheus.yml", "w") as fh:
        fh.write("global: {scrape_interval: 2s}\nscrape_configs:\n"
                 "  - job_name: prometheus\n    static_configs: [{targets: ['localhost:9090']}]\n")
    os.chmod(d, 0o755)
    os.chmod(f"{d}/prometheus.yml", 0o644)
    name = f"cairn-opsui-prom-{os.getpid()}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{p}:9090", "-v",
                    f"{d}/prometheus.yml:/etc/prometheus/prometheus.yml:ro", PROMETHEUS,
                    "--config.file=/etc/prometheus/prometheus.yml"], check=True, capture_output=True)
    url = f"http://127.0.0.1:{p}"
    try:
        assert until(lambda: _ok(f"{url}/-/ready"), 120), "Prometheus did not start"
        # Requests to Prometheus itself, so its own request counter has a rate.
        for _ in range(20):
            httpx.get(f"{url}/api/v1/query", params={"query": "up"}, timeout=5)
        assert until(lambda: httpx.get(f"{url}/api/v1/query", params={
            "query": 'sum(rate(prometheus_http_requests_total{job="prometheus"}[1m])) > 0'}, timeout=5)
            .json()["data"]["result"], 90), "no rate yet"
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        shutil.rmtree(d, ignore_errors=True)


def port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ok(url):
    try:
        return httpx.get(url, timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="module")
def cairn(prometheus, tmp_path_factory):
    import uvicorn
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.services import build_services

    tmp = tmp_path_factory.mktemp("opsui")
    keep = {k: os.environ.get(k) for k in ("CAIRN_OBS_PROMETHEUS_URL", "CAIRN_OBS_LOKI_URL", "CAIRN_OBS_TEMPO_URL")}
    os.environ["CAIRN_OBS_PROMETHEUS_URL"] = prometheus
    os.environ.pop("CAIRN_OBS_LOKI_URL", None)
    os.environ.pop("CAIRN_OBS_TEMPO_URL", None)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = os.environ.get("NO_PROXY", "") + ",127.0.0.1"
    graph = InMemoryGraph()
    for urn, name in ((OPS, "Olu Ops"), (BOB, "Bob Stone")):
        graph.aspects[(urn, "corpUserInfo")] = {"active": True, "displayName": name}
    s = build_services(Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="t" * 32,
                                dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")}),
                       graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="ops", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": OPS})]))
    app = create_api_app(s)
    app.mount("/ui", StaticFiles(directory=BUNDLE), name="ui")
    app.add_api_route("/host", lambda: HTMLResponse(HOST), methods=["GET"])
    p = port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=p, timeout_keep_alive=75, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    assert until(lambda: _ok(f"http://127.0.0.1:{p}/health"), 30)
    yield s, f"http://127.0.0.1:{p}"
    server.should_exit = True
    for k, v in keep.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _signed_in(pw, url, who):
    from playwright.sync_api import expect

    browser = pw.chromium.launch()
    ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": who}, viewport={"width": 1440, "height": 1000},
                              accept_downloads=True)
    page = ctx.new_page()
    page.goto(f"{url}/host#/discover", wait_until="domcontentloaded")
    role = page.get_by_role("dialog", name="Welcome to Cairn. What do you mostly do here?")
    expect(role).to_be_visible(timeout=30_000)
    role.get_by_role("radio", name=re.compile("^Admin")).check()
    role.get_by_role("button", name="Continue").click()
    expect(role).to_be_hidden(timeout=20_000)
    return browser, page


def audit(page) -> list[dict]:
    """axe's serious and critical WCAG 2.1 A/AA violations. A drop-down list whose box is closed (its combobox says
    aria-expanded="false") is not on screen; this host page loads no stylesheet, so antd's closing animation never ends
    and the closed list stays laid out: it is left out."""
    page.wait_for_timeout(400)
    page.add_script_tag(path=AXE)
    result = page.evaluate("""async () => {
        for (const d of document.querySelectorAll('.ant-select-dropdown')) {
            const list = d.querySelector('[role=listbox]');
            const box = list && document.querySelector(`[aria-controls="${list.id}"]`);
            if (!box || box.getAttribute('aria-expanded') !== 'true') d.setAttribute('data-closed-list', '');
        }
        const r = await axe.run({include: [document], exclude: [['[data-closed-list]']]},
                                {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']},
                                 resultTypes: ['violations']});
        return r.violations.map(v => ({id: v.id, impact: v.impact, help: v.help,
                                       nodes: v.nodes.slice(0, 5).map(n => n.target.join(' ') + ' :: ' + n.failureSummary)}));
    }""")
    return [v for v in result if v["impact"] in ("serious", "critical")]


def test_an_operator_builds_an_ops_dashboard_in_the_browser(cairn):
    from playwright.sync_api import expect, sync_playwright

    s, url = cairn
    found: dict[str, list] = {}
    with sync_playwright() as pw:
        browser, page = _signed_in(pw, url, OPS)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            # ---- from the command palette ------------------------------------------------------------------------
            page.keyboard.press("Control+k")       # the header's one search (L150) finds the command
            page.get_by_role("combobox", name="Search data or jump to a page").fill("New ops dashboard")
            page.get_by_role("option", name=re.compile("New ops dashboard")).first.click()
            editor = page.get_by_role("dialog", name="New ops dashboard")
            expect(editor).to_be_visible(timeout=20_000)
            editor.get_by_label("Dashboard name").fill("Prometheus itself")
            editor.get_by_role("button", name="Add variable").click()
            editor.get_by_label("Variable 1 name").fill("job")
            editor.get_by_label("Variable 1 label name").fill("job")
            editor.get_by_label("Variable 1 default").fill("prometheus")
            found["editor"] = audit(page)
            editor.get_by_role("button", name="Add tile").click()
            tile = page.get_by_role("dialog", name="New ops tile")
            expect(tile).to_be_visible(timeout=10_000)
            tile.get_by_label("Tile title").fill("HTTP requests / s")
            tile.get_by_label("Query 1").fill(EXPR)
            tile.get_by_label("Legend 1").fill("{{job}}")
            tile.get_by_role("combobox", name="Unit").click()
            page.locator(".ant-select-dropdown:visible .ant-select-item-option", has_text="Requests / s").first.click()
            tile.get_by_label("Tile title").click()            # the list closes
            tile.get_by_role("button", name="Preview").click()
            preview = tile.get_by_role("region", name="Preview")
            expect(preview.get_by_role("img", name="HTTP requests / s: time series of 1 series")).to_be_visible(timeout=30_000)
            expect(preview.get_by_role("list", name="HTTP requests / s: legend")).to_contain_text("prometheus")
            found["tile form"] = audit(page)
            tile.get_by_role("button", name="Add tile").click()
            expect(tile).to_be_hidden(timeout=10_000)
            editor.get_by_role("button", name="Create").click()
            expect(editor).to_be_hidden(timeout=20_000)
            board = until(lambda: next((d for d in s.sql_boards.dashboards(OPS) if d["name"] == "Prometheus itself"), None))
            assert board and board["tiles"][0]["promql"]["queries"][0]["expr"] == EXPR
            assert board["tiles"][0]["display"] == {"unit": "reqps"}
            assert [c["kind"] for c in board["controls"]] == ["time", "variable"]
            # ---- the saved dashboard, with real series --------------------------------------------------------------
            expect(page.get_by_role("img", name="HTTP requests / s: time series of 1 series")).to_be_visible(timeout=30_000)
            expect(page.get_by_role("list", name="HTTP requests / s: legend")).to_contain_text("req/s")
            page.get_by_role("combobox", name="Time range").click()
            page.locator(".ant-select-dropdown:visible .ant-select-item-option", has_text="Last 6h").first.click()
            expect(page.get_by_role("img", name="HTTP requests / s: time series of 1 series")).to_be_visible(timeout=30_000)
            page.get_by_role("combobox", name="job").click()       # its choices come from Prometheus
            choice = page.locator(".ant-select-dropdown:visible .ant-select-item-option", has_text="prometheus")
            expect(choice).to_be_visible(timeout=20_000)
            page.keyboard.press("Escape")
            found["ops dashboard"] = audit(page)
            with page.expect_download() as dl:
                page.get_by_role("button", name="Export to Grafana").click()
            doc = json.loads(open(dl.value.path()).read())
            assert doc["panels"][0]["targets"][0]["expr"] == EXPR and doc["templating"]["list"][-1]["name"] == "job"
            # ---- the built-in Platform overview, copied ----------------------------------------------------------
            page.get_by_role("combobox", name="Dashboard").click()
            page.locator(".ant-select-dropdown:visible .ant-select-item-option", has_text="Platform overview (built-in)").first.click()
            expect(page.get_by_role("button", name="Copy to edit")).to_be_visible(timeout=20_000)
            expect(page.get_by_text("Requests / s by service")).to_be_visible(timeout=30_000)
            found["built-in"] = audit(page)
            page.get_by_role("button", name="Copy to edit").click()
            assert until(lambda: any(d["name"] == "Platform overview (copy)" and d["owner"] == OPS
                                     for d in s.sql_boards.dashboards(OPS)))
            expect(page.get_by_role("button", name="Edit")).to_be_visible(timeout=20_000)
        finally:
            browser.close()
    assert not errors, errors
    assert not any(found.values()), "axe found serious or critical problems:\n" + json.dumps(found, indent=1)


def test_without_view_operations_ops_tiles_are_refused_in_the_browser(cairn):
    from playwright.sync_api import expect, sync_playwright

    s, url = cairn
    board = s.sql_boards.save_dashboard(OPS, {"name": "Shared ops", "shared": True, "tiles": [
        {"title": "Requests", "promql": {"queries": [{"expr": "sum(rate(prometheus_http_requests_total[5m]))"}]}}]})
    with sync_playwright() as pw:
        browser, page = _signed_in(pw, url, BOB)
        try:
            page.goto(f"{url}/host#/dashboards?open={board['id']}", wait_until="domcontentloaded")
            expect(page.get_by_text(re.compile("ops tiles need VIEW_OPERATIONS"))).to_be_visible(timeout=30_000)
            page.get_by_role("combobox", name="Dashboard").click()
            options = page.locator(".ant-select-dropdown:visible .ant-select-item-option")
            expect(options.filter(has_text="Shared ops")).to_be_visible(timeout=20_000)
            expect(options.filter(has_text="(built-in)")).to_have_count(0)
        finally:
            browser.close()
