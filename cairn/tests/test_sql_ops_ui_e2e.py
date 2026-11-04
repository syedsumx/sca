"""The SQL workflow, git folder and query insight pages in Chromium, against a real Cairn API: the built UI
bundle (ui/dist/mfe, `npm run build:mfe`) is mounted the way Cairn Core's host mounts it, workflows run their
real steps on a real database, git folders pull from and push to a real git repository, and query history comes
in through the endpoint Trino's event listener posts to.

Needs Playwright (with Chromium), git, and the built bundle."""

import json
import os
import shutil
import socket
import subprocess
import threading
import time

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "ui", "dist", "mfe")
ANA = "urn:li:corpuser:ana"
KEY = "t" * 32


def _ready():
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(os.path.join(BUNDLE, "remoteEntry.js")) and shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _ready(), reason="needs Playwright, git and the built UI bundle (npm run build:mfe)")

# The host page: loads the remote as Cairn Core's MFE host does and mounts it, API on the same origin.
HOST = """<!doctype html><html><head><meta charset="utf-8"><title>Cairn</title></head>
<body><div id="root"></div><script src="/ui/remoteEntry.js"></script><script>
(async () => {
  await cairnConsumer.init({});
  const mount = (await cairnConsumer.get('./mount'))().mount;
  mount(document.getElementById('root'), { apiBaseUrl: window.location.origin, catalogUrl: null });
})();
</script></body></html>"""


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@x", "GIT_COMMITTER_NAME": "dev",
                        "GIT_COMMITTER_EMAIL": "dev@x"})


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def trino_event(qid, user, sql, wall_s, queued_s, read_bytes, read_rows, out_rows):
    """What Trino's HTTP event listener posts for a finished query (the fields Cairn reads)."""
    return {"createTime": "2026-11-03T09:00:00.000Z", "endTime": "2026-11-03T09:00:42.000Z",
            "metadata": {"queryId": qid, "queryState": "FINISHED", "query": sql},
            "context": {"user": user, "source": "cairn-sql", "serverAddress": "trino"},
            "statistics": {"wallTime": float(wall_s), "cpuTime": 3.5, "queuedTime": float(queued_s),   # seconds
                           "physicalInputBytes": read_bytes, "physicalInputRows": read_rows, "outputRows": out_rows,
                           "peakUserMemoryBytes": 3 * 1024 ** 2, "completedSplits": 12},
            "ioMetadata": {"inputs": [{"catalogName": "lakehouse", "schema": "sales", "table": "orders"}]}}


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
    bare, dev = tmp_path / "origin.git", tmp_path / "dev"
    git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(bare))
    git(tmp_path, "clone", "--quiet", str(bare), str(dev))
    (dev / "sql").mkdir()
    (dev / "sql" / "revenue.sql").write_text("-- connection: wh\n-- description: Revenue by region\n"
                                              "SELECT region, sum(amount) AS total FROM main.orders GROUP BY region\n")
    git(dev, "add", "-A")
    git(dev, "commit", "--quiet", "-m", "queries")
    git(dev, "push", "--quiet", "origin", "main")
    monkeypatch.setenv("CAIRN_QUALITY_CONNECTIONS", str(conns))
    monkeypatch.setenv("CAIRN_GIT_FOLDER_URLS", "file://")
    monkeypatch.setenv("CAIRN_GIT_FOLDERS_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("CAIRN_TRINO_ACCESS_KEY", KEY)
    monkeypatch.setenv("CAIRN_TRINO_CATALOGS", '{"lakehouse": {"platform": "iceberg", "database": ""}}')
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    # Ana may do what a person does, but is no operator: she sees her own queries only.
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="all", effect="allow", actions=["*"]),
                                            Policy(id="no-ops", effect="deny", actions=["MANAGE_SYSTEM_OPERATIONS"])]))
    graph.aspects[("urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)", "schemaMetadata")] = {
        "fields": [{"fieldPath": c, "nativeDataType": "TEXT"} for c in ("id", "region", "amount")]}
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
    yield s, f"http://127.0.0.1:{p}", bare, dev
    server.should_exit = True


def _browser(pw, url):
    browser = pw.chromium.launch()
    ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": ANA}, viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    return browser, page, errors


def test_workflows_git_folders_and_query_insight_in_the_browser(cairn):
    from playwright.sync_api import expect, sync_playwright

    s, url, bare, dev = cairn
    ana = {"X-Cairn-Actor": ANA}
    # Saved queries for the workflow: a check that cannot hold, and a report after it.
    r = httpx.post(f"{url}/sql/saved", headers=ana, json={"name": "EU orders", "connection": "wh",
                                                          "sql": "SELECT count(*) AS n FROM main.orders WHERE region = 'EU'"})
    assert r.status_code in (200, 201), r.text
    eu = r.json()["id"]
    # Trino's event listener reports two of Ana's queries and one of Bob's.
    slow = "SELECT * FROM lakehouse.sales.orders ORDER BY amount"
    for e in (trino_event("q_slow", "ana", slow, 42, 20, 6 * 1024 ** 3, 9_000_000, 9_000_000),
              trino_event("q_fast", "ana", "SELECT region FROM lakehouse.sales.orders LIMIT 5", 1, 0, 1024, 5, 5),
              trino_event("q_bob", "bob", "SELECT 'bob only' FROM lakehouse.sales.orders", 1, 0, 10, 1, 1)):
        assert httpx.post(f"{url}/v1/trino/{KEY}/queries", json=e).status_code == 200

    with sync_playwright() as pw:
        browser, page, errors = _browser(pw, url)
        try:
            # ---- SQL workflows: made in the editor, run now, each step's outcome shown.
            page.goto(f"{url}/host#/sql/workflows", wait_until="domcontentloaded")
            page.get_by_role("button", name="New workflow").click()
            dialog = page.get_by_role("dialog")
            dialog.get_by_label("Workflow name").fill("EU check")
            dialog.get_by_label("Step name").fill("enough")
            dialog.get_by_role("combobox", name="enough type").click()
            page.get_by_text("Check", exact=True).click()
            dialog.get_by_role("combobox", name="enough saved query").click()
            page.locator(".ant-select-item-option", has_text="EU orders").click()
            dialog.get_by_label("enough measure").fill("n")
            dialog.get_by_label("enough value").fill("5")
            dialog.get_by_role("button", name="Add a step").click()
            dialog.get_by_label("Step name").nth(1).fill("report")
            dialog.get_by_role("combobox", name="report saved query").click()
            page.locator(".ant-select-item-option", has_text="EU orders").last.click()
            dialog.get_by_role("combobox", name="report after").click()
            page.locator(".ant-select-item-option", has_text="enough").last.click()
            page.keyboard.press("Escape")
            dialog.get_by_role("button", name="Save").click()
            expect(page.get_by_text("enough → report")).to_be_visible(timeout=20_000)
            wf = s.sql_workflows.list(ANA)
            assert [(x["name"], [st["name"] for st in x["steps"]]) for x in wf] == [("EU check", ["enough", "report"])]
            assert wf[0]["steps"][0] == {**wf[0]["steps"][0], "type": "check", "measure": "n", "op": ">", "value": 5.0}
            assert wf[0]["steps"][1]["after"] == ["enough"] and wf[0]["steps"][0]["query"] == eu
            page.get_by_role("button", name="Run now").click()
            expect(page.get_by_text("n is 2", exact=False)).to_be_visible(timeout=30_000)     # the check failed: 2 EU orders
            expect(page.get_by_text("enough did not succeed")).to_be_visible()               # so the report was skipped
            [run] = s.sql_workflows.runs(ANA, wf[0]["id"])
            assert run["status"] == "failed" and [x["status"] for x in run["steps"]] == ["error", "skipped"]
            told = [n for n in s.notify.inbox(ANA) if n["type"] == "sql.workflow_failed"]   # the owner is told
            assert told and "EU check" in told[0]["title"], s.notify.inbox(ANA)

            # ---- Git folders: linked, pulled from git, a change pushed back on a new branch.
            page.goto(f"{url}/host#/sql/git", wait_until="domcontentloaded")
            page.get_by_role("button", name="Link a folder").click()
            link = page.get_by_role("dialog")
            link.get_by_label("Folder name").fill("finance")
            link.get_by_role("combobox", name="Source").click()
            page.get_by_text("A repository URL your operator allows").click()
            link.get_by_label("URL").fill(f"file://{bare}")
            link.get_by_label("Path").fill("sql")
            link.get_by_role("button", name="Link").click()
            expect(page.get_by_text(f"file://{bare}")).to_be_visible(timeout=20_000)
            page.get_by_role("button", name="Pull").click()
            expect(page.get_by_text("New queries:")).to_be_visible(timeout=30_000)
            expect(page.get_by_text("revenue.sql")).to_be_visible()
            revenue = next(q for q in s.sql_workspace.saved(ANA) if q["name"] == "revenue")
            assert "sum(amount)" in revenue["sql"] and revenue["connection"] == "wh"
            s.sql_workspace.save(ANA, {**revenue, "description": "Revenue by region, edited in Cairn"}, revenue["id"])
            page.get_by_role("button", name="Push").click()
            push = page.get_by_role("dialog")
            push.get_by_label("Commit message").fill("Describe revenue")
            push.get_by_role("button", name="Push").click()
            expect(page.get_by_text("pushed to cairn/")).to_be_visible(timeout=30_000)
            branches = subprocess.run(["git", "branch", "--list", "cairn/*"], cwd=bare, capture_output=True, text=True).stdout
            assert branches.strip(), "the push made no branch"
            branch = branches.split()[-1]
            pushed = subprocess.run(["git", "show", f"{branch}:sql/revenue.sql"], cwd=bare, capture_output=True, text=True).stdout
            assert "edited in Cairn" in pushed
            log = subprocess.run(["git", "log", "-1", "--format=%s", branch], cwd=bare, capture_output=True, text=True).stdout
            assert log.strip() == "Describe revenue"
            main = subprocess.run(["git", "show", "main:sql/revenue.sql"], cwd=bare, capture_output=True, text=True).stdout
            assert "edited in Cairn" not in main                                              # never the folder's own branch

            # ---- Query insight: Ana's queries only, the slowest first, a profile with its hints.
            page.goto(f"{url}/host#/queries", wait_until="domcontentloaded")
            expect(page.get_by_text("SELECT * FROM lakehouse.sales.orders ORDER BY amount")).to_be_visible(timeout=20_000)
            expect(page.get_by_text("SELECT region FROM lakehouse.sales.orders LIMIT 5")).to_be_visible()
            assert page.get_by_text("bob only").count() == 0                                   # not hers
            page.get_by_role("checkbox", name="Slowest first").check()
            first = page.locator(".ant-table-tbody tr").first
            expect(first).to_contain_text("ORDER BY amount", timeout=20_000)
            first.click()
            drawer = page.locator(".ant-drawer")
            expect(drawer.get_by_text("Waited 20 s of 42 s in the queue")).to_be_visible(timeout=20_000)
            expect(drawer.get_by_text("SELECT * reads every column")).to_be_visible()
            expect(drawer.get_by_text("6.0 GB")).to_be_visible()
            expect(drawer.get_by_text("42.0 s")).to_be_visible()
            shots = os.environ.get("CAIRN_E2E_SCREENSHOT_DIR")
            if shots:
                page.screenshot(path=os.path.join(shots, "query-insight.png"))
        finally:
            browser.close()
    assert not [e for e in errors if "ResizeObserver" not in e], errors
    print("\n" + json.dumps({"workflow_run": run["status"], "pushed_branch": branch}))


def test_the_sql_editor_suggests_what_the_person_may_query(cairn):
    """Autocomplete in the SQL editor, in Chromium against Cairn: tables and columns come from what the signed-in
    person may query (a table denied to her is never suggested), and a query built from suggestions runs."""
    from playwright.sync_api import sync_playwright

    from cairn.governance.abac import Policy, PolicyBundle, Target

    s, url, _, _ = cairn
    secret = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.secrets,PROD)"
    s.graph.aspects[(secret, "schemaMetadata")] = {"fields": [{"fieldPath": "salary", "nativeDataType": "REAL"}]}
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="all", effect="allow", actions=["*"]),
        Policy(id="no-ops", effect="deny", actions=["MANAGE_SYSTEM_OPERATIONS"]),
        Policy(id="no-secrets", effect="deny", actions=["VIEW_ENTITY_PAGE"], target=Target(resource_urns=[secret]))]))
    with sync_playwright() as pw:
        browser, page, errors = _browser(pw, url)
        try:
            page.goto(f"{url}/host#/sql", wait_until="domcontentloaded")
            page.get_by_text("main.orders", exact=True).wait_for(timeout=30_000)
            box = page.get_by_label("SQL", exact=True)
            box.click()
            page.keyboard.type("SELECT  FROM main.")
            options = page.get_by_role("listbox", name="Suggestions").get_by_role("option")
            options.first.wait_for()
            assert [o.split("\n")[0] for o in options.all_inner_texts()] == ["orders"], options.all_inner_texts()
            page.keyboard.press("Enter")
            assert box.input_value() == "SELECT  FROM main.orders"
            # Back to the select list: the columns of the table read.
            assert box.evaluate("e => e.getBoundingClientRect().width") > 600      # the editor fills its panel
            page.keyboard.press("Control+Home")
            for _ in range(7):
                page.keyboard.press("ArrowRight")
            page.keyboard.press("Control+Space")
            options.first.wait_for()
            assert [o.split("\n")[0] for o in options.all_inner_texts()] == ["amount", "id", "region"]
            page.keyboard.type("reg")
            page.wait_for_function("() => (document.querySelector('[role=option]') || {}).textContent?.startsWith('region')")
            assert any(o.startswith("regexp_like") for o in options.all_inner_texts())   # functions after the column
            page.keyboard.press("Tab")
            assert box.input_value() == "SELECT region FROM main.orders"
            page.keyboard.press("Control+Enter")
            page.get_by_text("3 row(s)", exact=False).wait_for(timeout=30_000)
            assert page.get_by_role("cell", name="EU").count() == 2
            assert errors == [], errors
        finally:
            browser.close()
