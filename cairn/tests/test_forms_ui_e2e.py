"""Friendly forms (L127) in Chromium, against a real Cairn API: resources in four hubs are created, changed and
deleted with the forms alone (no JSON typed), pickers choose from what the API lists, a field shows its problem (the
form's own check, and the API's refusal placed on the field), and a person without the right sees a sentence that
says so, not raw API text.

The built UI bundle (ui/dist/mfe, `npm run build:mfe`) is mounted the way Cairn Core's host mounts it. Needs
Playwright (with Chromium) and the built bundle."""

import base64
import os
import re
import socket
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "ui", "dist", "mfe")
ANA = "urn:li:corpuser:ana"          # an operator
BOB = "urn:li:corpuser:bob"          # may do what a person does, but is no operator
DS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"


def _ready():
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(os.path.join(BUNDLE, "remoteEntry.js"))


pytestmark = pytest.mark.skipif(not _ready(), reason="needs Playwright and the built UI bundle (npm run build:mfe)")

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
        c.execute(text("INSERT INTO orders VALUES (1, 'EU', 9.5), (2, 'US', 3.0)"))
    conns = tmp_path / "connections.yaml"
    conns.write_text(f"connections:\n  wh:\n    url: sqlite:///{wh}\n    catalog:\n      platform: sqlite\n")
    monkeypatch.setenv("CAIRN_QUALITY_CONNECTIONS", str(conns))
    monkeypatch.setenv("CAIRN_SECRETS_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("CAIRN_JOBS_RUNTIME", "docker")       # jobs are defined here, not run
    # A team warehouse (its proxy is not running here: listed as unreachable, which the picker does not mind).
    monkeypatch.setenv("CAIRN_WAREHOUSES", '{"warehouses": [{"name": "sales"}]}')
    monkeypatch.setenv("CAIRN_WAREHOUSES_URL", f"http://127.0.0.1:{_port()}")
    monkeypatch.setenv("CAIRN_WAREHOUSE_PORTS", f'{{"sales": {_port()}}}')
    monkeypatch.setenv("CAIRN_GIT_FOLDER_URLS", "file://")                 # git folders on local bare repositories
    monkeypatch.setenv("CAIRN_GIT_FOLDERS_DIR", str(tmp_path / "git-work"))
    if os.environ.get("CAIRN_CATALOG_BIN"):     # external catalogs ride on Trino's access control (no Trino needed here)
        monkeypatch.setenv("CAIRN_TRINO_ACCESS_KEY", "forms-e2e-trino-key-" + "k" * 16)
        monkeypatch.setenv("CAIRN_TRINO_CATALOGS", '{"lakehouse": {"platform": "iceberg", "database": ""}}')
    graph.aspects[(DS, "schemaMetadata")] = {"fields": [{"fieldPath": c, "nativeDataType": "TEXT"}
                                                        for c in ("id", "region", "amount")]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="all", effect="allow", actions=["*"]),
        Policy(id="bob-no-ops", effect="deny", actions=["MANAGE_SYSTEM_OPERATIONS"],
               condition={"attr": "subject.urn", "op": "eq", "value": BOB})]))
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


def _api(url, actor=ANA):
    import httpx

    return httpx.Client(base_url=url, headers={"X-Cairn-Actor": actor}, timeout=30)


def test_resources_created_changed_and_deleted_with_forms(cairn):
    from playwright.sync_api import expect, sync_playwright

    s, url = cairn
    api = _api(url)
    label = lambda text: re.compile(rf"^{re.escape(text)}")   # noqa: E731 - a required field's label ends in *
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": ANA}, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def area(hub, tab):
            page.goto(f"{url}/host#/{hub}?tab={tab}", wait_until="networkidle")

        def dialog():
            d = page.get_by_role("dialog").last
            expect(d).to_be_visible()
            return d

        def choose(d, field, option):
            """A select or picker: open it, then take the option (typed first when given as (typed, option))."""
            typed, option = option if isinstance(option, tuple) else (None, option)
            box = d.get_by_label(label(field))
            box.click()
            if typed:
                box.fill(typed)
            page.locator(".ant-select-dropdown:visible .ant-select-item-option", has_text=option).first.click()

        def submit(d, name):
            # The button's name; without the host's stylesheet antd's loading icon stays after a first try ("loading …").
            d.get_by_role("button", name=re.compile(rf"(^|loading ){re.escape(name)}$")).last.click()

        def closed(d):
            expect(d).to_be_hidden(timeout=20_000)

        try:
            # ---- Compute & jobs: a saved query, with the connection picker and the SQL editor -------------------
            area("compute", "saved-queries")
            empty = page.get_by_role("region", name="No Saved queries yet")
            expect(empty.get_by_role("link", name="Learn more")).to_have_attribute("href", re.compile("sql-workspace"))
            empty.get_by_role("button", name="New saved query").click()
            d = dialog()
            d.get_by_label(label("Name")).fill("Revenue by region")
            choose(d, "Connection", "wh (sqlite)")
            d.get_by_label(label("SQL")).fill("SELECT region, sum(amount) AS total FROM main.orders GROUP BY region")
            submit(d, "New saved query")
            closed(d)
            expect(page.get_by_role("button", name="Revenue by region", exact=True)).to_be_visible()
            saved = api.get("/sql/saved").json()
            assert [(q["name"], q["connection"]) for q in saved] == [("Revenue by region", "wh")]
            page.get_by_role("button", name="Edit: Revenue by region").click()
            d = dialog()
            expect(d.get_by_label(label("Name"))).to_have_value("Revenue by region")
            d.get_by_role("button", name=re.compile("Advanced options")).click()
            d.get_by_label("Description").fill("Revenue per region, all time")
            submit(d, "Edit")
            closed(d)
            assert api.get("/sql/saved").json()[0]["description"] == "Revenue per region, all time"
            page.get_by_role("button", name="Delete: Revenue by region").click()
            d = dialog()
            expect(d.get_by_text("Delete Revenue by region? This cannot be undone.")).to_be_visible()
            submit(d, "Delete")
            closed(d)
            expect(page.get_by_role("region", name="No Saved queries yet")).to_be_visible(timeout=20_000)
            assert api.get("/sql/saved").json() == []

            # ---- Compute & jobs: a job; the API's refusal of a name shown on the Name field; cron in words ------
            area("compute", "jobs")
            page.get_by_role("button", name="New job").first.click()
            d = dialog()
            d.get_by_label(label("Name")).fill("Nightly Export")
            choose(d, "Kind", "python")
            d.get_by_label("Code").fill("print('exported')")
            d.get_by_label("Cron").fill("0 2 * * *")
            expect(d.get_by_text("Every day at 02:00 UTC")).to_be_visible()
            submit(d, "New job")
            field_error = d.locator('[data-field="name"] [role="alert"]')
            expect(field_error).to_have_text("name: lowercase letters, digits and -")
            expect(d.get_by_label(label("Name"))).to_have_attribute("aria-invalid", "true")
            expect(d.get_by_text("Some values need fixing")).to_be_visible()
            d.get_by_label(label("Name")).fill("nightly-export")
            submit(d, "New job")
            closed(d)
            job = api.get("/v1/jobs/nightly-export", params={"source": "true"}).json()
            assert job["kind"] == "python" and job["cron"] == "0 2 * * *" and job["code"] == "print('exported')"
            page.get_by_role("button", name="Edit: nightly-export").click()
            d = dialog()
            expect(d.get_by_label("Code")).to_have_value("print('exported')")    # read whole: the list leaves code out
            d.get_by_label("Cron").fill("30 6 * * 1-5")
            expect(d.get_by_text("Every weekday at 06:30 UTC")).to_be_visible()
            submit(d, "Edit")
            closed(d)
            assert api.get("/v1/jobs/nightly-export").json()["cron"] == "30 6 * * 1-5"
            page.get_by_role("button", name="Delete: nightly-export").click()
            d = dialog()
            expect(d.get_by_text("Delete nightly-export? This cannot be undone.")).to_be_visible()
            submit(d, "Delete")
            closed(d)
            assert api.get("/v1/jobs/nightly-export").status_code == 404

            # ---- Lakehouse: a quality monitor, with the dataset picker; the API's 422 placed on Config --------
            area("lakehouse", "quality-monitors")
            page.get_by_role("button", name="New monitor").first.click()
            d = dialog()
            d.get_by_label(label("Name")).fill("Orders checked")
            choose(d, "Dataset URN", "main.orders")
            choose(d, "Type", "custom_sql")
            submit(d, "New monitor")
            expect(d.locator('[data-field="config"] [role="alert"]')).to_contain_text("config.connection is required")
            d.get_by_label("Config").fill('{"source": "sql", "connection": "wh", "sql": "SELECT count(*) AS n FROM main.orders"}')
            submit(d, "New monitor")
            closed(d)
            mon = api.get("/quality/monitors").json()
            assert [(m["name"], m["dataset_urn"], m["type"]) for m in mon] == [("Orders checked", DS, "custom_sql")]
            page.get_by_role("button", name="Edit: Orders checked").click()
            d = dialog()
            d.get_by_role("button", name=re.compile("Advanced options")).click()
            choose(d, "Severity", "HIGH")
            submit(d, "Edit")
            closed(d)
            assert api.get("/quality/monitors").json()[0]["severity"] == "HIGH"
            page.get_by_role("button", name="Delete: Orders checked").click()
            submit(dialog(), "Delete")
            expect(page.get_by_role("region", name="No Quality monitors yet")).to_be_visible(timeout=20_000)
            assert api.get("/quality/monitors").json() == []

            # ---- Trust & security: a notification route; the form's own check on a field ----------------------
            area("trust", "notification-routes")
            page.get_by_role("button", name="New route").first.click()
            d = dialog()
            d.get_by_label(label("ID")).fill("Finance Alerts")
            choose(d, "Channel", "webhook")
            d.get_by_label(label("Target")).fill("https://hooks.example/T0K3N")
            assert d.get_by_label(label("Target")).get_attribute("type") == "password"
            submit(d, "New route")
            expect(d.locator('[data-field="id"] [role="alert"]')).to_have_text("ID is not in the expected format.")
            d.get_by_label(label("ID")).fill("finance-alerts")
            types = d.get_by_label("Event types")
            types.fill("quality.incident")
            types.press("Enter")
            submit(d, "New route")
            closed(d)
            routes = api.get("/notify/routes").json()
            assert [(r["id"], r["channel"], r["target"], r["event_types"]) for r in routes] == [
                ("finance-alerts", "webhook", "https://hooks.example/T0K3N", ["quality.incident"])]
            page.get_by_role("button", name="Edit: finance-alerts").click()
            d = dialog()
            choose(d, "Channel", "slack")
            d.get_by_label(label("Target")).fill("#finance-data")
            submit(d, "Edit")
            closed(d)
            assert [(r["channel"], r["target"]) for r in api.get("/notify/routes").json()] == [("slack", "#finance-data")]
            page.get_by_role("button", name="finance-alerts", exact=True).click()  # the details: a panel, not JSON
            drawer = page.locator(".ant-drawer-content")
            expect(drawer.get_by_role("button", name="View JSON")).to_be_visible()
            expect(drawer.get_by_text("#finance-data")).to_have_count(0)            # sensitive: masked
            page.keyboard.press("Escape")
            page.get_by_role("button", name="Delete: finance-alerts").click()
            submit(dialog(), "Delete")
            expect(page.get_by_role("region", name="No Notification routes yet")).to_be_visible(timeout=20_000)
            assert api.get("/notify/routes").json() == []

            # ---- Trust & security: a secret scope, a secret typed into a password field, access to a group ------
            area("trust", "secret-scopes")
            page.get_by_role("button", name="Create scope").first.click()
            d = dialog()
            d.get_by_label(label("Name")).fill("payments")
            submit(d, "Create scope")
            closed(d)
            page.get_by_role("button", name="Put secret: payments").click()
            d = dialog()
            expect(d.get_by_label(label("Scope"))).to_be_disabled()               # the row's, not asked again
            d.get_by_label(label("Key")).fill("api-key")
            d.get_by_label(label("Value")).fill("sk_live_never_shown")
            assert d.get_by_label(label("Value")).get_attribute("type") == "password"
            submit(d, "Put secret")
            closed(d)
            assert "sk_live_never_shown" not in page.content()
            page.get_by_role("button", name="Grant access: payments").click()
            d = dialog()
            choose(d, "Principal", ("data-eng", "data-eng (group)"))
            choose(d, "Permission", "READ")
            submit(d, "Grant access")
            closed(d)
            scope = api.get("/v1/secret-scopes/payments").json()
            assert [k["key"] for k in scope["secrets"]] == ["api-key"]
            assert {"principal": "urn:li:corpGroup:data-eng", "permission": "READ"} in scope["acl"]
            assert s.secret_scopes.read(ANA, "payments", "api-key") == "sk_live_never_shown"
            page.get_by_role("button", name="Delete scope: payments").click()
            d = dialog()
            expect(d.get_by_text("Delete payments? This cannot be undone.")).to_be_visible()
            submit(d, "Delete scope")
            closed(d)
            assert api.get("/v1/secret-scopes").json() == []

            # ---- Cost & usage: a budget (a PATCH sends what changed) --------------------------------------------
            area("usage", "budgets")
            page.get_by_role("button", name="Create", exact=True).first.click()
            d = dialog()
            d.get_by_label(label("Name")).fill("AI spend")
            d.get_by_label(label("Amount")).fill("2000")
            d.get_by_label(label("AI")).click()                                  # the scope: one of; here AI spend
            submit(d, "Create")
            closed(d)
            budget = api.get("/v1/budgets").json()[0]
            assert (budget["name"], budget["amount_usd"], budget["scope"], budget["period"]) == ("AI spend", 2000, {"ai": True}, "month")
            page.get_by_role("button", name="Edit: AI spend").click()
            d = dialog()
            d.get_by_label(label("Amount")).fill("2500")
            submit(d, "Edit")
            closed(d)
            assert api.get("/v1/budgets").json()[0]["amount_usd"] == 2500
            page.get_by_role("button", name="Delete: AI spend").click()
            d = dialog()
            submit(d, "Delete")
            closed(d)
            assert api.get("/v1/budgets").json() == []
            # Raw JSON round-trips with the form.
            page.get_by_role("button", name="Create", exact=True).first.click()
            d = dialog()
            d.get_by_label(label("Name")).fill("Raw")
            d.get_by_label("Raw JSON").click()
            raw = d.get_by_label("Request body (JSON)")
            assert '"name": "Raw"' in raw.input_value()
            raw.fill('{"name": "From JSON", "amount_usd": 5, "scope": {"all": true}}')
            d.get_by_label("Raw JSON").click()
            expect(d.get_by_label(label("Name"))).to_have_value("From JSON")
            expect(d.get_by_label(label("Amount"))).to_have_value("5")
            d.get_by_role("button", name="Cancel").click()
        finally:
            browser.close()
    assert not errors, errors


def test_a_person_without_the_right_sees_why_in_words(cairn):
    from playwright.sync_api import expect, sync_playwright

    _s, url = cairn
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": BOB}, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(f"{url}/host#/usage?tab=budgets", wait_until="networkidle")
            page.get_by_role("button", name="Create", exact=True).first.click()
            d = page.get_by_role("dialog").last
            d.get_by_label(re.compile("^Name")).fill("Mine")
            d.get_by_label(re.compile("^Amount")).fill("10")
            d.get_by_label(re.compile("^All")).click()                           # the scope: everything
            d.get_by_role("button", name=re.compile(r"(^|loading )Create$")).last.click()
            notice = d.locator('[data-error-kind="forbidden"]')
            expect(notice).to_contain_text("You don’t have permission for “Create”")
            expect(notice).to_contain_text("Ask an owner of Budgets, or an admin, to give you access. "
                                           "It needs MANAGE_SYSTEM_OPERATIONS.")
            expect(notice.get_by_text("HTTP 403")).to_have_count(0)               # the detail waits under Details
            notice.get_by_role("button", name="Details").click()
            expect(notice).to_contain_text("budgets are set by operators (MANAGE_SYSTEM_OPERATIONS)")
        finally:
            browser.close()
    assert not errors, errors


def _git(cwd, *args):
    import subprocess

    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                   capture_output=True)


def _pick(page, box, option):
    """Open a picker (an antd select) and take the option with this text."""
    box.click()
    page.locator(".ant-select-dropdown:visible .ant-select-item-option", has_text=option).first.click()


def test_the_warehouse_git_folder_and_flow_pickers_choose_from_cairn(cairn, tmp_path):
    """The pickers that name a Cairn warehouse (a budget's scope), one of the person's git folders (committing a change
    request for review) and another flow (a flow's "after another flow" trigger) list what Cairn has, and the choice
    is what the API receives."""
    import json
    import subprocess

    from playwright.sync_api import expect, sync_playwright

    s, url = cairn
    api = _api(url)
    # A git repository of Ana's, as a folder in Cairn; a change request proposed through the API.
    remote = tmp_path / "infra.git"
    _git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(remote))
    _git(tmp_path, "clone", "--quiet", str(remote), str(tmp_path / "seed"))
    (tmp_path / "seed" / "README.md").write_text("infra\n")
    _git(tmp_path / "seed", "add", ".")
    _git(tmp_path / "seed", "commit", "--quiet", "-m", "init")
    _git(tmp_path / "seed", "push", "--quiet", "origin", "HEAD:main")
    folder = api.post("/v1/git-folders", json={"name": "infra", "url": f"file://{remote}", "branch": "main"})
    assert folder.status_code == 201, folder.text
    change = api.post("/v1/iac/changes", json={
        "title": "Revenue query", "intent": "a saved query for the finance team",
        "bundle_yaml": "queries:\n  - {name: picked-q, connection: wh, sql: SELECT 1 AS n}\n"})
    assert change.status_code in (200, 201), change.text
    cid = change.json()["id"]
    # Two flows: the second starts after the first.
    s.flows.create(ANA, {"name": "ingest-orders", "steps": [
        {"name": "count", "type": "sql", "sql": "SELECT count(*) AS n FROM main.orders", "connection": "wh"}]},
        publish=True)
    s.flows.create(ANA, {"name": "orders-report", "steps": [
        {"name": "count", "type": "sql", "sql": "SELECT count(*) AS n FROM main.orders", "connection": "wh"}]})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": ANA}, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            # ---- a budget for a warehouse's node-hours: the warehouse picker lists Cairn's warehouses -------------
            page.goto(f"{url}/host#/usage?tab=budgets", wait_until="networkidle")
            page.get_by_role("button", name="Create", exact=True).first.click()
            d = page.get_by_role("dialog").last
            d.get_by_label(re.compile("^Name")).fill("Sales warehouse")
            d.get_by_label(re.compile("^Amount")).fill("300")
            _pick(page, d.get_by_label(re.compile("^Warehouse")), "sales")
            d.get_by_label(re.compile("^AI")).click()                             # two choices: the form says one
            d.get_by_role("button", name=re.compile(r"(^|loading )Create$")).last.click()
            expect(d.get_by_text("Choose only one for Scope.")).to_be_visible()
            d.get_by_label(re.compile("^AI")).click()
            d.get_by_role("button", name=re.compile(r"(^|loading )Create$")).last.click()
            expect(d).to_be_hidden(timeout=20_000)
            assert [(b["name"], b["scope"]) for b in api.get("/v1/budgets").json()] == [
                ("Sales warehouse", {"warehouse": "sales"})]

            # ---- a change request committed to a branch of a git folder chosen with the git folder picker --------
            page.goto(f"{url}/host#/settings", wait_until="networkidle")
            page.get_by_role("tab", name="Infrastructure as code").click()
            page.get_by_role("tab", name="Changes").click()
            page.get_by_role("button", name="Revenue query").click()
            box = page.get_by_role("combobox", name="Git folder")
            expect(box).to_be_visible(timeout=20_000)
            _pick(page, box, "infra")
            page.get_by_role("button", name="Commit to a branch for review").click()
            branch = f"cairn/iac-change-{cid[:12]}"
            end = time.time() + 30
            while branch not in subprocess.run(["git", "--git-dir", str(remote), "branch", "--list", "cairn/*"],
                                               capture_output=True, text=True).stdout:
                assert time.time() < end, "the change was not committed"
                time.sleep(0.5)
            files = subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", branch],
                                   capture_output=True, text=True).stdout.split()
            assert f"iac/changes/{cid[:12]}/change.yaml" in files
            assert api.get(f"/v1/iac/changes/{cid}").json()["commit"]["branch"] == branch

            # ---- a flow that starts after another: the flow picker lists the person's flows ----------------------
            page.goto(f"{url}/host#/flows?flow=orders-report", wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name="orders-report")).to_be_visible(timeout=30_000)
            page.get_by_role("tab", name="Settings").click()
            _pick(page, page.get_by_label("After another flow succeeds"), "ingest-orders")
            page.get_by_role("button", name="Save draft").click()
            expect(page.get_by_text("Draft saved")).to_be_visible(timeout=20_000)
            triggers = api.get("/v1/flows/orders-report").json()["draft"]["triggers"]
            assert [(t["type"], t["flow"], t["on"]) for t in triggers] == [("flow", "ingest-orders", ["succeeded"])], \
                json.dumps(triggers)
        finally:
            browser.close()
    assert not errors, errors


def _catalog_ready():
    return _ready() and bool(os.environ.get("CAIRN_CATALOG_BIN")) and bool(os.environ.get("CAIRN_STORE_BIN"))


@pytest.mark.skipif(not _catalog_ready(), reason="needs CAIRN_CATALOG_BIN and CAIRN_STORE_BIN (a real Iceberg catalog)")
def test_a_secret_reference_is_picked_for_an_external_catalog(cairn, tmp_path):
    """Registering another organisation's Iceberg catalog (a real Cairn Catalog stands for it): its credential is a
    reference chosen with the secret-reference picker (scope / key, never the value), and Cairn signs in with it."""
    import subprocess

    from playwright.sync_api import expect, sync_playwright

    _s, url = cairn
    api = _api(url)
    token = "partner-catalog-token-" + "t" * 24
    assert api.post("/v1/secret-scopes", json={"name": "partner"}).status_code in (200, 201)
    r = api.put("/v1/secret-scopes/partner/secrets/catalog-token", json={"value": token})
    assert r.status_code in (200, 201, 204), r.text
    # The partner's lakehouse: its own store and catalog.
    sp, cp = _port(), _port()
    key = ("partner-store", "partner-store-secret-" + "s" * 12)
    procs = [subprocess.Popen([os.environ["CAIRN_STORE_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **os.environ, "CAIRN_STORE_ACCESS_KEY": key[0], "CAIRN_STORE_SECRET_KEY": key[1],
        "CAIRN_STORE_DATA": str(tmp_path / "store"), "CAIRN_STORE_ADDR": f"127.0.0.1:{sp}",
        "CAIRN_STORE_BUCKETS": "partner", "CAIRN_STORE_FSYNC": "false", "CAIRN_STORE_META": ""})]
    procs.append(subprocess.Popen([os.environ["CAIRN_CATALOG_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **os.environ, "CAIRN_CATALOG_ADDR": f"127.0.0.1:{cp}", "CAIRN_CATALOG_DATA": str(tmp_path / "catalog"),
        "CAIRN_CATALOG_TOKEN": token, "CAIRN_CATALOG_WAREHOUSE": "s3://partner/warehouse",
        "CAIRN_CATALOG_S3_ENDPOINT": f"http://127.0.0.1:{sp}", "CAIRN_CATALOG_S3_ACCESS_KEY": key[0],
        "CAIRN_CATALOG_S3_SECRET_KEY": key[1], "CAIRN_CATALOG_FSYNC": "false"}))
    try:
        for port_, proc in ((sp, procs[0]), (cp, procs[1])):
            end = time.time() + 30
            while True:
                try:
                    socket.create_connection(("127.0.0.1", port_), 0.3).close()
                    break
                except OSError:
                    assert time.time() < end and proc.poll() is None, "the partner's store or catalog did not start"
                    time.sleep(0.2)
        from pyiceberg.catalog.rest import RestCatalog
        import pyarrow as pa

        cat = RestCatalog("partner", uri=f"http://127.0.0.1:{cp}", token=token)
        cat.create_namespace("sales")
        orders = pa.table({"id": pa.array([1, 2], pa.int64()), "region": ["EU", "US"]})
        cat.create_table("sales.orders", orders.schema).append(orders)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": ANA}, viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            try:
                page.goto(f"{url}/host#/lakehouse?tab=external-catalogs", wait_until="networkidle")
                page.get_by_role("button", name="Register").first.click()
                d = page.get_by_role("dialog").last
                d.get_by_label(re.compile("^Name")).fill("partner")
                d.get_by_label(re.compile("^URI")).fill(f"http://127.0.0.1:{cp}")
                _pick(page, d.get_by_label(re.compile("^Auth")), "token")
                _pick(page, d.get_by_label(re.compile("^Credential ref")), "partner / catalog-token")
                assert token not in page.content()                                # a reference, never the value
                d.get_by_role("button", name=re.compile(r"(^|loading )Register$")).last.click()
                expect(d).to_be_hidden(timeout=30_000)
                cat = api.get("/v1/external-catalogs/partner").json()
                assert cat["credential_ref"] == "scope:partner/catalog-token" and cat["auth"] == "token", cat
            finally:
                browser.close()
            assert not errors, errors
    finally:
        for proc in reversed(procs):
            proc.terminate()
            proc.wait(15)
