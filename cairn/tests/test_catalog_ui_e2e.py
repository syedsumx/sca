"""The Catalog section (L147: Cairn Core at the centre), in Chromium, against a real Cairn API.

The stack: the built UI bundle (ui/dist/mfe) mounted the way Cairn Core's host mounts it; Cairn's API with its real
services, database and policy decision point. Data products are made, changed and removed through Cairn's
``/catalog/products`` (written to the metadata graph); contracts are published through ``/quality/contracts`` and
checked against the dataset's schema; a published flow registers itself in the catalog. Cairn Core is not part of
this stack: its GraphQL (the catalog's search and facets, the glossary, domains, column lineage) is answered from the
same in-memory metadata graph Cairn's services write (tests/core_standin.py), so what Cairn writes is what the pages
then read. tests/e2e/test_ui_catalog_live.py runs the same pages against the real Cairn Core in the stack job.

Needs Playwright (with Chromium) and the built bundle (npm run build:mfe)."""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time

import pytest
from fastapi import Request     # at module level: with postponed annotations FastAPI must resolve it here

from core_standin import CoreStandIn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "ui", "dist", "mfe")
AXE = os.path.join(ROOT, "ui", "node_modules", "axe-core", "axe.min.js")
ANA = "urn:li:corpuser:ana"
DS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"
RAW = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.raw_orders,PROD)"
BOARD = "urn:li:dashboard:(looker,sales)"
FINANCE = "urn:li:domain:finance"
NODE, TERM = "urn:li:glossaryNode:Finance", "urn:li:glossaryTerm:Revenue"
PRODUCT = "urn:li:dataProduct:orders-360"


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


def seed(graph):
    a = graph.aspects
    for urn, name, cols in ((DS, "orders", ("id", "region", "amount")), (RAW, "raw_orders", ("id", "amt"))):
        a[(urn, "datasetProperties")] = {"name": name, "description": f"{name}: one row per order."}
        a[(urn, "schemaMetadata")] = {"fields": [{"fieldPath": c, "nativeDataType": "INTEGER" if c == "id" else "TEXT"}
                                                 for c in cols]}
    a[(DS, "domains")] = {"domains": [FINANCE]}
    a[(DS, "glossaryTerms")] = {"terms": [{"urn": TERM}]}
    a[(DS, "ownership")] = {"owners": [{"owner": ANA, "type": "DATAOWNER"}]}
    a[(DS, "upstreamLineage")] = {"upstreams": [{"dataset": RAW, "type": "TRANSFORMED"}], "fineGrainedLineages": [
        {"upstreams": [f"urn:li:schemaField:({RAW},amt)"], "downstreams": [f"urn:li:schemaField:({DS},amount)"],
         "transformOperation": "CAST"}]}
    a[(BOARD, "dashboardInfo")] = {"name": "Sales", "title": "Sales", "description": "Sales by region"}
    a[(FINANCE, "domainProperties")] = {"name": "Finance", "description": "Money in and out"}
    a[(FINANCE, "ownership")] = {"owners": [{"owner": "urn:li:corpuser:olga", "type": "DATAOWNER"}]}
    a[(NODE, "glossaryNodeInfo")] = {"name": "Finance", "definition": "Money words"}
    a[(TERM, "glossaryTermInfo")] = {"name": "Revenue", "definition": "Money **in** from sales", "parentNode": NODE}
    a[(TERM, "ownership")] = {"owners": [{"owner": "urn:li:corpuser:olga", "type": "DATAOWNER"}]}


@pytest.fixture
def cairn(settings, graph, tmp_path, monkeypatch):
    import uvicorn
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    from cairn.app import create_api_app
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.services import build_services

    seed(graph)
    core = CoreStandIn(graph)
    graph.graphql_handler = core
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="all", effect="allow", actions=["*"])]))
    # A flow is in the catalog from its first publish (a pipeline, with its owner).
    s.flows.create(ANA, {"name": "orders-daily", "description": "Orders, every day",
                         "steps": [{"name": "tell", "type": "notify", "message": "done"}]}, publish=True)
    app = create_api_app(s)

    async def graphql(request: Request):
        body = await request.json()
        return JSONResponse({"data": core(body.get("query") or "", body.get("variables") or {})})
    app.add_api_route("/api/v2/graphql", graphql, methods=["POST"])
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
    yield s, graph, f"http://127.0.0.1:{p}"
    server.should_exit = True


def test_the_catalog_section_against_cairns_api(cairn):
    from playwright.sync_api import expect, sync_playwright

    s, graph, url = cairn
    label = lambda text: re.compile(rf"^{re.escape(text)}")   # noqa: E731 - a required field's label ends in *
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": ANA}, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def go(route):
            page.goto(f"{url}/host#{route}", wait_until="networkidle")

        def dialog():
            d = page.get_by_role("dialog").last
            expect(d).to_be_visible()
            return d

        def submit(d, name):
            d.get_by_role("button", name=re.compile(rf"(^|loading ){re.escape(name)}$")).last.click()

        def raw(d, body):
            d.get_by_label("Raw JSON").click()
            d.get_by_label("Request body (JSON)").fill(json.dumps(body))
            d.get_by_label("Raw JSON").click()

        try:
            # ---- the sidebar: Cairn Core first, the rest around it ------------------------------------------------
            go("/discover")
            nav = page.get_by_role("navigation", name="Workspace sections")
            expect(nav).to_be_visible(timeout=30_000)
            groups = [h.strip() for h in nav.get_by_role("heading").all_inner_texts()]
            assert groups[:1] == ["CATALOG"] or groups[:1] == ["Catalog"], groups
            for entry in ("Home", "Search", "Data products", "Glossary", "Domains", "Semantics", "Lineage", "Requests"):   # L149
                expect(nav.get_by_role("link", name=entry, exact=True)).to_be_visible()
            # Home, catalog first: the person's issues and requests from Cairn's records.
            expect(page.get_by_role("heading", name="Issues on your assets")).to_be_visible()
            # An earlier route still opens.
            go("/search?q=orders")
            expect(page.get_by_role("link", name=re.compile("^orders")).first).to_be_visible(timeout=20_000)
            expect(page.get_by_text("Page not found")).to_have_count(0)

            # ---- the catalog: types with counts, a flow among the pipelines, facets, an asset page -----------------
            go("/catalog")
            types = page.get_by_role("group", name="Entity type")
            expect(types.get_by_role("button", name=re.compile(r"^All assets\s*4$"))).to_be_visible(timeout=20_000)
            expect(types.get_by_role("button", name=re.compile(r"^Dataset\s*2$"))).to_be_visible()
            expect(types.get_by_role("button", name=re.compile(r"^Dashboard\s*1$"))).to_be_visible()
            types.get_by_role("button", name=re.compile(r"^Pipeline\s*1$")).click()
            expect(page.get_by_role("link", name=re.compile("^orders-daily"))).to_be_visible()
            types.get_by_role("button", name=re.compile(r"^Dataset")).click()
            page.get_by_role("navigation", name="Search filters").get_by_role("checkbox", name=re.compile("Finance")).check()
            expect(page.get_by_role("link", name=re.compile("^raw_orders"))).to_have_count(0)
            page.get_by_role("link", name=re.compile("^orders")).first.click()
            expect(page.get_by_role("heading", name="orders", level=1)).to_be_visible(timeout=20_000)

            # ---- data products: made with the generated form, changed and removed through Cairn ---------------------
            go("/products")
            expect(page.get_by_text("No data products yet")).to_be_visible(timeout=20_000)
            page.get_by_role("button", name="New data product").click()
            d = dialog()
            d.get_by_label(label("ID")).fill("orders-360")
            d.get_by_label(label("Name")).fill("Orders 360")
            raw(d, {"id": "orders-360", "name": "Orders 360", "description": "Every order", "domain": FINANCE,
                    "owners": [ANA], "assets": [DS, BOARD], "output_ports": [DS]})
            submit(d, "New data product")
            expect(d).to_be_hidden(timeout=20_000)
            assert graph.aspects[(PRODUCT, "domains")] == {"domains": [FINANCE]}
            assert json.loads(graph.aspects[(PRODUCT, "dataProductProperties")]["customProperties"]["cairn.output_ports"]) == [DS]
            row = page.get_by_role("row", name=re.compile("Orders 360"))
            expect(row).to_be_visible(timeout=20_000)
            expect(row.get_by_role("link", name="Finance")).to_be_visible()
            expect(row.get_by_text("1 output ports")).to_be_visible()
            row.get_by_role("link", name="Orders 360").click()
            expect(page.get_by_role("heading", name="Orders 360", level=1)).to_be_visible(timeout=20_000)
            page.get_by_role("tab", name=re.compile("^Assets & ports")).click()      # one product page (L149)
            assets = page.get_by_role("tabpanel")
            expect(assets.get_by_role("row", name=re.compile("^orders")).get_by_text("Output port")).to_be_visible()
            expect(assets.get_by_role("row", name=re.compile("^Sales")).get_by_text("Output port")).to_have_count(0)
            page.get_by_role("button", name="Edit", exact=True).click()
            d = dialog()
            expect(d.get_by_label(label("Name"))).to_have_value("Orders 360")
            d.get_by_label("Description").fill("Every order, by region")
            submit(d, "Edit data product")
            expect(d).to_be_hidden(timeout=20_000)
            assert graph.aspects[(PRODUCT, "dataProductProperties")]["description"] == "Every order, by region"

            # ---- contracts: published with the form, checked against the schema, listed and opened ------------------
            go("/contracts")
            page.get_by_role("button", name="New contract").click()
            d = dialog()
            raw(d, {"dataset": DS, "version": "1.0.0", "owner": ANA,
                    "schema": {"columns": [{"name": "id", "type": "integer", "required": False}]}})
            submit(d, "Publish contract")
            expect(d).to_be_hidden(timeout=20_000)
            row = page.get_by_role("row", name=re.compile("orders"))
            expect(row).to_be_visible(timeout=20_000)
            expect(row.get_by_text(re.compile("^(Passing|Failing|Not checked yet)$"))).to_be_visible()
            status = s.contracts.list()[0]["status"]
            expect(row.get_by_text({"MET": "Passing", "BROKEN": "Failing", "PENDING": "Not checked yet"}[status])).to_be_visible()
            row.get_by_role("link", name="orders").click()
            expect(page.get_by_role("heading", name="Terms")).to_be_visible(timeout=20_000)
            expect(page.locator("code", has_text="id").first).to_be_visible()

            # The product's page lists the contract on its dataset.
            go(f"/products/{PRODUCT}")
            page.get_by_role("tab", name="Contracts & service levels (1)").click()
            expect(page.get_by_role("tabpanel").get_by_role("link", name="orders")).to_be_visible()

            # ---- glossary and domains, from Core, read here ------------------------------------------------------------
            go("/glossary")
            page.get_by_role("link", name=re.compile("^Finance")).click()
            page.get_by_role("link", name="Revenue").click()
            expect(page.get_by_role("heading", name="Revenue", level=1)).to_be_visible(timeout=20_000)
            expect(page.locator("strong", has_text="in").first).to_be_visible()
            expect(page.get_by_text("olga")).to_be_visible()
            expect(page.get_by_role("region", name="Assets using it").get_by_role("link", name=re.compile("^orders"))).to_be_visible()
            go("/domains")
            page.get_by_role("link", name=re.compile("^Finance")).click()
            expect(page.get_by_role("heading", name="Finance", level=1)).to_be_visible(timeout=20_000)
            expect(page.get_by_role("link", name="Orders 360")).to_be_visible()

            # ---- lineage as a section: an asset, and its column lineage --------------------------------------------
            go(f"/lineage?urn={DS}")
            columns = page.get_by_role("region", name="Column-level lineage")
            expect(columns.get_by_text("amount", exact=True)).to_be_visible(timeout=20_000)
            expect(columns.get_by_role("link", name="raw_orders")).to_be_visible()

            # ---- removing the product ---------------------------------------------------------------------------------
            go(f"/products/{PRODUCT}")
            page.get_by_role("button", name="Delete", exact=True).click()
            d = dialog()
            submit(d, "Delete")
            expect(d).to_be_hidden(timeout=20_000)
            assert graph.aspects[(PRODUCT, "status")] == {"removed": True}
            expect(page.get_by_text("No data products yet")).to_be_visible(timeout=20_000)
        finally:
            browser.close()
    assert not errors, errors
