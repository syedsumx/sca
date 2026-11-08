"""Agents and skills in the marketplace, end to end through the HTTP API: Ana deploys an agent app (its real code in
its own process, the model behind Cairn's AI gateway) and lists it; Cal finds it beside data products, requests
access, Ana approves from her inbox, Cal invokes the agent, and the access ends at its expiry (the decision point
stops allowing it, the sweep marks the request expired and takes the grant away). A skill (an installed extension)
is listed and granted the same way, with USE_SKILL on its URN only."""

import httpx
import pytest
from sqlalchemy import select, update

from cairn.governance.abac import Policy, Target
from test_agent_apps import ADMIN, ANA, BOB, stack  # noqa: F401  (fixture)
from test_ai_gateway import SONNET

CAL = "urn:li:corpuser:cal"
pytest.importorskip("anthropic")


def h(who):
    return {"X-Cairn-Actor": who}


def test_an_agent_is_listed_requested_approved_used_and_expires(stack):  # noqa: F811
    from cairn.agentkit.project import deploy, init
    from cairn.extensions.service import Manifest
    from cairn.governance.personas import grants_table
    from cairn.marketplace.service import access_requests

    s, url, tmp = stack
    bundle = s.pdp.bundle                                     # Cal may call the model, not the agent (yet)
    s.pdp.set_bundle(bundle.model_copy(update={"policies": [*bundle.policies, Policy(
        id="cal-models", effect="allow", actions=["USE_AI_GATEWAY"], target=Target(resource_urns=[f"urn:li:aiModel:{SONNET}"]),
        condition={"attr": "subject.urn", "op": "eq", "value": CAL})]}))
    app = deploy(str(init(str(tmp / "triage"), template="basic")), url, h(ANA))
    assert app["status"] == "running"

    def invoke(who):
        return httpx.post(f"{url}/v1/agent-apps/triage/invocations", json={"input": "Which tickets are open?",
                                                                            "session_id": "m1"}, headers=h(who), timeout=120)
    assert invoke(CAL).status_code == 404

    # Only the agent's owner lists it.
    listing = {"kind": "agent", "target": "triage", "title": "Ticket triage", "tags": ["support"],
               "description": "Sorts the support queue by urgency.", "max_duration_days": 30}
    assert httpx.post(f"{url}/marketplace/listings", json=listing, headers=h(BOB), timeout=30).status_code == 403
    assert httpx.post(f"{url}/marketplace/listings", json={**listing, "target": "nope"}, headers=h(ANA),
                      timeout=30).status_code == 404
    r = httpx.post(f"{url}/marketplace/listings", json=listing, headers=h(ANA), timeout=30)
    assert r.status_code == 201, r.text
    card = r.json()
    assert card["urn"] == f"urn:li:cairnListing:{card['id']}" and card["source"] == "agent_app"
    assert card["request_policy"]["grants"] == ["USE_AGENT on urn:li:cairnAgent:triage"]
    assert httpx.post(f"{url}/marketplace/listings", json=listing, headers=h(ANA), timeout=30).status_code == 409

    # Cal finds it beside data products, and on its own.
    found = httpx.get(f"{url}/marketplace/search", params={"q": "support queue"}, headers=h(CAL), timeout=30).json()
    assert [(x["kind"], x["name"]) for x in found["results"] if x["kind"] != "data_product"] == [("agent", "Ticket triage")]
    listed = httpx.get(f"{url}/marketplace/listings", params={"kind": "agent", "q": "triage"}, headers=h(CAL), timeout=30)
    assert [x["id"] for x in listed.json()["listings"]] == [card["id"]]
    assert listed.json()["listings"][0]["access"]["status"] == "NONE"
    assert httpx.get(f"{url}/marketplace/listings", params={"kind": "skill"}, headers=h(CAL)).json()["listings"] == []

    # Cal asks; the rules of the listing hold; Cal cannot approve his own request.
    ask = f"{url}/marketplace/listings/{card['id']}/access-requests"
    assert httpx.post(ask, json={"purpose": "Operations", "justification": "short"}, headers=h(CAL)).status_code == 422
    assert httpx.post(ask, json={"purpose": "Operations", "justification": "Triage the support queue each morning",
                                 "duration_days": 90}, headers=h(CAL)).status_code == 422
    r = httpx.post(ask, json={"purpose": "Operations", "justification": "Triage the support queue each morning",
                              "duration_days": 7}, headers=h(CAL), timeout=30)
    assert r.status_code == 201, r.text
    req = r.json()
    assert req["state"] == "PENDING" and req["approvers"] == [ANA] and req["target_urn"] == "urn:li:cairnAgent:triage"
    assert httpx.post(ask, json={"purpose": "Operations", "justification": "Triage the support queue again"},
                      headers=h(CAL)).status_code == 409
    assert httpx.post(f"{url}/marketplace/access-requests/{req['id']}/approve", headers=h(CAL)).status_code == 403
    assert invoke(CAL).status_code == 404

    # Ana sees it in her inbox and approves: Cal invokes the agent, and nothing else is granted.
    inbox = httpx.get(f"{url}/marketplace/access-requests", params={"scope": "inbox"}, headers=h(ANA)).json()
    assert req["id"] in [x["id"] for x in inbox]
    r = httpx.post(f"{url}/marketplace/access-requests/{req['id']}/approve", json={"comment": "ok"}, headers=h(ANA))
    assert r.status_code == 200 and r.json()["state"] == "APPROVED", r.text
    out = invoke(CAL)
    assert out.status_code == 200 and out.json()["output"] == "Hello from the model", out.text
    assert httpx.get(f"{url}/marketplace/listings/{card['id']}", headers=h(CAL)).json()["access"]["status"] == "GRANTED"
    from cairn.common.security import Principal
    assert not s.authorize(Principal(urn=CAL, via="delegated"), "USE_AGENT", "urn:li:cairnAgent:other", raise_on_deny=False)
    assert not s.authorize(Principal(urn=CAL, via="delegated"), "BUILD_AGENTS", None, raise_on_deny=False)

    # The access period ends: refused at once, then marked expired and the grant taken away by the sweep.
    grant_id = s.marketplace.get_request(req["id"])["grant_id"]
    import time
    past = int(time.time() * 1000) - 1000
    with s.engine.begin() as c:
        c.execute(update(grants_table).where(grants_table.c.id == grant_id).values(expires_at_ms=past))
        c.execute(update(access_requests).where(access_requests.c.id == req["id"]).values(expires_at_ms=past))
    s.personas.reload()
    s.personas._invalidate(CAL)
    assert invoke(CAL).status_code == 404
    assert s.marketplace.sweep()["expired"] == 1
    assert s.marketplace.get_request(req["id"])["state"] == "EXPIRED"
    with s.engine.connect() as c:
        assert c.execute(select(grants_table.c.revoked).where(grants_table.c.id == grant_id)).scalar() is True
    assert httpx.get(f"{url}/marketplace/listings/{card['id']}", headers=h(CAL)).json()["access"]["status"] == "EXPIRED"

    # A skill: an installed extension, listed by whoever installed it, granted as USE_SKILL on its URN only.
    s.extensions.install(Manifest(name="pii-scanner", version="1.0.0", publisher="Data team",
                                  description="Finds personal data in text columns"), ANA)
    r = httpx.post(f"{url}/marketplace/listings", json={"kind": "skill", "target": "pii-scanner", "title": "PII scanner",
                                                       "requires_justification": False}, headers=h(ANA), timeout=30)
    assert r.status_code == 201 and r.json()["source"] == "extension", r.text
    skill = r.json()
    skills = httpx.get(f"{url}/marketplace/search", params={"q": "personal", "kind": "skill"}, headers=h(CAL)).json()
    assert [x["id"] for x in skills["results"]] == []           # the description is the extension's, not the card's
    skills = httpx.get(f"{url}/marketplace/search", params={"q": "pii", "kind": "skill"}, headers=h(CAL)).json()
    assert [x["id"] for x in skills["results"]] == [skill["id"]]
    rid = httpx.post(f"{url}/marketplace/listings/{skill['id']}/access-requests", json={"purpose": "Analytics"},
                     headers=h(CAL)).json()["id"]
    assert not s.authorize(Principal(urn=CAL, via="delegated"), "USE_SKILL", "urn:li:cairnSkill:pii-scanner",
                           raise_on_deny=False)
    httpx.post(f"{url}/marketplace/access-requests/{rid}/approve", headers=h(ANA)).raise_for_status()
    assert s.authorize(Principal(urn=CAL, via="delegated"), "USE_SKILL", "urn:li:cairnSkill:pii-scanner",
                       raise_on_deny=False)
    assert not s.authorize(Principal(urn=CAL, via="delegated"), "USE_AGENT", "urn:li:cairnAgent:triage",
                           raise_on_deny=False)
    # Delisting ends the grants made through it.
    gone = httpx.delete(f"{url}/marketplace/listings/{skill['id']}", headers=h(ANA)).json()
    assert gone == {"id": skill["id"], "removed": True, "requests_ended": 1}
    assert not s.authorize(Principal(urn=CAL, via="delegated"), "USE_SKILL", "urn:li:cairnSkill:pii-scanner",
                           raise_on_deny=False)
    actions = {e["action"] for e in s.audit.entries(category="marketplace", limit=200)}
    assert {"marketplace.listing_published", "access.requested", "access.approved", "access.expired",
            "marketplace.listing_removed"} <= actions
