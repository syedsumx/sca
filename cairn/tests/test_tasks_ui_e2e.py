"""The fifteen most common tasks (L128), done in Chromium the way a new person would: from the home page, with only
the command palette, the sidebar, the "Get started" links and forms (no address typed after the first), against a
real Cairn API.

The stack: the built UI bundle (ui/dist/mfe, `npm run build:mfe`) mounted the way Cairn Core's host mounts it; Cairn's
API with its real services, database and policy decision point; a real SQLite warehouse behind Cairn's governed data
access (preview, SQL, saved queries and dashboards run on it as the person); the assistant on the official Anthropic SDK
against a local stand-in for the provider's API (tests/test_ai_gateway.py's), metered like any model call.

Cairn Core is not part of this stack. Its GraphQL (catalog search, an asset's header, data products) is stood in for by
a small handler that answers from the same in-memory catalog Cairn's own services read and write: a search finds the
dataset, an asset page shows the owners Cairn wrote. The glossary-term-or-owner task is done with Cairn's own bulk edit
(an owner, written by Cairn to the catalog, with Undo), not Core's glossary pages.

Accepting an ontology proposal (task 11) needs the compiler and its stack (test_ontology_e2e's: PostgreSQL, Cairn
Store, Cairn Catalog, Trino); it runs when CAIRN_ONTOLOGY_BIN, CAIRN_STORE_BIN, CAIRN_CATALOG_BIN and docker are there,
and is skipped (saying so) otherwise.

Needs Playwright (with Chromium) and the built bundle."""

from __future__ import annotations

import base64
import http.server
import os
import re
import socket
import threading
import time

import pytest
from fastapi import Request     # at module level: with postponed annotations FastAPI must resolve it here

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "ui", "dist", "mfe")
ANA = "urn:li:corpuser:ana"          # new here: an analyst
OLGA = "urn:li:corpuser:olga"        # owns the Orders data product
BOB = "urn:li:corpuser:bob"          # a person without Cairn's operator privileges
DS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"
PRODUCT = "urn:li:dataProduct:orders"
ANSWER = "The EU sells most: 16.50 over 2 orders, against 3.00 in the US."


def _ready():
    try:
        import anthropic  # noqa: F401
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(os.path.join(BUNDLE, "remoteEntry.js"))


pytestmark = pytest.mark.skipif(not _ready(), reason="needs Playwright, the anthropic SDK and the built UI bundle "
                                                     "(npm run build:mfe)")

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


class CoreGraphQL:
    """Cairn Core's GraphQL, for the few queries these tasks make, answered from the in-memory catalog."""

    def __init__(self, graph):
        self.graph = graph

    def _name(self, urn: str) -> str:
        return (self.graph.aspects.get((urn, "datasetProperties")) or {}).get("name") or urn

    def _entity(self, urn: str) -> dict:
        kind = urn.split(":")[2]
        if kind == "dataProduct":
            props = self.graph.aspects.get((urn, "dataProductProperties")) or {}
            owners = (self.graph.aspects.get((urn, "ownership")) or {}).get("owners") or []
            return {"urn": urn, "type": "DATA_PRODUCT",
                    "properties": {"name": props.get("name"), "description": props.get("description"), "numAssets": 1,
                                   "externalUrl": None, "customProperties": []},
                    "domain": None, "tags": {"tags": []}, "institutionalMemory": {"elements": []},
                    "ownership": {"owners": [{"owner": {"urn": o["owner"]}} for o in owners]},
                    "entities": {"total": 1, "searchResults": [{"entity": {
                        "urn": DS, "type": "DATASET", "name": self._name(DS), "platform": {"name": "sqlite"},
                        "properties": {"name": self._name(DS)}}}]}}
        props = self.graph.aspects.get((urn, "datasetProperties")) or {}
        owners = (self.graph.aspects.get((urn, "ownership")) or {}).get("owners") or []
        fields = (self.graph.aspects.get((urn, "schemaMetadata")) or {}).get("fields") or []
        return {"urn": urn, "type": "DATASET", "name": props.get("name"), "platform": {"name": "sqlite"},
                "properties": {"name": props.get("name"), "description": props.get("description")},
                "editableProperties": None, "domain": None, "tags": {"tags": []}, "glossaryTerms": {"terms": []},
                "ownership": {"owners": [{"owner": {"urn": o["owner"], "username": o["owner"].split(":")[-1]},
                                          "ownershipType": None} for o in owners]},
                "schemaMetadata": {"fields": [{"fieldPath": f["fieldPath"], "nativeDataType": f.get("nativeDataType"),
                                               "description": None, "glossaryTerms": None} for f in fields]},
                "editableSchemaMetadata": None}

    def __call__(self, query: str, variables: dict) -> dict:
        op = (re.search(r"query\s+(\w+)", query) or [None, ""])[1]
        if op in ("cairnConsumerSearch", "cairnProducts"):
            inp = variables.get("input") or {}
            types = inp.get("types") or ["DATASET"]
            q = (inp.get("query") or "*").strip("* ").lower()
            urns = sorted({u for (u, a) in self.graph.aspects if a in ("datasetProperties", "dataProductProperties")})
            hits = []
            for u in urns:
                e = self._entity(u)
                if e["type"] in types and (not q or q in (e["properties"]["name"] or "").lower()):
                    hits.append({"entity": e})
            return {"searchAcrossEntities": {"total": len(hits), "searchResults": hits, "facets": []}}
        if op == "cairnProduct":
            return {"dataProduct": self._entity(variables["urn"])}
        if op == "cairnConsumerAsset":
            return {"entity": self._entity(variables["urn"])}
        return {}


@pytest.fixture
def workspace(settings, graph, tmp_path, monkeypatch):
    import anthropic
    import uvicorn
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from sqlalchemy import create_engine, text

    from cairn.ai.catalog import Catalog
    from cairn.ai.llm import AnthropicProvider
    from cairn.ai.usage import MeteredProvider
    from cairn.app import create_api_app
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.services import build_services
    from test_ai_gateway import Provider

    class Model(Provider):
        def reply(self, body):
            return ANSWER

    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    wh = tmp_path / "wh.db"
    with create_engine(f"sqlite:///{wh}").begin() as c:
        c.execute(text("CREATE TABLE orders (id INTEGER, region TEXT, amount REAL)"))
        c.execute(text("INSERT INTO orders VALUES (1, 'EU', 9.5), (2, 'US', 3.0), (3, 'EU', 7.0)"))
    conns = tmp_path / "connections.yaml"
    conns.write_text(f"connections:\n  wh:\n    url: sqlite:///{wh}\n    catalog:\n      platform: sqlite\n")
    monkeypatch.setenv("CAIRN_QUALITY_CONNECTIONS", str(conns))
    monkeypatch.setenv("CAIRN_SECRETS_KEY", base64.b64encode(os.urandom(32)).decode())
    graph.aspects[(DS, "datasetProperties")] = {"name": "orders", "description": "One row per order."}
    graph.aspects[(DS, "schemaMetadata")] = {"fields": [{"fieldPath": c, "nativeDataType": t} for c, t in
                                                        (("id", "INTEGER"), ("region", "TEXT"), ("amount", "REAL"))]}
    graph.aspects[(PRODUCT, "dataProductProperties")] = {"name": "Orders", "description": "Every order, by region."}
    graph.aspects[(PRODUCT, "ownership")] = {"owners": [{"owner": OLGA, "type": "DATAOWNER"}]}
    core = CoreGraphQL(graph)
    graph.graphql_handler = core
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="all", effect="allow", actions=["*"]),
        Policy(id="bob-no-ops", effect="deny", actions=["MANAGE_POLICIES", "MANAGE_SYSTEM_OPERATIONS",
                                                        "MANAGE_GLOBAL_SETTINGS"],
               condition={"attr": "subject.urn", "op": "eq", "value": BOB})]))
    s.catalog = Catalog(graph)              # the catalog's reader (search, bulk edit's export), without other AI services
    client = anthropic.Anthropic(api_key="sk-ant-upstream", base_url=f"http://127.0.0.1:{up.server_address[1]}")
    s.assistant.llm = MeteredProvider(AnthropicProvider("stand-in-model", server_side_fallback=False, client=client),
                                      s.ai_usage)
    app = create_api_app(s)

    async def graphql(request: Request):
        body = await request.json()
        return JSONResponse({"data": core(body.get("query") or "", body.get("variables") or {})})
    app.add_api_route("/api/v2/graphql", graphql, methods=["POST"])
    app.mount("/ui", StaticFiles(directory=BUNDLE), name="ui")
    app.add_api_route("/host", lambda: HTMLResponse(HOST), methods=["GET"])
    # The catalog shell's entries (deploy/mfe/mfe.config.yaml): each mounts the same bundle at /mfe/<entry>.
    app.add_api_route("/mfe/{entry}", lambda entry: HTMLResponse(HOST), methods=["GET"])
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
    up.shutdown()


def _api(url, actor=ANA):
    import httpx

    return httpx.Client(base_url=url, headers={"X-Cairn-Actor": actor}, timeout=30)


class Person:
    """A person in a browser, getting around only as the UI offers."""

    def __init__(self, browser, url, actor):
        from playwright.sync_api import expect

        self.expect = expect
        self.ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": actor}, viewport={"width": 1440, "height": 900})
        self.page = self.ctx.new_page()
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.url = url

    def home(self):
        self.page.goto(f"{self.url}/host#/discover", wait_until="domcontentloaded")
        self.expect(self.nav()).to_be_visible(timeout=30_000)

    def nav(self):
        return self.page.get_by_role("navigation", name="Workspace sections")

    def palette(self, typed: str, option: str | re.Pattern, keyboard: bool = True):
        """⌘K, type, then the option: with the arrow keys and Enter, or a click."""
        page = self.page
        page.keyboard.press("Control+k")
        box = page.get_by_role("combobox", name="Type a page, an action or a dataset…")
        self.expect(box).to_be_focused(timeout=10_000)
        box.fill(typed)
        listbox = page.get_by_role("listbox", name="Command palette")
        target = listbox.get_by_role("option", name=option).first
        self.expect(target).to_be_visible(timeout=15_000)
        if not keyboard:
            target.click()
            return
        options = listbox.get_by_role("option")
        want = target.get_attribute("id")
        for _ in range(options.count()):
            if box.get_attribute("aria-activedescendant") == want:
                break
            box.press("ArrowDown")
        assert box.get_attribute("aria-activedescendant") == want
        box.press("Enter")
        self.expect(box).to_be_hidden()

    def dialog(self):
        d = self.page.get_by_role("dialog").last
        self.expect(d).to_be_visible()
        return d

    def submit(self, d, name):
        d.get_by_role("button", name=re.compile(rf"(^|loading ){re.escape(name)}$")).last.click()


def until(fn, timeout=20.0):
    """fn()'s first truthy answer (polled)."""
    end = time.time() + timeout
    while True:
        out = fn()
        if out or time.time() > end:
            return out
        time.sleep(0.2)


def field(text):
    return re.compile(rf"^{re.escape(text)}")      # a required field's label ends in *


def test_the_common_tasks_end_to_end(workspace):
    from playwright.sync_api import expect, sync_playwright

    s, url = workspace
    api = _api(url)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ana = Person(browser, url, ANA)
        page = ana.page
        try:
            # ---- First sign-in: the role, then "Get started" from the sidebar ---------------------------------
            ana.home()
            role = page.get_by_role("dialog", name="Welcome to Cairn. What do you mostly do here?")
            expect(role).to_be_visible(timeout=20_000)
            role.get_by_role("radio", name=re.compile("^Analyst")).check()
            role.get_by_role("button", name="Continue").click()
            expect(role).to_be_hidden(timeout=20_000)
            assert api.get("/v1/me/profile").json()["ui"]["onboarding"]["role"] == "analyst"
            for_you = ana.nav().get_by_role("heading", name="For you")
            expect(for_you).to_be_visible()
            ana.nav().get_by_role("link", name="Get started").click()
            expect(page.get_by_role("heading", name="Get started", level=1)).to_be_visible()
            expect(page.get_by_text("0 of 5 done")).to_be_visible(timeout=20_000)

            # ---- 1. Find a dataset by search (the palette's catalog results, found as the person) -------------
            ana.palette("orders", re.compile(r"^orders.*Catalog$"))
            expect(page.get_by_role("heading", name="orders", level=1)).to_be_visible(timeout=20_000)

            # ---- 2. Preview it ----------------------------------------------------------------------------------
            page.get_by_role("tab", name="Data").click()
            preview = page.get_by_role("region", name="Preview")
            expect(preview.get_by_role("cell", name="EU").first).to_be_visible(timeout=20_000)
            expect(preview.get_by_role("cell", name="9.5")).to_be_visible()

            # ---- 3. Run a SQL query and see the results -----------------------------------------------------------
            ana.palette("sql", "SQL")
            expect(page.get_by_role("heading", name="SQL", level=1)).to_be_visible(timeout=20_000)
            page.get_by_role("textbox", name="SQL").fill(
                "SELECT region, sum(amount) AS total FROM main.orders GROUP BY region ORDER BY region")
            page.get_by_role("textbox", name="SQL").press("Escape")          # the editor's suggestions close
            page.get_by_role("button", name="Run", exact=True).click()
            expect(page.get_by_role("cell", name="16.5")).to_be_visible(timeout=20_000)

            # ---- 4. Save the query --------------------------------------------------------------------------------
            page.get_by_role("button", name="Save", exact=True).click()
            d = ana.dialog()
            d.get_by_label("Name").fill("Revenue by region")
            d.get_by_role("button", name="Save").click()
            # (Without the host's stylesheet antd's closing animation never ends: the answer is what counts.)
            saved = until(lambda: api.get("/sql/saved").json())
            assert [(q["name"], q["owner"]) for q in saved] == [("Revenue by region", ANA)]

            # ---- 5. A dashboard from it -----------------------------------------------------------------------------
            ana.palette("dashb", "Dashboards")
            page.get_by_role("button", name="New dashboard").click()
            d = ana.dialog()
            d.get_by_label("Dashboard name").fill("Sales")
            d.get_by_role("combobox", name="Queries").click()
            page.locator(".ant-select-dropdown:visible .ant-select-item-option", has_text="Revenue by region").first.click()
            d.get_by_label("Dashboard name").click()                     # the list closes (Escape would close the form)
            d.get_by_role("button", name="Create").click()
            # Ana's own (an operator also sees the built-in ops dashboards Cairn owns, L143): waited for, since the
            # built-ins are there before hers is saved.
            boards = until(lambda: [b for b in api.get("/sql/dashboards").json()
                                    if b["owner"] != "urn:li:corpuser:cairn-observability"])
            assert [(b["name"], b["owner"]) for b in boards] == [("Sales", ANA)]
            expect(page.get_by_text("16.5").first).to_be_visible(timeout=20_000)

            # ---- The checklist ticked itself: a dataset page seen, a query run, saved, a dashboard built -------------
            ana.nav().get_by_role("link", name="Get started").click()
            expect(page.get_by_text("4 of 5 done")).to_be_visible(timeout=30_000)
            for step in ("find-dataset", "run-query", "save-query", "build-dashboard"):
                expect(page.locator(f'[data-step="{step}"]')).to_have_attribute("data-done", "true")
            expect(page.locator('[data-step="ask-question"]')).to_have_attribute("data-done", "false")
            assert api.get("/v1/me/profile").json()["ui"]["onboarding"]["done"] == [
                "find-dataset", "run-query", "save-query", "build-dashboard"]

            # ---- 6. Ask a question in plain English (the palette's last entry hands it to the assistant) -------------
            ana.palette("Which region sells most?", re.compile(r"^Ask: Which region sells most\?"))
            expect(page.get_by_text(ANSWER)).to_be_visible(timeout=30_000)
            ana.nav().get_by_role("link", name="Get started").click()
            expect(page.get_by_text("5 of 5 done")).to_be_visible(timeout=20_000)
            expect(page.get_by_text("All done. You know your way around.")).to_be_visible()

            # ---- 7. A flow from a template, run --------------------------------------------------------------------
            ana.palette("new flow", "New flow")
            page.get_by_role("radio", name=re.compile("^Blank flow")).click()
            page.get_by_label("Name (lowercase letters, digits, - and _)").fill("hello-team")
            page.get_by_role("button", name="Create draft").click()
            expect(page.get_by_role("heading", name="hello-team")).to_be_visible(timeout=20_000)
            page.get_by_role("button", name="Publish…").click()
            d = page.get_by_role("dialog", name="Publish hello-team")
            d.get_by_role("button", name="Publish").click()
            expect(page.get_by_text("Published version 1")).to_be_visible(timeout=20_000)
            page.get_by_role("button", name="Run", exact=True).click()
            expect(page.locator(".fl-badge.succeeded").first).to_be_visible(timeout=60_000)
            assert s.flows.runs(ANA, "hello-team")[0]["status"] == "succeeded"

            # ---- 8. A notification route (the form the palette opens) ----------------------------------------------
            ana.palette("new route", re.compile("^New route"))
            d = ana.dialog()
            d.get_by_label(field("ID")).fill("sales-alerts")
            d.get_by_label(field("Channel")).click()
            page.locator(".ant-select-dropdown:visible .ant-select-item-option", has_text="webhook").first.click()
            d.get_by_label(field("Target")).fill("https://hooks.example/sales")
            types = d.get_by_label("Event types")
            types.fill("quality.incident")
            types.press("Enter")
            ana.submit(d, "New route")
            expect(d).to_be_hidden(timeout=20_000)
            assert [(r["id"], r["channel"]) for r in api.get("/notify/routes").json()] == [("sales-alerts", "webhook")]

            # ---- 9. Request access to the dataset's data product -----------------------------------------------------
            ana.nav().get_by_role("link", name="Marketplace").first.click()
            card = page.get_by_label("Data product Orders")
            card.get_by_role("button", name="Request access").click()
            drawer = page.locator(".ant-drawer-content").last
            drawer.get_by_role("radio", name="Reporting").check()
            drawer.get_by_role("button", name="Next").click()
            drawer.get_by_role("button", name="Next").click()
            drawer.get_by_label("Justification").fill("I report revenue by region to finance every month.")
            drawer.get_by_role("button", name="Submit request").click()
            expect(card.get_by_text("Request pending")).to_be_visible(timeout=20_000)
            reqs = api.get("/marketplace/access-requests", params={"scope": "mine"}).json()
            assert [(r["product_urn"], r["state"]) for r in reqs] == [(PRODUCT, "PENDING")]
            assert OLGA in reqs[0]["approvers"]

            # ---- 10. The owner approves it -----------------------------------------------------------------------------
            olga = Person(browser, url, OLGA)
            olga.home()
            olga.page.get_by_role("dialog").get_by_role("button", name="Not now").click()
            olga.nav().get_by_role("link", name=re.compile("^Requests")).click()
            olga.page.get_by_role("tab", name=re.compile("^Approvals")).click()
            olga.page.get_by_role("button", name="Approve").click()
            olga.dialog().get_by_role("button", name="Confirm").click()
            expect(olga.page.get_by_text("APPROVED").first).to_be_visible(timeout=20_000)
            assert api.get(f"/marketplace/access-requests/{reqs[0]['id']}").json()["state"] == "APPROVED"
            olga.ctx.close()

            # ---- 12. An owner on the dataset, with Cairn's bulk edit (and Undo in the toast) ------------------------
            ana.palette("search", "Search")
            page.get_by_role("combobox", name=re.compile("Search", re.I)).first.fill("orders")
            page.keyboard.press("Enter")
            page.get_by_role("button", name="Bulk edit…").click()
            d = ana.dialog()
            d.get_by_label("CSV text").fill(f"urn,owners\n\"{DS}\",ana\n")
            d.get_by_role("button", name="Preview changes").click()
            d.get_by_role("button", name=re.compile(r"(^|loading )Apply 1 change")).click()
            toast = page.get_by_role("status").filter(has_text="1 change(s) applied")
            expect(toast.get_by_role("button", name="Undo")).to_be_visible(timeout=20_000)
            owners = (s.graph.aspects.get((DS, "ownership")) or {}).get("owners") or []
            assert [o["owner"] for o in owners] == [ANA]
            d.get_by_role("button", name="Done").click()

            # ---- 13. A secret scope with a secret -----------------------------------------------------------------------
            ana.palette("create scope", re.compile("^Create scope"))
            d = ana.dialog()
            d.get_by_label(field("Name")).fill("sales-api")
            ana.submit(d, "Create scope")
            expect(d).to_be_hidden(timeout=20_000)
            page.get_by_role("button", name="Put secret: sales-api").click()
            d = ana.dialog()
            d.get_by_label(field("Key")).fill("token")
            d.get_by_label(field("Value")).fill("s3cr3t-never-shown")
            ana.submit(d, "Put secret")
            expect(d).to_be_hidden(timeout=20_000)
            assert "s3cr3t-never-shown" not in page.content()
            assert s.secret_scopes.read(ANA, "sales-api", "token") == "s3cr3t-never-shown"

            # ---- 14. Their own usage and cost ------------------------------------------------------------------------
            ana.palette("your usage", re.compile("Your usage"))
            mine = page.get_by_role("region", name="Your usage")
            expect(mine.get_by_role("row").nth(1)).to_be_visible(timeout=20_000)
            usage = api.get("/ai/usage", params={"mine": "true", "group_by": "day"}).json()
            assert sum(g["calls"] for g in usage["groups"]) >= 1

            # ---- 15. Their profile's language -------------------------------------------------------------------------
            ana.nav().get_by_role("link", name="Profile").click()
            page.get_by_role("main").get_by_role("combobox", name="Language").click()     # the profile's, not this browser's
            page.locator(".ant-select-dropdown:visible .ant-select-item-option", has_text="Deutsch").first.click()
            page.get_by_role("button", name="Save").last.click()
            expect(page.get_by_role("navigation", name="Bereiche").get_by_role("link", name="Entdecken")).to_be_visible(
                timeout=20_000)
            assert api.get("/v1/me/profile").json()["language"] == "de"
        finally:
            browser.close()
    assert not ana.errors, ana.errors


def test_the_palette_finds_pages_and_actions_and_the_sidebar_fits_the_person(workspace):
    from playwright.sync_api import expect, sync_playwright

    s, url = workspace
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        bob = Person(browser, url, BOB)
        page = bob.page
        try:
            bob.home()
            page.get_by_role("dialog").get_by_role("button", name="Not now").click()
            # A page, by letters in order.
            bob.palette("sqwf", "SQL workflows")
            expect(page.get_by_role("heading", name="SQL workflows", level=1)).to_be_visible(timeout=20_000)
            # An action: the form opens on its hub's tab.
            bob.palette("new saved query", re.compile("^New saved query"))
            expect(bob.dialog().get_by_label(field("SQL"))).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.get_by_role("tab", name="Saved queries", selected=True)).to_be_visible()
            # "?" lists the shortcuts.
            page.get_by_role("heading", level=1).first.click()   # somewhere that is not a field
            page.keyboard.press("?")
            expect(page.get_by_role("dialog", name="Keyboard shortcuts")).to_be_visible()
            page.keyboard.press("Escape")
            # Help for the tab on screen, from the bundled docs.
            page.get_by_role("button", name="Help for this page").first.click()
            help_panel = page.locator(".ant-drawer-content").last
            expect(help_panel.get_by_role("heading", name="SQL workspace")).to_be_visible(timeout=10_000)
            expect(help_panel.get_by_role("link", name="Open full docs")).to_have_attribute(
                "href", "/docs/administration.html#sql-workspace")
            page.keyboard.press("Escape")
            # Bob can't administer Cairn: those areas are not offered (the API refuses them anyway).
            nav = bob.nav()
            expect(nav.get_by_role("link", name="SQL").first).to_be_visible()
            expect(nav.get_by_role("link", name="Governance")).to_have_count(0)
            nav.get_by_role("link", name="Cost & usage").click()
            expect(page.get_by_role("tab", name="Your usage")).to_be_visible(timeout=20_000)
            expect(page.get_by_role("tab", name="AI usage")).to_have_count(0)
            assert _api(url, BOB).get("/ai/usage").status_code == 403
            # Customise the sidebar: hide a page; it follows Bob (kept in his profile); Undo puts it back.
            page.get_by_role("button", name="Customise sidebar").click()
            d = page.get_by_role("dialog", name="Customise sidebar")
            d.get_by_role("button", name="Hide Workbooks").click()
            expect(nav.get_by_role("link", name="Workbooks")).to_have_count(0)
            assert "/sql/workbooks" in _api(url, BOB).get("/v1/me/profile").json()["ui"]["sidebar"]["hidden"]
            page.get_by_role("status").filter(has_text="Hide Workbooks").get_by_role("button", name="Undo").click()
            expect(nav.get_by_role("link", name="Workbooks")).to_be_visible(timeout=10_000)
            d.get_by_role("button", name="Done").click()
        finally:
            browser.close()
    assert not bob.errors, bob.errors


def test_the_catalog_shells_compact_bar(workspace):
    """Hosted by the catalog shell (a page under /mfe/<entry>), the workspace has no sidebar of its own: a compact bar
    of its sections under its top bar (palette, search, ask, help). A bare entry opens its own section; a section
    owned by another entry moves the shell there (the shell's URL, which the shell then mounts); one it owns opens in
    place; the palette and the help work there too; and axe finds nothing serious."""
    import json

    from playwright.sync_api import expect, sync_playwright

    _s, url = workspace
    axe = os.path.join(ROOT, "ui", "node_modules", "axe-core", "axe.min.js")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ana = Person(browser, url, ANA)
        page = ana.page
        try:
            page.goto(f"{url}/mfe/cairn-marketplace", wait_until="domcontentloaded")
            bar = page.get_by_role("navigation", name="Workspace sections")
            expect(bar).to_be_visible(timeout=30_000)
            expect(page).to_have_url(re.compile(r"/mfe/cairn-marketplace#/marketplace$"))    # the entry's own section
            expect(page.get_by_role("heading", level=1, name="Data Marketplace")).to_be_visible()
            expect(bar.get_by_role("link", name="Marketplace")).to_have_attribute("aria-current", "page")
            expect(page.get_by_role("button", name="Collapse sidebar")).to_have_count(0)      # the shell has the sidebar
            for name in ("Discover", "Catalog", "Data products", "Requests", "Settings"):
                expect(bar.get_by_role("link", name=re.compile(rf"^{name}"))).to_be_visible()
            # The top bar above it: the palette, search, ask and help.
            expect(page.get_by_role("button", name="Search or jump to…")).to_be_visible()
            expect(page.get_by_role("textbox", name="Search", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="✨ Ask")).to_be_visible()
            # Requests belongs to another shell entry: the shell's URL moves (the shell then mounts the entry there).
            bar.get_by_role("link", name=re.compile("^Requests")).click()
            expect(page).to_have_url(re.compile(r"/mfe/cairn-requests#/requests$"))
            page.reload(wait_until="domcontentloaded")                                         # what the shell does
            expect(page.get_by_role("heading", level=1, name="Requests")).to_be_visible(timeout=30_000)
            expect(bar.get_by_role("link", name=re.compile("^Requests"))).to_have_attribute("aria-current", "page")
            # Lineage has no entry of its own: it opens in place (Search, no sidebar entry since L147, is in the top bar).
            bar.get_by_role("link", name="Lineage").click()
            expect(page).to_have_url(re.compile(r"/mfe/cairn-requests#/lineage"))
            # The palette and the help, as everywhere.
            page.keyboard.press("Control+k")
            expect(page.get_by_role("combobox", name="Type a page, an action or a dataset…")).to_be_focused(timeout=10_000)
            page.keyboard.press("Escape")
            page.goto(f"{url}/mfe/cairn-settings", wait_until="domcontentloaded")
            header = page.locator("[data-page-header]")
            expect(header.get_by_role("heading", level=1, name="Settings")).to_be_visible(timeout=30_000)
            assert header.get_attribute("data-help-anchor") == "integrations.html#notifications"
            header.get_by_role("button", name=re.compile("help", re.I)).click()
            help_panel = page.get_by_role("dialog").last
            expect(help_panel).to_be_visible()
            expect(help_panel.get_by_role("link", name=re.compile("Open full docs"))).to_have_attribute(
                "href", re.compile("integrations.html#notifications"))
            page.keyboard.press("Escape")
            # axe on the hosted layout (WCAG 2.1 A and AA): nothing serious or critical.
            page.add_script_tag(path=axe)
            found = page.evaluate("""async () => {
                const r = await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']},
                                                   resultTypes: ['violations']});
                return r.violations.filter(v => v.impact === 'serious' || v.impact === 'critical')
                    .map(v => ({id: v.id, nodes: v.nodes.slice(0, 3).map(n => n.target.join(' '))}));
            }""")
            assert not found, json.dumps(found, indent=1)
        finally:
            browser.close()
    assert not ana.errors, ana.errors


# ---- 11. Accepting an ontology proposal: the compiler's stack ------------------------------------------------------------
def _ontology_ready() -> bool:
    try:
        import test_trino_e2e

        return bool(os.environ.get("CAIRN_ONTOLOGY_BIN")) and test_trino_e2e._ready()
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _ontology_ready(), reason="accepting an ontology proposal needs the compiler's stack: "
                                                  "CAIRN_ONTOLOGY_BIN, CAIRN_STORE_BIN, CAIRN_CATALOG_BIN and docker")
def test_a_steward_accepts_an_ontology_proposal_from_get_started(request):
    from playwright.sync_api import expect, sync_playwright

    stack = request.getfixturevalue("stack")
    request.getfixturevalue("compiled")
    from test_ontology_e2e import STEW
    from test_ontology_ui_e2e import _serve

    url, s = stack["url"], stack["s"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": STEW}, viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        _serve(page, url)
        try:
            page.goto(f"{url}/host#/discover", wait_until="domcontentloaded")
            role = page.get_by_role("dialog", name="Welcome to Cairn. What do you mostly do here?")
            expect(role).to_be_visible(timeout=30_000)
            role.get_by_role("radio", name=re.compile("^Data steward")).check()
            role.get_by_role("button", name="Continue").click()
            page.get_by_role("navigation", name="Workspace sections").get_by_role("link", name="Get started").click()
            step = page.locator('[data-step="review-proposals"]')
            step.get_by_role("link", name="Review the ontology’s proposals").click()
            queue = page.get_by_test_id("review-queue")
            expect(queue).to_be_visible(timeout=30_000)
            first = queue.locator("[data-proposal]").first
            fid = first.get_attribute("data-proposal")
            queue.focus()
            page.keyboard.press("a")
            expect(page.get_by_text("Accepted 1", exact=False)).to_be_visible(timeout=30_000)
            assert s.context.get(fid)["status"] == "ACCEPTED"
            page.get_by_role("navigation", name="Workspace sections").get_by_role("link", name="Get started").click()
            expect(page.locator('[data-step="review-proposals"]')).to_have_attribute("data-done", "true", timeout=30_000)
        finally:
            browser.close()
    assert not errors, errors


# The compiler's fixtures, used only by the test above (pytest finds them here; they start nothing unless asked).
if _ontology_ready():
    from test_ontology_e2e import cairn, compiled, provider, stack  # noqa: E402,F401
    from test_trino_e2e import lake, trino  # noqa: E402,F401
