"""Accessibility (L67): the main pages pass axe (WCAG 2.1 A and AA rules) with no serious or critical violations.

Each page is loaded in Chromium from the built UI bundle (ui/dist/mfe), mounted the way Cairn Core's host mounts
it, against a real Cairn API with a catalog, a warehouse and data in it, so the pages are checked as people see
them, not empty. axe-core comes from ui/node_modules (a pinned devDependency).

Needs Playwright (with Chromium), the built bundle and ui/node_modules/axe-core."""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "ui", "dist", "mfe")
AXE = os.path.join(ROOT, "ui", "node_modules", "axe-core", "axe.min.js")
ANA = "urn:li:corpuser:ana"
DS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"
PAGES = ["/discover", "/search?q=orders", f"/asset/{DS}", "/marketplace", "/requests", "/assistant", "/context",
         "/governance", "/reports", "/pipelines", "/sql", "/sql/workflows", "/sql/workbooks", "/sql/input-tables",
         "/sql/metrics", "/sql/git", "/queries", "/dashboards", "/profile", "/settings", "/extensions", "/flows",
         "/flows?flow=a11y-flow", "/usage?tab=budgets", "/compute?tab=jobs", "/device?code=BCDF-GHJK",
         # Without a compiler here; tests/test_ontology_ui_e2e.py runs axe on the same pages with a compiled ontology.
         "/ontology", "/ontology?tab=review", "/ontology?tab=compile",
         # People & roles (L139): the directory, a person's page open, groups, roles, service accounts, the access check.
         "/people", "/people?person=ana", "/people?tab=groups", "/people?tab=roles", "/people?tab=principals",
         "/people?tab=access&check=ana"]
# Forms (L127): a hub's create form open, with a picker, a cron field, Advanced shown and a field's problem shown.
FORMS = [("/compute?tab=saved-queries", "New saved query"), ("/compute?tab=jobs", "New job"),
         ("/trust?tab=secret-scopes", "Create scope"), ("/people", "New person")]
BLOCKING = {"serious", "critical"}


def _ready():
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(os.path.join(BUNDLE, "remoteEntry.js")) and os.path.exists(AXE)


pytestmark = pytest.mark.skipif(not _ready(), reason="needs Playwright, the built UI bundle and ui/node_modules/axe-core")

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


@pytest.fixture
def cairn(settings, graph, tmp_path, monkeypatch):
    import uvicorn
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
    from sqlalchemy import create_engine, text

    from cairn.app import create_api_app
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.services import build_services

    wh = tmp_path / "wh.db"
    with create_engine(f"sqlite:///{wh}").begin() as c:
        c.execute(text("CREATE TABLE orders (id INTEGER, region TEXT, amount REAL)"))
        c.execute(text("INSERT INTO orders VALUES (1, 'EU', 9.5), (2, 'US', 3.0), (3, 'EU', 7.0)"))
    conns = tmp_path / "connections.yaml"
    conns.write_text(f"connections:\n  wh:\n    url: sqlite:///{wh}\n    catalog:\n      platform: sqlite\n")
    monkeypatch.setenv("CAIRN_QUALITY_CONNECTIONS", str(conns))
    graph.aspects[(DS, "schemaMetadata")] = {"fields": [{"fieldPath": c, "nativeDataType": "TEXT"}
                                                        for c in ("id", "region", "amount")]}
    graph.aspects[(DS, "datasetProperties")] = {"name": "orders", "description": "One row per order."}
    graph.aspects[(ANA, "corpUserInfo")] = {"active": True, "displayName": "Ana Lima", "departmentName": "Sales"}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="all", effect="allow", actions=["*"])]))
    s.sql_workspace.save(ANA, {"name": "Revenue by region", "connection": "wh",
                               "sql": "SELECT region, sum(amount) AS total FROM main.orders GROUP BY region"})
    s.flows.create(ANA, {"name": "a11y-flow", "steps": [          # the canvas editor, with a branch and an approval
        {"name": "count", "type": "sql", "sql": "SELECT count(*) AS n FROM main.orders", "connection": "wh"},
        {"name": "big", "type": "branch", "after": ["count"], "condition": "steps.count.rows > 0", "then": ["ask"]},
        {"name": "ask", "type": "approval", "after": ["big"], "approvers": [ANA]}]}, publish=True)
    app = create_api_app(s)
    app.mount("/ui", StaticFiles(directory=BUNDLE), name="ui")
    app.add_api_route("/host", lambda: HTMLResponse(HOST), methods=["GET"])
    p = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=p, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    end = time.time() + 30
    while True:
        try:
            socket.create_connection(("127.0.0.1", p), 0.3).close()
            break
        except OSError:
            assert time.time() < end
            time.sleep(0.1)
    yield f"http://127.0.0.1:{p}"
    server.should_exit = True


def audit(page) -> list[dict]:
    """axe's WCAG 2.1 A/AA violations on the page as it stands, serious and critical ones."""
    page.add_script_tag(path=AXE)
    result = page.evaluate("""async () => {
        const r = await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']},
                                           resultTypes: ['violations']});
        return r.violations.map(v => ({id: v.id, impact: v.impact, help: v.help,
                                       nodes: v.nodes.slice(0, 5).map(n => n.target.join(' ') + ' :: ' + n.failureSummary)}));
    }""")
    return [v for v in result if v["impact"] in BLOCKING]


def test_the_main_pages_pass_axe(cairn):
    from playwright.sync_api import sync_playwright

    found: dict[str, list[dict]] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": ANA}, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            for path in PAGES:
                page.goto(f"{cairn}/host#{path}", wait_until="networkidle")
                page.wait_for_function("document.querySelector('#root') && document.querySelector('#root').innerText.length > 20",
                                       timeout=20_000)
                page.wait_for_load_state("networkidle")
                bad = audit(page)
                if bad:
                    found[path] = bad
            for path, button in FORMS:
                page.goto(f"{cairn}/host#{path}", wait_until="networkidle")
                page.get_by_role("button", name=button, exact=True).first.click()
                dialog = page.get_by_role("dialog").last
                dialog.wait_for()
                more = dialog.get_by_role("button", name=re.compile("Advanced options"))
                if more.count():
                    more.click()
                dialog.get_by_role("button", name=button, exact=True).last.click()     # empty: required fields say so
                dialog.get_by_role("alert").first.wait_for()
                bad = audit(page)
                if bad:
                    found[f"{path} ({button})"] = bad
                page.keyboard.press("Escape")
        finally:
            browser.close()
    assert not errors, errors
    assert not found, "axe found serious or critical problems:\n" + json.dumps(found, indent=1)


def test_the_guided_experience_passes_axe(cairn):
    """L128: the first-run role question, "Get started", the command palette open with results, the help panel open
    and the keyboard shortcuts, each checked by axe as people see them."""
    from playwright.sync_api import expect, sync_playwright

    found: dict[str, list[dict]] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": ANA}, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def check(name):
            page.wait_for_load_state("networkidle")
            bad = audit(page)
            if bad:
                found[name] = bad
        try:
            page.goto(f"{cairn}/host#/discover", wait_until="networkidle")
            role = page.get_by_role("dialog", name="Welcome to Cairn. What do you mostly do here?")
            expect(role).to_be_visible(timeout=20_000)
            check("role question")
            role.get_by_role("radio", name=re.compile("^Analyst")).check()
            role.get_by_role("button", name="Continue").click()
            page.goto(f"{cairn}/host#/get-started", wait_until="networkidle")
            expect(page.get_by_text(re.compile(r"\d of 5 done"))).to_be_visible(timeout=20_000)
            check("get started")
            page.keyboard.press("Control+k")
            box = page.get_by_role("combobox", name="Type a page, an action or a dataset…")
            box.fill("sql")
            expect(page.get_by_role("option").first).to_be_visible()
            box.press("ArrowDown")
            check("command palette")
            box.press("Escape")
            page.goto(f"{cairn}/host#/trust?tab=secret-scopes", wait_until="networkidle")
            page.get_by_role("button", name="Help for this page").first.click()
            expect(page.locator(".ant-drawer-content").last.get_by_role("heading", name="Secret scopes")).to_be_visible(
                timeout=10_000)
            check("help panel")
            page.keyboard.press("Escape")
            page.locator("main").click(position={"x": 5, "y": 5})
            page.keyboard.press("?")
            expect(page.get_by_role("dialog", name="Keyboard shortcuts")).to_be_visible()
            check("keyboard shortcuts")
        finally:
            browser.close()
    assert not errors, errors
    assert not found, "axe found serious or critical problems:\n" + json.dumps(found, indent=1)
