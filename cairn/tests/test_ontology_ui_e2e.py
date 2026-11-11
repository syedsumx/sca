"""The Ontology explorer (L129) in Chromium against a real Cairn with a compiled ontology: the built UI bundle
(ui/dist/mfe, `npm run build:mfe`) mounted the way Cairn Core's host mounts it, over test_ontology_e2e's stack (the
real compiler on PostgreSQL, Cairn Store, Cairn Catalog, Trino, and the AI gateway to the provider stand-in).

As a steward: the explorer opens, a term is searched, zoomed to and its details shown beside the graph; its page
opens; a proposal is accepted from the review queue with the keyboard (and is accepted in Cairn); the compile wizard
runs a full compile of one table and shows its progress to completion and the diff; an asset page shows the
table's business terms. axe checks the explorer's tabs and the term page with real data.

The asset page's header comes from Cairn Core's GraphQL, which is not part of this stack: a minimal answer for that
one query stands in for it (the business terms on it come from the real API).

Needs Playwright (with Chromium), the built bundle, docker, CAIRN_STORE_BIN, CAIRN_CATALOG_BIN and
CAIRN_ONTOLOGY_BIN."""

from __future__ import annotations

import json
import mimetypes
import os
import urllib.parse

import pytest

import test_trino_e2e
from test_ontology_e2e import CUSTOMERS, ORDERS, STEW, cairn, compiled, provider, stack  # noqa: F401 - fixtures
from test_trino_e2e import lake, trino  # noqa: F401 - fixtures

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "ui", "dist", "mfe")


def _ready():
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return (os.path.exists(os.path.join(BUNDLE, "remoteEntry.js")) and test_trino_e2e._ready()
            and bool(os.environ.get("CAIRN_ONTOLOGY_BIN")))


pytestmark = pytest.mark.skipif(not _ready(), reason="needs Playwright, the built UI bundle (npm run build:mfe), docker, "
                                                     "CAIRN_STORE_BIN, CAIRN_CATALOG_BIN and CAIRN_ONTOLOGY_BIN")

HOST = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Cairn</title></head>
<body><div id="root"></div><script src="/ui/remoteEntry.js"></script><script>
(async () => {
  await cairnConsumer.init({});
  const mount = (await cairnConsumer.get('./mount'))().mount;
  mount(document.getElementById('root'), { apiBaseUrl: window.location.origin, catalogUrl: null });
})();
</script></body></html>"""


def _serve(page, url: str) -> None:
    """The host page and the bundle from disk, on the API's origin (as Cairn Core serves them)."""
    page.route(f"{url}/host", lambda r: r.fulfill(body=HOST, content_type="text/html"))

    def bundle(route):
        rel = urllib.parse.urlparse(route.request.url).path.removeprefix("/ui/")
        path = os.path.normpath(os.path.join(BUNDLE, rel))
        if not path.startswith(BUNDLE) or not os.path.isfile(path):
            return route.fulfill(status=404, body="")
        with open(path, "rb") as fh:
            route.fulfill(body=fh.read(), content_type=mimetypes.guess_type(path)[0] or "application/octet-stream")
    page.route(f"{url}/ui/**", bundle)

    def graphql(route):
        body = json.loads(route.request.post_data or "{}")
        urn = (body.get("variables") or {}).get("urn")
        if "cairnConsumerAsset" in body.get("query", "") and urn:
            name = urn.split(",")[1].split(".")[-1]
            return route.fulfill(content_type="application/json", body=json.dumps({"data": {"entity": {
                "urn": urn, "type": "DATASET", "name": name, "platform": {"name": "iceberg"},
                "properties": {"name": name, "description": ""}}}}))
        return route.fulfill(content_type="application/json", body=json.dumps({"data": {}}))
    page.route(f"{url}/api/v2/graphql", graphql)


def test_explore_review_compile_and_see_terms_on_an_asset(stack, compiled):  # noqa: F811
    from playwright.sync_api import expect, sync_playwright

    from test_accessibility_e2e import AXE, audit

    url, s = stack["url"], stack["s"]
    found: dict[str, list] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": STEW}, viewport={"width": 1600, "height": 1000},
                                  accept_downloads=True)
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        _serve(page, url)

        def axe(name):
            if os.path.exists(AXE):
                page.wait_for_load_state("networkidle")
                bad = audit(page)
                if bad:
                    found[name] = bad
        try:
            # The explorer: search a term, zoom to it, its details beside the graph.
            page.goto(f"{url}/host#/ontology", wait_until="domcontentloaded")
            # /ontology is Semantics › Ontology (L149)
            expect(page.get_by_role("heading", name="Semantics", exact=True)).to_be_visible(timeout=30_000)
            canvas = page.get_by_test_id("ontology-canvas")
            expect(canvas).to_be_visible(timeout=30_000)
            expect(canvas.locator('[data-node]').first).to_be_visible()
            axe("graph")
            page.get_by_role("combobox", name="Find a term").fill("Custom")
            page.locator(".ant-select-item-option-content", has_text="Customer").first.click()
            detail = page.get_by_test_id("term-detail")
            expect(detail).to_contain_text("Customer", timeout=20_000)
            expect(detail).to_contain_text("signup_day")
            expect(detail).to_contain_text("YYYY-MM-DD")
            pressed = canvas.locator('[aria-pressed="true"]')
            expect(pressed).to_have_count(1)
            assert "Customer" in pressed.get_attribute("aria-label")
            # Its page.
            page.get_by_role("link", name="Open the term page").click()
            page.wait_for_function("location.hash.startsWith('#/ontology/terms/')", timeout=10_000)
            expect(page.get_by_role("heading", name="Customer", exact=True)).to_be_visible(timeout=20_000)
            expect(page.get_by_text("Mapped columns")).to_be_visible()
            expect(page.get_by_text("Provenance")).to_be_visible()
            axe("term page")
            # The list alternative.
            page.goto(f"{url}/host#/ontology?tab=list", wait_until="domcontentloaded")
            expect(page.get_by_role("link", name="Purchase Order (retail.orders)").first).to_be_visible(timeout=30_000)
            axe("list")

            # The review queue, with the keyboard: j moves down, a accepts the row under the cursor.
            page.goto(f"{url}/host#/ontology?tab=review", wait_until="domcontentloaded")
            queue = page.get_by_test_id("review-queue")
            expect(queue).to_be_visible(timeout=30_000)
            axe("review")
            page.get_by_label("Review comment").fill("checked in the browser")
            queue.focus()
            page.keyboard.press("j")
            current = queue.locator('[aria-current="true"]')
            fid = current.get_attribute("data-proposal")
            assert fid and fid == queue.locator("[data-proposal]").nth(1).get_attribute("data-proposal")
            page.keyboard.press("a")
            expect(page.get_by_text("Accepted 1", exact=False)).to_be_visible(timeout=30_000)
            expect(queue.locator(f'[data-proposal="{fid}"]')).to_have_count(0)
            assert s.context.get(fid)["status"] == "ACCEPTED"
            audit_log = s.audit.entries("ontology", limit=50)
            assert any(e["subject"] == fid and e["actor"] == STEW and e["details"]["comment"] == "checked in the browser"
                       for e in audit_log)

            # The compile wizard: one table, full; the run progresses to the end, then the diff.
            page.goto(f"{url}/host#/ontology?tab=compile", wait_until="domcontentloaded")
            wizard = page.get_by_test_id("compile-wizard")
            expect(wizard).to_be_visible(timeout=30_000)
            axe("compile")
            page.get_by_label("Some tables").check()
            page.get_by_role("combobox", name="Tables").click()          # antd opens the picker, then takes typing
            page.keyboard.type("retail.customers")
            page.locator(".ant-select-item-option-content", has_text="retail.customers").first.click()
            page.keyboard.press("Escape")
            page.get_by_role("button", name="Next").click()
            page.get_by_label("Full: compile every table in scope again").check()
            page.get_by_role("button", name="Next").click()
            expect(wizard.get_by_text("Tables to compile")).to_be_visible(timeout=30_000)
            expect(wizard.get_by_text(r"up to 4 (4 a table)", exact=False)).to_be_visible()
            page.get_by_role("button", name="Run").click()
            state = page.get_by_test_id("run-state")
            expect(state).to_be_visible(timeout=30_000)
            expect(state).to_have_text("completed", timeout=300_000)
            expect(page.get_by_test_id("run-diff")).to_be_visible(timeout=60_000)
            expect(page.get_by_text("new proposals", exact=False)).to_be_visible()
            runs = s.ontology.client.runs()["runs"]
            assert runs[0]["state"] == "completed" and runs[0]["stats"]["scope"] == [CUSTOMERS], runs[0]

            # An asset page: the table's business terms, per column, linked to the term pages.
            page.goto(f"{url}/host#/asset/{urllib.parse.quote(ORDERS, safe='')}", wait_until="domcontentloaded")
            terms = page.get_by_test_id("business-terms")
            expect(terms).to_be_visible(timeout=30_000)
            expect(terms.get_by_role("link", name="Purchase Order (retail.orders)").first).to_be_visible()
            expect(terms).to_contain_text("order_day")
            expect(terms).to_contain_text("DD/MM/YYYY")
            terms.get_by_role("link", name="Purchase Order (retail.orders)").first.click()
            page.wait_for_function("location.hash.startsWith('#/ontology/terms/')", timeout=10_000)
            expect(page.get_by_test_id("term-detail")).to_contain_text("buyer_ref", timeout=20_000)
        finally:
            browser.close()
    assert not errors, errors
    assert not found, "axe found serious or critical problems:\n" + json.dumps(found, indent=1)
