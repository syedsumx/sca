"""Observability in Chromium (L141), against a real Cairn API with its database, audit log and notifications: an
operator opens the section from the sidebar, makes an SLO and an alert rule with the generated forms (kept and
audited), opens an incident an alert opened, reads its timeline (the alert, a deploy, a change), adds a finding and
resolves it; a journey links out to a vendor's trace view; someone without MANAGE_SYSTEM_OPERATIONS is not offered the
section and is refused it. The bundled stack's own pages (health, journeys from Tempo, logs from Loki) are exercised
against the real backends by tests/test_observability_e2e.py; here they say what is not configured.

Needs Playwright (with Chromium) and the built bundle (ui/dist/mfe, `npm run build:mfe`)."""

from __future__ import annotations

import os
import re
import socket
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "ui", "dist", "mfe")
OPS, BOB = "urn:li:corpuser:ops", "urn:li:corpuser:bob"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


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
def cairn(settings, graph, monkeypatch):
    import uvicorn
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    from cairn.app import create_api_app
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.observability.service import _fingerprint
    from cairn.services import build_services

    for k in ("CAIRN_OBS_PROMETHEUS_URL", "CAIRN_OBS_LOKI_URL", "CAIRN_OBS_TEMPO_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CAIRN_OBS_TRACE_URL", "https://vendor.example.test/trace/{trace_id}")
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="ops", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": OPS})]))
    for urn, name in ((OPS, "Olu Ops"), (BOB, "Bob Stone")):
        graph.aspects[(urn, "corpUserInfo")] = {"active": True, "displayName": name}
    # What the evaluator does when a rule's condition holds: an alert fires and opens an incident.
    obs = s.observability
    obs.record_deploy("urn:li:corpuser:ci", {"component": "cairn-store", "version": "2.4.0"})
    s.audit.record("governance", "policy.updated", OPS, "analysts-read", {})
    labels = {"service": "cairn-store"}
    obs._advance("rule:store-errors", {"name": "Store answers errors", "severity": "critical",
                                       "summary": "{{ $labels.service }} answers 7% errors"},
                 {_fingerprint("rule:store-errors", labels): {"labels": labels, "value": 0.07}}, int(time.time() * 1000))
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


def until(fn, timeout=20.0):
    end = time.time() + timeout
    while True:
        out = fn()
        if out or time.time() > end:
            return out
        time.sleep(0.2)


def _signed_in(pw, url, who):
    from playwright.sync_api import expect

    browser = pw.chromium.launch()
    ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": who}, viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.goto(f"{url}/host#/discover", wait_until="domcontentloaded")
    role = page.get_by_role("dialog", name="Welcome to Cairn. What do you mostly do here?")
    expect(role).to_be_visible(timeout=30_000)
    role.get_by_role("radio", name=re.compile("^Admin")).check()
    role.get_by_role("button", name="Continue").click()
    expect(role).to_be_hidden(timeout=20_000)
    return browser, page


def test_slos_rules_incidents_and_journeys_in_the_browser(cairn):
    from playwright.sync_api import expect, sync_playwright

    s, url = cairn
    with sync_playwright() as pw:
        browser, page = _signed_in(pw, url, OPS)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            nav = page.get_by_role("navigation", name="Workspace sections")
            nav.get_by_role("link", name="Observability").first.click()
            expect(page.get_by_role("heading", name="Observability", level=1)).to_be_visible(timeout=20_000)
            expect(page.get_by_text("1 firing")).to_be_visible(timeout=20_000)
            expect(page.get_by_text("Not configured").first).to_be_visible(timeout=20_000)    # no metrics backend here

            # ---- an SLO with the generated form ----------------------------------------------------------------------
            page.get_by_role("tab", name="SLOs").click()
            page.get_by_role("button", name="New SLO").first.click()
            d = page.get_by_role("dialog").last
            expect(d).to_be_visible()
            d.get_by_label(re.compile(r"^ID")).fill("store-availability")
            d.get_by_label(re.compile(r"^Name")).fill("Store availability")
            d.get_by_label(re.compile(r"^Service")).fill("cairn-store")
            d.get_by_role("combobox", name=re.compile(r"^Type")).click()
            page.locator(".ant-select-dropdown:visible .ant-select-item-option", has_text="ratio").first.click()
            sel = 'service_name="cairn-store",span_kind="SPAN_KIND_SERVER"'
            d.get_by_label(re.compile(r"^Errors")).fill(
                f'sum(rate(traces_span_metrics_calls_total{{{sel},status_code="STATUS_CODE_ERROR"}}[$window]))')
            d.get_by_label(re.compile(r"^Total")).fill(f"sum(rate(traces_span_metrics_calls_total{{{sel}}}[$window]))")
            d.get_by_label(re.compile(r"^Objective")).fill("99.5")
            d.get_by_role("button", name=re.compile(r"(^|loading )New SLO$")).last.click()
            slo = until(lambda: next((x for x in s.observability.list("slo") if x["id"] == "store-availability"), None))
            assert slo and slo["service"] == "cairn-store" and slo["indicator"]["type"] == "ratio"
            # The evaluator's own entries (alerts firing, built-ins) share the category: look past them.
            assert any(e["action"] == "observability.slo_created" and e["actor"] == OPS
                       and e["subject"] == "store-availability" for e in s.audit.entries("observability", 1000))
            page.keyboard.press("Escape")
            expect(page.get_by_role("cell", name="Store availability")).to_be_visible(timeout=20_000)

            # ---- an alert rule with the generated form ---------------------------------------------------------------
            page.get_by_role("tab", name="Alerts").click()
            expect(page.get_by_role("cell", name="Store answers errors").first).to_be_visible(timeout=20_000)
            page.get_by_role("button", name="New alert rule").first.click()
            d = page.get_by_role("dialog").last
            d.get_by_label(re.compile(r"^ID")).fill("api-slow")
            d.get_by_label(re.compile(r"^Name")).fill("API is slow")
            d.get_by_label(re.compile(r"^Expr")).fill('histogram_quantile(0.95, sum by (le) (rate(x_bucket[5m]))) > 2')
            d.get_by_role("button", name=re.compile(r"(^|loading )New alert rule$")).last.click()
            rule = until(lambda: next((x for x in s.observability.list("alert_rule") if x["id"] == "api-slow"), None))
            assert rule and rule["expr"].startswith("histogram_quantile")
            page.keyboard.press("Escape")

            # ---- the incident the alert opened: timeline, a finding, resolved ------------------------------------------
            page.get_by_role("tab", name="Incidents").click()
            page.get_by_role("button", name="Open Store answers errors").click()
            inc = page.get_by_role("region", name="Incident Store answers errors")
            expect(inc).to_be_visible(timeout=20_000)
            timeline = inc.get_by_role("region", name="Timeline")
            expect(timeline.get_by_text("cairn-store 2.4.0 deployed")).to_be_visible()
            expect(timeline.get_by_text(re.compile("Store answers errors fired: cairn-store answers 7% errors"))).to_be_visible()
            expect(timeline.get_by_text("policy.updated analysts-read")).to_be_visible()
            inc.get_by_label("What you found or did").fill("the 2.4.0 deploy changed the store's disk layout")
            inc.get_by_role("button", name="Add").click()
            expect(timeline.get_by_text("the 2.4.0 deploy changed the store's disk layout")).to_be_visible(timeout=20_000)
            inc.get_by_label("What happened and what fixed it").fill("rolled back to 2.3.9")
            inc.get_by_role("button", name="Resolve").click()
            iid = s.observability.incidents()[0]["id"]
            assert until(lambda: s.observability.incident(iid)["status"] == "resolved")
            assert s.observability.incident(iid)["summary"] == "rolled back to 2.3.9"
            expect(inc.get_by_role("button", name="Reopen")).to_be_visible(timeout=20_000)
            page.keyboard.press("Escape")

            # ---- a journey with a vendor backend: a link out ----------------------------------------------------------
            page.get_by_role("tab", name="Request journey").click()
            page.get_by_label("Trace id").first.fill(TRACE)
            page.get_by_role("button", name="Show the journey").click()
            link = page.get_by_role("link", name=re.compile("^Open in"))
            expect(link).to_have_attribute("href", f"https://vendor.example.test/trace/{TRACE}", timeout=20_000)
        finally:
            browser.close()
    assert not errors, errors


def test_without_system_operations_the_section_is_not_offered_and_is_refused(cairn):
    from playwright.sync_api import expect, sync_playwright

    _s, url = cairn
    with sync_playwright() as pw:
        browser, page = _signed_in(pw, url, BOB)
        try:
            nav = page.get_by_role("navigation", name="Workspace sections")
            expect(nav).to_be_visible(timeout=20_000)
            expect(nav.get_by_role("link", name="Observability")).to_have_count(0)
            page.goto(f"{url}/host#/observability", wait_until="domcontentloaded")
            expect(page.get_by_text("Observability is for operators (MANAGE_SYSTEM_OPERATIONS).")).to_be_visible(
                timeout=30_000)
        finally:
            browser.close()
