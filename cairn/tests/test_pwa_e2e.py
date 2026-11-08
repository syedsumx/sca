"""The installable workspace and the Profile page (L119), in Chromium against a real Cairn.

The built UI bundle (``ui/dist/mfe``, with the app shell from ``ui/pwa``) is served at ``/cairn-ui`` and Cairn's real
API at ``/cairn/api`` on the same origin, as the ingress serves them. Checked:

- the web app manifest (name, scope, start URL, standalone, 192 and 512 icons that are real PNGs of those sizes);
- the service worker registers for ``/cairn-ui/``, controls the page, caches the app shell and no API response;
- offline, opening the app shows the offline page, and the API is not answered from a cache;
- the Profile page round-trips through the real API (and the profile is what the API then returns);
- at phone width (390 px) the main pages fit without sideways scrolling, the sidebar is a drawer, and the Profile
  page passes axe (no serious or critical WCAG 2.1 A/AA violations).

Needs Playwright with Chromium and the built bundle (``npm run build:mfe`` in ui)."""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "ui", "dist", "mfe")
AXE = os.path.join(ROOT, "ui", "node_modules", "axe-core", "axe.min.js")
ANA = "urn:li:corpuser:ana"
DS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"
PHONE_PAGES = ["/discover", "/search?q=orders", "/sql", "/dashboards", "/assistant", "/profile", "/settings",
               "/marketplace", f"/asset/{DS}"]


def _ready():
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return all(os.path.exists(os.path.join(BUNDLE, f)) for f in ("remoteEntry.js", "app.html", "sw.js")) and os.path.exists(AXE)


pytestmark = pytest.mark.skipif(not _ready(), reason="needs Playwright and the built UI bundle (npm run build:mfe)")


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def cairn(settings, graph, tmp_path, monkeypatch):
    import uvicorn
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
    from sqlalchemy import create_engine, text

    from cairn.app import create_api_app
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.governance.personas import Members, PersonaSpec
    from cairn.services import build_services

    wh = tmp_path / "wh.db"
    with create_engine(f"sqlite:///{wh}").begin() as c:
        c.execute(text("CREATE TABLE orders (id INTEGER, region TEXT, amount REAL)"))
        c.execute(text("INSERT INTO orders VALUES (1, 'EU', 9.5), (2, 'US', 3.0)"))
    conns = tmp_path / "connections.yaml"
    conns.write_text(f"connections:\n  wh:\n    url: sqlite:///{wh}\n    catalog:\n      platform: sqlite\n")
    monkeypatch.setenv("CAIRN_QUALITY_CONNECTIONS", str(conns))
    graph.aspects[(DS, "schemaMetadata")] = {"fields": [{"fieldPath": c, "nativeDataType": "TEXT"} for c in ("id", "region", "amount")]}
    graph.aspects[(DS, "datasetProperties")] = {"name": "orders", "description": "One row per order."}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="all", effect="allow", actions=["*"])]))
    s.personas.upsert_persona(PersonaSpec(name="Finance analyst", members=Members(users=[ANA])), "urn:li:corpuser:admin")
    root = FastAPI()
    root.mount("/cairn/api", create_api_app(s))
    root.mount("/cairn-ui", StaticFiles(directory=BUNDLE))
    p = _port()
    running: list = []

    def start():
        server = uvicorn.Server(uvicorn.Config(root, host="127.0.0.1", port=p, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        end = time.time() + 30
        while not server.started:
            assert time.time() < end
            time.sleep(0.05)
        running.append((server, thread))

    def stop():
        """The network goes away: Cairn's address refuses connections."""
        server, thread = running.pop()
        server.should_exit = True
        thread.join(10)

    start()
    yield {"url": f"http://127.0.0.1:{p}", "s": s, "start": start, "stop": stop}
    while running:
        stop()


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def _overflow(page) -> int:
    return page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")


def test_manifest_service_worker_offline_shell_and_profile(cairn):
    from playwright.sync_api import sync_playwright

    url = cairn["url"]
    errors: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": ANA}, viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{url}/cairn-ui/app.html#/discover")
        page.wait_for_selector("nav[aria-label]", timeout=30000)

        # The manifest, as the browser finds it.
        href = page.eval_on_selector("link[rel=manifest]", "l => l.href")
        manifest = httpx.get(href).json()
        assert manifest["name"] == "Cairn" and manifest["display"] == "standalone"
        assert manifest["scope"] == "./" and manifest["start_url"].startswith("./app.html")
        sizes = {}
        for icon in manifest["icons"]:
            got = httpx.get(f"{url}/cairn-ui/{icon['src']}")
            assert got.status_code == 200 and got.headers["content-type"] == "image/png"
            sizes[(icon["sizes"], icon.get("purpose", "any"))] = _png_size(got.content)
        assert sizes[("192x192", "any")] == (192, 192) and sizes[("512x512", "any")] == (512, 512)
        assert sizes[("512x512", "maskable")] == (512, 512)

        # The service worker: registered for the UI's scope, controlling the page after a reload.
        scope = page.evaluate("async () => (await navigator.serviceWorker.ready).scope")
        assert scope == f"{url}/cairn-ui/"
        page.reload()
        page.wait_for_selector("nav[aria-label]", timeout=30000)
        page.wait_for_function("() => !!navigator.serviceWorker.controller", timeout=15000)
        cached = page.evaluate("""async () => {
            const out = [];
            for (const k of await caches.keys()) for (const r of await (await caches.open(k)).keys()) out.push(new URL(r.url).pathname);
            return out;
        }""")
        assert "/cairn-ui/app.html" in cached and "/cairn-ui/remoteEntry.js" in cached and "/cairn-ui/offline.html" in cached
        assert all(p.startswith("/cairn-ui/") for p in cached), cached          # never an API response
        assert any(p.endswith(".js") and p.count(".") == 2 for p in cached)       # the hashed chunks the page loaded

        # The Profile page, through the real API.
        page.goto(f"{url}/cairn-ui/app.html#/profile")
        page.get_by_label("Role", exact=True).fill("Financial analyst")
        page.get_by_label("Team", exact=True).fill("FP&A")
        page.get_by_role("textbox", name="Standing instructions").fill("Start with the number.")
        page.get_by_role("combobox", name="Default persona").click()
        page.get_by_text("Finance analyst", exact=True).last.click()
        page.get_by_role("textbox", name="What to pin (an urn or id)").fill(DS)
        page.get_by_label("Label", exact=True).fill("Orders")
        page.get_by_role("button", name="Pin", exact=True).click()
        page.get_by_role("checkbox", name="Email").check()
        page.get_by_role("button", name="Save profile").click()
        page.get_by_text("Profile saved").wait_for()
        stored = httpx.get(f"{url}/cairn/api/v1/me/profile", headers={"X-Cairn-Actor": ANA}).json()
        assert (stored["role"], stored["team"], stored["instructions"]) == ("Financial analyst", "FP&A", "Start with the number.")
        assert stored["default_persona"] and stored["pinned"] == [{"kind": "asset", "ref": DS, "label": "Orders"}]
        assert stored["notifications"]["channels"]["email"] is True
        page.reload()
        assert page.get_by_label("Role", exact=True).input_value() == "Financial analyst"
        assert page.get_by_role("link", name="Orders").count() == 1
        page.get_by_role("link", name="Profile").first.wait_for()                # in the sidebar, under Platform

        # Offline (Cairn unreachable): opening the app shows the offline page from the service worker's cache, and
        # the API is not answered from any cache.
        cairn["stop"]()
        page.goto(f"{url}/cairn-ui/app.html?opened=offline#/discover")
        page.get_by_role("heading", name="You’re offline").wait_for()
        assert page.locator("img").first.evaluate("i => i.complete && i.naturalWidth") == 192        # cached icon
        api = page.evaluate("async () => { try { await fetch('/cairn/api/v1/me/profile'); return 'answered'; } "
                            "catch (e) { return 'failed'; } }")
        assert api == "failed"
        # Back online: Try again opens the app.
        cairn["start"]()
        page.get_by_role("button", name="Try again").click()
        page.wait_for_selector("nav[aria-label]", timeout=30000)
        browser.close()
    assert not errors, errors


def test_phone_width_layouts_and_profile_accessibility(cairn):
    from playwright.sync_api import sync_playwright

    from test_accessibility_e2e import audit

    url = cairn["url"]
    wide: dict[str, int] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        phone = pw.devices["Pixel 7"]
        ctx = browser.new_context(**{**phone, "viewport": {"width": 390, "height": 844}},
                                  extra_http_headers={"X-Cairn-Actor": ANA})
        page = ctx.new_page()
        for path in PHONE_PAGES:
            page.goto(f"{url}/cairn-ui/app.html#{path}")
            page.wait_for_selector("main, [role=main], h1", timeout=30000)
            page.wait_for_timeout(600)
            if _overflow(page) > 1:
                wide[path] = _overflow(page)
        # The sidebar is a drawer: hidden until the menu button opens it.
        page.goto(f"{url}/cairn-ui/app.html#/discover")
        menu = page.get_by_role("button", name="Menu")
        assert menu.is_visible()
        side = page.locator("aside").first
        assert side.bounding_box()["x"] + side.bounding_box()["width"] <= 1
        menu.click()
        page.wait_for_timeout(400)
        assert side.bounding_box()["x"] >= 0
        # The Profile page at phone width passes axe.
        page = ctx.new_page()
        page.goto(f"{url}/cairn-ui/app.html#/profile")
        page.get_by_label("Role", exact=True).wait_for()
        violations = audit(page)
        browser.close()
    assert not wide, f"pages wider than the phone: {json.dumps(wide)}"
    assert not violations, json.dumps(violations, indent=2)
