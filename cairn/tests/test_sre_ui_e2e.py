"""The SRE agent in Chromium (L142), against a real Cairn API with its database, audit log and policy decisions: an
operator opens an incident the agent worked on, reads its cited finding (the evidence: the tool call and its result),
approves a proposed runbook action (a silence for 30 minutes), which runs as the operator and is kept and audited as
theirs; someone without MANAGE_DATABASE_INSTANCE is refused a proposed backup, which stays pending; without a model the
panel says why the agent cannot work; axe finds no serious or critical problem in the incident with the agent's panel.

The proposals are the agent's records as its tools write them (the model's part is tests/test_sre_agent_e2e.py's).
Needs Playwright (with Chromium), the built bundle (ui/dist/mfe, `npm run build:mfe`) and ui/node_modules/axe-core."""

from __future__ import annotations

import os
import re
import socket
import threading
import time

import pytest

from test_observability_ui_e2e import BUNDLE, HOST, _port, _signed_in, until

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AXE = os.path.join(ROOT, "ui", "node_modules", "axe-core", "axe.min.js")
OPS, BOB = "urn:li:corpuser:ops", "urn:li:corpuser:bob"
EVIDENCE = [{"id": "e2", "tool": "promql", "input": {"expr": "sum(rate(http_errors_total[5m]))"},
             "result": '{"matches": [{"labels": {}, "value": 7.5}], "count": 1}'}]


def _ready():
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return os.path.exists(os.path.join(BUNDLE, "remoteEntry.js")) and os.path.exists(AXE)


pytestmark = pytest.mark.skipif(not _ready(), reason="needs Playwright, the built UI bundle and axe-core")


@pytest.fixture
def cairn(settings, graph, monkeypatch):
    import uvicorn
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    from cairn.app import create_api_app
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.services import build_services
    from cairn.sre.agent import ACTOR, _Run

    for k in ("CAIRN_OBS_PROMETHEUS_URL", "CAIRN_OBS_LOKI_URL", "CAIRN_OBS_TEMPO_URL", "CAIRN_SRE_AGENT_MODEL"):
        monkeypatch.delenv(k, raising=False)
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    me = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="ops", effect="allow", actions=["*"], condition=me(OPS)),
        Policy(id="bob", effect="allow", actions=["MANAGE_SYSTEM_OPERATIONS"], condition=me(BOB))]))
    for urn, name in ((OPS, "Olu Ops"), (BOB, "Bob Stone")):
        graph.aspects[(urn, "corpUserInfo")] = {"active": True, "displayName": name}
    inc = s.observability.open_incident(OPS, {"title": "Checkout errors", "severity": "critical",
                                              "services": ["checkout"]})
    iid = inc["id"]
    # What the agent's tools record: a finding citing its evidence, a silence and a backup proposed.
    run = _Run("run_ui", "investigate", iid, OPS, None, s.sre.settings())
    run.steps.append({"id": "e2", "tool": "promql", "input": EVIDENCE[0]["input"], "ok": True,
                      "result": EVIDENCE[0]["result"], "ms": 5})
    s.sre._note(run, "finding", "checkout answers 7.5 errors a second since 10:02", ["e2"])
    silence = s.sre._action_row(run, iid, kind="runbook_action", action="silence_alert",
                                params={"alertname": "Checkout errors", "minutes": 30}, title="Silence Checkout errors 30 min",
                                rationale="a known upstream outage; the page repeats every 4 hours", evidence=EVIDENCE)
    backup = s.sre._action_row(run, iid, kind="runbook_action", action="backup_instance", params={"instance": "orders-pg"},
                               title="Back up orders-pg now", rationale="the last backup is two days old", evidence=[])
    assert ACTOR.startswith("urn:li:")
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
    yield s, f"http://127.0.0.1:{p}", iid, silence["id"], backup["id"]
    server.should_exit = True


def axe(page) -> list[dict]:
    page.add_script_tag(path=AXE)
    result = page.evaluate("""async () => {
        const r = await axe.run(document, {runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']},
                                           resultTypes: ['violations']});
        return r.violations.map(v => ({id: v.id, impact: v.impact, help: v.help,
                                       nodes: v.nodes.slice(0, 5).map(n => n.target.join(' ') + ' :: ' + n.failureSummary)}));
    }""")
    return [v for v in result if v["impact"] in ("serious", "critical")]


def test_an_operator_approves_a_proposal_someone_without_the_privilege_is_refused(cairn):
    from playwright.sync_api import expect, sync_playwright

    s, url, iid, silence, backup = cairn
    errors: list[str] = []
    with sync_playwright() as pw:
        browser, page = _signed_in(pw, url, OPS)
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(f"{url}/host#/observability?tab=incidents&incident={iid}", wait_until="domcontentloaded")
            inc = page.get_by_role("region", name="Incident Checkout errors")
            expect(inc).to_be_visible(timeout=30_000)
            panel = inc.get_by_role("region", name="SRE agent")
            expect(panel).to_be_visible(timeout=20_000)
            expect(panel.get_by_text("no model is configured for the SRE agent", exact=False)).to_be_visible()
            timeline = inc.get_by_role("region", name="Timeline")
            expect(timeline.get_by_text("checkout answers 7.5 errors a second since 10:02")).to_be_visible()
            timeline.get_by_text("Evidence (1)").click()
            expect(timeline.get_by_text("sum(rate(http_errors_total[5m]))", exact=False)).to_be_visible()
            found = axe(page)
            assert not found, found

            prop = panel.get_by_role("group", name="Proposal Silence Checkout errors 30 min")
            expect(prop.get_by_text("waits for approval")).to_be_visible()
            prop.get_by_role("button", name="Approve Silence Checkout errors 30 min").click()
            page.get_by_role("button", name="Approve", exact=True).click()       # it runs now, as you
            expect(prop.get_by_text("done")).to_be_visible(timeout=20_000)
            made = until(lambda: [x for x in s.observability.list("silence") if x["created_by"] == OPS])
            assert made and made[0]["matchers"][0] == {"name": "alertname", "value": "Checkout errors", "regex": False}
            assert s.sre.action(silence)["status"] == "executed" and s.sre.action(silence)["decided_by"] == OPS
            assert any(e["action"] == "observability.silence_created" and e["actor"] == OPS
                       for e in s.audit.entries("observability", 50))
            expect(timeline.get_by_text(re.compile("approved and ran Silence Checkout errors"))).to_be_visible(timeout=20_000)
        finally:
            browser.close()

        browser, page = _signed_in(pw, url, BOB)
        try:
            page.goto(f"{url}/host#/observability?tab=incidents&incident={iid}", wait_until="domcontentloaded")
            panel = page.get_by_role("region", name="SRE agent")
            expect(panel).to_be_visible(timeout=30_000)
            prop = panel.get_by_role("group", name="Proposal Back up orders-pg now")
            prop.get_by_role("button", name="Approve Back up orders-pg now").click()
            page.get_by_role("button", name="Approve", exact=True).click()
            expect(page.get_by_text(re.compile("It needs MANAGE_DATABASE_INSTANCE"))).to_be_visible(timeout=20_000)
            expect(prop.get_by_text("waits for approval")).to_be_visible()
            assert s.sre.action(backup)["status"] == "pending"
            assert any(e["action"] == "sre.action_refused" and e["actor"] == BOB
                       for e in s.audit.entries("observability", 50))
        finally:
            browser.close()
    assert not errors, errors
