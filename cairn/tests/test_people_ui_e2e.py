"""People & roles in Chromium (L139), against a real Cairn API with its policy decision point, database and services:
an administrator opens the section from the sidebar, opens a person, reads why they hold each role and what they can
see, checks their access to two datasets (yes through a persona, no through a purpose), makes a person with the
generated form and deactivates them; someone without MANAGE_POLICIES is not offered the section and is refused it.

The built UI bundle (ui/dist/mfe, `npm run build:mfe`) is mounted the way Cairn Core's host mounts it. Cairn Core is not
part of this stack: people and groups are in the in-memory catalog Cairn's services read and write (the stack job's
tests/e2e/test_people_live.py runs the API against Cairn Core itself).

Needs Playwright (with Chromium) and the built bundle."""

from __future__ import annotations

import os
import re
import socket
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "ui", "dist", "mfe")
ADMIN, BOB, DANA = "urn:li:corpuser:admin", "urn:li:corpuser:bob", "urn:li:corpuser:dana.lee"
LEDGER = "urn:li:dataset:(urn:li:dataPlatform:postgres,finance.public.ledger,PROD)"
PAYROLL = "urn:li:dataset:(urn:li:dataPlatform:postgres,finance.public.payroll,PROD)"
CHECK = re.compile(r"^(loading )?Check$")     # without the host's stylesheet antd's loading icon never leaves the name


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
def cairn(settings, graph):
    import uvicorn
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    from cairn.app import create_api_app
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.governance.personas import Members, PersonaSpec, PurposeSpec, Scope
    from cairn.identity.people import GroupIn, PersonIn
    from cairn.services import build_services

    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="admins", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    for urn, name in ((ADMIN, "Admin"), (BOB, "Bob Stone")):
        graph.aspects[(urn, "corpUserInfo")] = {"active": True, "displayName": name}
    for ds in (LEDGER, PAYROLL):
        graph.aspects[(ds, "domains")] = {"domains": ["urn:li:domain:finance"]}
    graph.aspects[(PAYROLL, "glossaryTerms")] = {"terms": [{"urn": "urn:li:glossaryTerm:Classification.PII.Salary"}]}
    s.personas.upsert_persona(PersonaSpec(name="Finance analysts", permissions=["view", "data_access"],
                                          members=Members(groups=["urn:li:corpGroup:finance-analysts"]),
                                          scope=Scope(domains=["urn:li:domain:finance"])), ADMIN)
    s.personas.upsert_purpose(PurposeSpec(name="Payroll", tags=["PII.*"], deny=["view", "data_access"],
                                          allowed={"groups": ["urn:li:corpGroup:hr"]}), ADMIN)
    s.people.create_person(PersonIn(id="dana.lee", display_name="Dana Lee", email="dana.lee@example.com",
                                    department="Finance", employment_type="contractor"), ADMIN)
    s.people.create_group(GroupIn(id="finance-analysts", name="Finance analysts"), ADMIN)
    s.people.add_member("finance-analysts", "dana.lee", ADMIN)
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


def test_people_person_why_and_the_access_check(cairn):
    from playwright.sync_api import expect, sync_playwright

    s, url = cairn
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": ADMIN}, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(f"{url}/host#/discover", wait_until="domcontentloaded")
            role = page.get_by_role("dialog", name="Welcome to Cairn. What do you mostly do here?")
            expect(role).to_be_visible(timeout=30_000)
            role.get_by_role("radio", name=re.compile("^Admin")).check()
            role.get_by_role("button", name="Continue").click()
            expect(role).to_be_hidden(timeout=20_000)
            nav = page.get_by_role("navigation", name="Workspace sections")
            nav.get_by_role("link", name="People & roles").first.click()
            expect(page.get_by_role("heading", name="People & roles", level=1)).to_be_visible(timeout=20_000)

            # ---- People → a person → why --------------------------------------------------------------------------
            page.get_by_role("button", name="Open Dana Lee").click()
            person = page.get_by_role("region", name="Person Dana Lee")
            expect(person).to_be_visible(timeout=20_000)
            expect(person.get_by_text("Why: through the group finance-analysts")).to_be_visible()
            expect(person.get_by_text(re.compile(r"Finance analysts: domains: finance"))).to_be_visible(timeout=20_000)
            expect(person.get_by_text(re.compile(r"Payroll \(PII\.\*\): denied"))).to_be_visible()
            expect(person.get_by_text("contractor")).to_be_visible()                  # the attribute policies read

            # ---- Check access from the person's page: yes through the persona -----------------------------------------
            person.get_by_role("button", name="Check access").click()
            expect(page.get_by_role("tab", name="Access check", selected=True)).to_be_visible(timeout=10_000)
            page.get_by_role("combobox", name="Can do").click()
            page.locator(".ant-select-dropdown:visible .ant-select-item-option", has_text="query its data").first.click()
            page.get_by_label("On (dataset or other asset, optional)").fill(LEDGER)
            page.get_by_role("button", name=CHECK).click()
            answer = page.get_by_role("status", name="Access check answer")
            expect(answer.get_by_text("Yes", exact=True)).to_be_visible(timeout=20_000)
            expect(answer.get_by_text("persona Finance analysts", exact=True)).to_be_visible()
            expect(answer.get_by_text("persona:finance-analysts")).to_be_visible()

            # ... and no on the payroll table: the purpose denies it, whatever the persona allows.
            page.get_by_label("On (dataset or other asset, optional)").fill(PAYROLL)
            page.get_by_role("button", name=CHECK).click()
            expect(answer.get_by_text("No", exact=True)).to_be_visible(timeout=20_000)
            expect(answer.get_by_text("purpose Payroll", exact=True)).to_be_visible()
            checks = [e for e in s.audit.entries("people", 20) if e["action"] == "access.checked"]
            assert [c["details"]["decision"] for c in checks[:2]] == ["DENY", "ALLOW"] and checks[0]["actor"] == ADMIN

            # ---- A person made with the generated form, then deactivated ---------------------------------------------
            page.get_by_role("tab", name="People").click()
            page.get_by_role("button", name="New person").first.click()
            d = page.get_by_role("dialog").last
            expect(d).to_be_visible()
            d.get_by_label(re.compile(r"^ID")).fill("erin.new")
            d.get_by_label(re.compile(r"^Display name")).fill("Erin New")
            d.get_by_label(re.compile(r"^Department")).fill("Risk")
            d.get_by_role("button", name=re.compile(r"(^|loading )New person$")).last.click()
            made = until(lambda: s.graph.aspects.get(("urn:li:corpuser:erin.new", "corpUserInfo")))
            assert made and made["displayName"] == "Erin New" and made["departmentName"] == "Risk"
            page.keyboard.press("Escape")
            page.get_by_role("button", name="Open Erin New").click()
            erin = page.get_by_role("region", name="Person Erin New")
            expect(erin).to_be_visible(timeout=20_000)
            erin.get_by_role("button", name="Deactivate").click()
            page.locator(".ant-popover:visible").get_by_role("button", name="Deactivate").click()
            assert until(lambda: (s.graph.aspects.get(("urn:li:corpuser:erin.new", "corpUserStatus")) or {})
                         .get("status") == "SUSPENDED")
            expect(erin.get_by_text("Deactivated", exact=True)).to_be_visible(timeout=20_000)
            expect(erin.get_by_role("cell", name="person.deactivated")).to_be_visible()      # the audit trail
        finally:
            browser.close()
    assert not errors, errors


def test_without_manage_policies_the_section_is_not_offered_and_is_refused(cairn):
    from playwright.sync_api import expect, sync_playwright

    _s, url = cairn
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(extra_http_headers={"X-Cairn-Actor": BOB}, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(f"{url}/host#/people", wait_until="domcontentloaded")
            expect(page.get_by_text("People & roles is for administrators (MANAGE_POLICIES).")).to_be_visible(
                timeout=30_000)
            nav = page.get_by_role("navigation", name="Workspace sections")
            expect(nav).to_be_visible()
            page.wait_for_load_state("networkidle")
            # L150: open the Govern group, so the absence below means "not offered", not "folded away".
            govern = nav.get_by_role("button", name="Govern", exact=True)
            if govern.count() and govern.get_attribute("aria-expanded") != "true":
                govern.click()
                expect(govern).to_have_attribute("aria-expanded", "true")
            assert nav.get_by_role("link", name="People & roles").count() == 0
        finally:
            browser.close()
