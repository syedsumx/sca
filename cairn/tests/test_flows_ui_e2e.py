"""The Flows canvas (L123) in Chromium against a real Cairn API: the built UI bundle (ui/dist/mfe, `npm run build:mfe`)
mounted the way Cairn Core's host mounts it. A flow is made from the template gallery, steps are added from the
palette and linked by dragging from one step's handle onto another with the mouse, a step's form is filled in,
the draft is saved and published (after the diff), and a run shows each step's status on the canvas while its
SQL runs on a real database as the signed-in owner. The Code tab shows the same flow as YAML.

Needs Playwright (with Chromium) and the built bundle."""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "ui", "dist", "mfe")
ANA = "urn:li:corpuser:ana"


def _ready():
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(os.path.join(BUNDLE, "remoteEntry.js"))


pytestmark = pytest.mark.skipif(not _ready(), reason="needs Playwright and the built UI bundle (npm run build:mfe)")

HOST = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cairn</title></head>
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
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="all", effect="allow", actions=["*"]),
                                            Policy(id="no-ops", effect="deny", actions=["MANAGE_SYSTEM_OPERATIONS"])]))
    graph.aspects[("urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)", "schemaMetadata")] = {
        "fields": [{"fieldPath": c, "nativeDataType": "TEXT"} for c in ("id", "region", "amount")]}
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
    yield s, f"http://127.0.0.1:{p}"
    server.should_exit = True


def _center(locator):
    box = locator.bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def test_build_save_publish_and_run_a_flow_on_the_canvas(cairn):
    from playwright.sync_api import expect, sync_playwright

    s, url = cairn
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": ANA}, viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(f"{url}/host#/flows", wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="Flows")).to_be_visible(timeout=30_000)
            # A draft from the gallery.
            page.get_by_role("button", name="New flow").first.click()
            page.get_by_role("radio", name="Blank flow").click()
            page.get_by_label("Name (lowercase letters, digits, - and _)").fill("orders-check")
            page.get_by_role("button", name="Create draft").click()
            expect(page.get_by_role("heading", name="orders-check")).to_be_visible(timeout=20_000)
            start = page.locator('[data-step="start"]')
            expect(start).to_be_visible()

            # Select the first step, add an SQL step from the palette (it comes after the selected one), fill its form.
            start.click()
            page.get_by_role("button", name="Add SQL query step").click()
            sql = page.locator('[data-step="sql-1"]')
            expect(sql).to_be_visible()
            side = page.get_by_role("complementary", name="Step sql-1")
            side.get_by_role("textbox", name="SQL", exact=True).fill("SELECT count(*) AS n FROM main.orders")
            side.get_by_role("textbox", name="Connection", exact=True).fill("wh")
            # A notify step on its own, then linked after the SQL step by dragging between handles with the mouse.
            page.keyboard.press("Escape")
            page.get_by_test_id("flow-canvas").click(position={"x": 30, "y": 30})
            page.get_by_role("button", name="Add Notify step").click()
            note = page.locator('[data-step="notify-1"]')
            expect(note).to_be_visible()
            page.get_by_role("complementary", name="Step notify-1").get_by_role("textbox", name="Message *").fill(
                "orders: {{ steps.sql-1.preview[0][0] }}")
            page.get_by_role("button", name="Auto-layout").click()
            hx, hy = _center(page.locator('[data-handle="sql-1"]'))
            nx, ny = _center(note)
            page.mouse.move(hx, hy)
            page.mouse.down()
            page.mouse.move((hx + nx) / 2, (hy + ny) / 2, steps=5)
            page.mouse.move(nx, ny, steps=5)
            page.mouse.up()
            expect(page.locator('[data-edge="sql-1->notify-1"]')).to_have_count(1)
            expect(page.locator('[data-edge="start->sql-1"]')).to_have_count(1)
            # Dragging a step moves it (and keeps it on the grid).
            before = note.bounding_box()
            nx, ny = _center(note)
            page.mouse.move(nx, ny)
            page.mouse.down()
            page.mouse.move(nx, ny + 120, steps=6)
            page.mouse.up()
            assert note.bounding_box()["y"] > before["y"] + 60
            page.get_by_test_id("flow-canvas").click(position={"x": 12, "y": 12})   # nothing selected: the step list
            expect(page.get_by_text("No problems.")).to_be_visible()

            # The Code tab shows the same flow; then save, publish (the diff first) and run.
            page.get_by_role("tab", name="Code").click()
            yaml_text = page.get_by_label("Flow definition (YAML)").input_value()
            assert "after: [sql-1]" in yaml_text and "name: notify-1" in yaml_text and "layout:" in yaml_text
            page.get_by_role("tab", name="Canvas").click()
            page.get_by_role("button", name="Save draft").click()
            expect(page.get_by_text("Draft saved")).to_be_visible(timeout=20_000)
            page.get_by_role("button", name="Publish…").click()
            dialog = page.get_by_role("dialog", name="Publish orders-check")
            expect(dialog.get_by_text("+ step sql-1 added")).to_be_visible()
            dialog.get_by_label("Note").fill("first version")
            dialog.get_by_role("button", name="Publish").click()
            expect(page.get_by_text("Published version 1")).to_be_visible(timeout=20_000)
            page.get_by_role("button", name="Run", exact=True).click()
            expect(page.locator('.cairn-status[data-state="succeeded"]').first).to_be_visible(timeout=60_000)   # StatusBadge (L150)
            for name in ("start", "sql-1", "notify-1"):            # each step coloured by its status
                expect(page.locator(f'[data-step="{name}"].st-ok')).to_have_count(1)
            # A step's output, as the run kept it.
            page.locator('[data-step="sql-1"]').click()
            expect(page.get_by_role("region", name="Run of step sql-1").locator("pre")).to_contain_text('"rows": 1')
            # Dark theme, from the profile menu (L150: one theme setting for the workspace).
            page.get_by_role("button", name="Your profile and settings").click()
            page.get_by_role("menuitem", name="Dark").click()
            expect(page.locator('[data-theme="dark"]')).to_have_count(1)
        finally:
            browser.close()
    assert not errors, errors
    flow = s.flows.get(ANA, "orders-check")
    assert flow["published_version"] == 1
    run = s.flows.runs(ANA, "orders-check")[0]
    detail = s.flows.run_detail(ANA, run["id"])
    by = {x["key"]: x for x in detail["steps"]}
    assert detail["status"] == "succeeded" and by["sql-1"]["output"]["preview"] == [[3]]
    assert by["notify-1"]["status"] == "ok"
    assert any(n["type"] == "flow.notice" and n["body"] == "orders: 3" for n in s.notify.inbox(ANA))
