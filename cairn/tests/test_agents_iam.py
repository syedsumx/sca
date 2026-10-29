"""AI agents: keys and OAuth 2.1 (PKCE) acting for a person within scopes, attributed and revocable."""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.common.audit import AuditLog
from cairn.governance.abac import Policy, PolicyBundle
from cairn.identity.agents import AgentError, AgentIn, AgentPatch, ClientRegistration, required_scope, valid_redirect
from cairn.services import build_services
from conftest import DS

ADMIN, ANA = "urn:li:corpuser:admin", "urn:li:corpuser:ana"
REDIRECT = "http://127.0.0.1:33418/callback"


@pytest.fixture
def stack(settings, graph):
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"],
               condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="read", effect="allow", actions=["VIEW_ENTITY_PAGE", "EDIT_ENTITY_DOCS"],
               condition={"attr": "subject.urn", "op": "exists"})]))
    graph.aspects[(DS, "datasetProperties")] = {"name": "orders"}
    return s, TestClient(create_api_app(s), follow_redirects=False)


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def sign_in(c, client_id, challenge, person=ANA, scope=None, allow=("read", "query_data", "propose")):
    """Walk the browser part: authorize page (as a signed-in person) → consent → redirect with a code."""
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": "xyz",
              "code_challenge": challenge, "code_challenge_method": "S256"}
    if scope:
        params["scope"] = scope
    page = c.get("/oauth/authorize", params=params, headers={"X-Cairn-Actor": person})
    assert page.status_code == 200, page.text
    assert page.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in page.headers["content-security-policy"]
    import re

    fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)">', page.text))
    form = {**fields, "decision": "allow", "scope": list(allow)}
    done = c.post("/oauth/authorize", data=form, headers={"X-Cairn-Actor": person})
    assert done.status_code == 302, done.text
    q = parse_qs(urlsplit(done.headers["location"]).query)
    assert q["state"] == ["xyz"] and q["iss"][0].endswith("/cairn/api/oauth")
    return q["code"][0]


def test_discovery_metadata(stack):
    _, c = stack
    pr = c.get("/.well-known/oauth-protected-resource/cairn/mcp/mcp").json()
    assert pr["resource"].endswith("/cairn/mcp/mcp") and pr["authorization_servers"][0].endswith("/cairn/api/oauth")
    md = c.get("/.well-known/oauth-authorization-server/cairn/api/oauth").json()
    assert md["code_challenge_methods_supported"] == ["S256"] and md["token_endpoint_auth_methods_supported"] == ["none"]
    assert md["registration_endpoint"].endswith("/oauth/register")
    assert c.get("/oauth/.well-known/oauth-authorization-server").json() == md


def test_oauth_flow_scopes_attribution_refresh_and_revocation(stack):
    s, c = stack
    reg = c.post("/oauth/register", json={"client_name": "Claude Desktop", "redirect_uris": [REDIRECT]})
    assert reg.status_code == 201
    client_id = reg.json()["client_id"]
    verifier, challenge = pkce()
    code = sign_in(c, client_id, challenge, allow=("read", "propose"))     # the person unticks query_data
    bad = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                       "redirect_uri": REDIRECT, "code_verifier": "wrong" * 10})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
    tok = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                       "redirect_uri": REDIRECT, "code_verifier": verifier})
    assert tok.status_code == 200 and tok.headers["cache-control"] == "no-store", tok.text
    t = tok.json()
    assert t["access_token"].startswith("cat_") and t["scope"] == "read propose" and t["expires_in"] == 3600
    h = {"Authorization": f"Bearer {t['access_token']}"}

    me = c.get("/me", headers=h).json()
    assert me["urn"] == ANA                                        # acts as the person
    assert c.post("/data/query", json={"sql": "select 1", "urn": DS}, headers=h).status_code == 403   # no query_data
    assert "needs scope query_data" in c.post("/data/query", json={"sql": "select 1", "urn": DS}, headers=h).text
    assert c.get("/admin/agents", headers=h).status_code == 403    # never administration
    assert c.put(f"/collab/bookmarks/{DS}", headers=h).status_code == 403          # a write
    wf = c.post("/governance/workflows", json={"kind": "DOCUMENTATION", "entity_urn": DS,
                                               "payload": {"draft": {"description": "x"}}}, headers=h)
    assert wf.status_code == 201, wf.text                          # propose is allowed
    entries = list(s.audit.export(category="governance"))
    assert entries[-1]["actor"] == ANA and entries[-1]["details"]["agent"] == client_id

    # A replayed code fails and revokes what it produced.
    assert c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                        "redirect_uri": REDIRECT, "code_verifier": verifier}).status_code == 400
    assert c.get("/me", headers=h).status_code == 401

    # A fresh sign-in, then a rotating refresh.
    verifier, challenge = pkce()
    code = sign_in(c, client_id, challenge)
    t = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                     "redirect_uri": REDIRECT, "code_verifier": verifier}).json()
    r1 = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                                      "client_id": client_id}).json()
    assert r1["access_token"] != t["access_token"]
    reuse = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"],
                                         "client_id": client_id})
    assert reuse.status_code == 400                                 # a spent refresh token revokes the grant
    assert c.get("/me", headers={"Authorization": f"Bearer {r1['access_token']}"}).status_code == 401

    # The person sees and disconnects their agents.
    verifier, challenge = pkce()
    code = sign_in(c, client_id, challenge)
    t = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                     "redirect_uri": REDIRECT, "code_verifier": verifier}).json()
    mine = c.get("/me/agents", headers={"X-Cairn-Actor": ANA}).json()
    assert [a["name"] for a in mine] == ["Claude Desktop"]
    assert c.delete(f"/me/agents/{client_id}", headers={"X-Cairn-Actor": ANA}).json()["revoked"] >= 1
    assert c.get("/me", headers={"Authorization": f"Bearer {t['access_token']}"}).status_code == 401
    assert c.get("/me/agents", headers={"X-Cairn-Actor": ANA}).json() == []


def test_consent_requires_a_session_and_a_valid_form(stack):
    _, c = stack
    client_id = c.post("/oauth/register", json={"client_name": "IDE", "redirect_uris": [REDIRECT]}).json()["client_id"]
    _, challenge = pkce()
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    login = c.get("/oauth/authorize", params=params)
    assert login.status_code == 302 and login.headers["location"].startswith("/login?redirect_uri=%2Fcairn%2Fapi%2Foauth")
    assert c.get("/oauth/authorize", params={**params, "redirect_uri": "https://evil.example/cb"},
                 headers={"X-Cairn-Actor": ANA}).status_code == 400       # never redirected to an unknown address
    plain = c.get("/oauth/authorize", params={**params, "code_challenge_method": "plain"}, headers={"X-Cairn-Actor": ANA})
    assert plain.status_code == 302 and "error=invalid_request" in plain.headers["location"]
    forged = c.post("/oauth/authorize", data={"client_id": client_id, "redirect_uri": REDIRECT, "code_challenge": challenge,
                                              "state": "", "resource": "", "ts": "9999999999", "csrf": "0" * 64,
                                              "decision": "allow", "scope": ["read"]}, headers={"X-Cairn-Actor": ANA})
    assert forged.status_code == 400
    # A token is not a session: an agent cannot approve itself.
    deny = c.get("/oauth/authorize", params=params, headers={"Authorization": "Bearer cat_x"})
    assert deny.status_code == 302 and deny.headers["location"].startswith("/login")


def test_key_agents_scopes_ai_system_link_and_admin(stack):
    s, c = stack
    admin = {"X-Cairn-Actor": ADMIN}
    assert c.post("/admin/agents", json={"name": "Nightly doc bot"}, headers={"X-Cairn-Actor": ANA}).status_code == 403
    made = c.post("/admin/agents", json={"name": "Nightly doc bot", "scopes": ["read", "write"], "owner": ANA},
                  headers=admin).json()
    key, aid = made["key"], made["id"]
    assert key.startswith(f"ca_{aid}.") and "key" not in c.get("/admin/agents", headers=admin).json()[0]
    h = {"Authorization": f"Bearer {key}"}
    assert c.get("/me", headers=h).json()["urn"] == ANA
    assert c.put(f"/collab/bookmarks/{DS}", headers=h).status_code == 200      # write scope
    assert c.post("/data/query", json={"sql": "select 1", "urn": DS}, headers=h).status_code == 403
    assert c.patch(f"/admin/agents/{aid}", json={"scopes": ["read"]}, headers=admin).json()["scopes"] == ["read"]
    assert c.put(f"/collab/bookmarks/{DS}", headers=h).status_code == 403
    rotated = c.post(f"/admin/agents/{aid}/rotate", headers=admin).json()["key"]
    assert c.get("/me", headers=h).status_code == 401 and c.get("/me", headers={"Authorization": f"Bearer {rotated}"}).status_code == 200
    # Linked to an AI system that is not approved: the agent stops working.
    states = {"ai_1": "IN_REVIEW"}
    s.agents.ai_system_state = states.get
    c.patch(f"/admin/agents/{aid}", json={"ai_system": "ai_1"}, headers=admin)
    assert c.get("/me", headers={"Authorization": f"Bearer {rotated}"}).status_code == 401
    states["ai_1"] = "APPROVED"
    assert c.get("/me", headers={"Authorization": f"Bearer {rotated}"}).status_code == 200
    c.patch(f"/admin/agents/{aid}", json={"enabled": False}, headers=admin)
    assert c.get("/me", headers={"Authorization": f"Bearer {rotated}"}).status_code == 401
    assert c.delete(f"/admin/agents/{aid}", headers=admin).status_code == 204
    actions = [e["action"] for e in s.audit.export(category="agents")]
    assert actions[:3] == ["agent.created", "agent.updated", "agent.rotated"]


def test_registration_rules(stack, settings, tmp_path):
    s, _ = stack
    assert valid_redirect("https://claude.ai/api/mcp/auth_callback") and valid_redirect("http://localhost:5000/cb")
    assert valid_redirect("com.example.app:/oauth") and not valid_redirect("http://evil.example/cb")
    assert not valid_redirect("javascript:alert(1)") and not valid_redirect("https://a.example/cb#frag")
    with pytest.raises(AgentError, match="redirect_uris"):
        s.agents.register_client(ClientRegistration(redirect_uris=["http://evil.example/cb"]))
    with pytest.raises(AgentError, match="unknown scopes"):
        s.agents.register_client(ClientRegistration(redirect_uris=[REDIRECT], scope="read admin"))
    s.agents.registration = "admin"
    pending = s.agents.register_client(ClientRegistration(client_name="New tool", redirect_uris=[REDIRECT]))
    with pytest.raises(AgentError, match="not enabled"):
        s.agents.check_authorization(pending["client_id"], REDIRECT, "a" * 43, "S256", None)
    s.agents.update(pending["client_id"], AgentPatch(enabled=True), ADMIN)
    assert s.agents.check_authorization(pending["client_id"], REDIRECT, "a" * 43, "S256", "read")[1] == ["read"]
    s.agents.registration = "off"
    with pytest.raises(AgentError, match="turned off"):
        s.agents.register_client(ClientRegistration(redirect_uris=[REDIRECT]))
    with pytest.raises(ValueError):
        AgentIn(name="x" * 3, scopes=["root"])
    assert isinstance(s.audit, AuditLog)


def test_scope_rules():
    reads = {"/collab/enrich", "/data/ask"}
    assert required_scope("GET", "/quality/monitors", reads) == "read"
    assert required_scope("POST", "/collab/enrich", reads) == "read"
    assert required_scope("POST", "/data/ask", reads) == "query_data"
    assert required_scope("POST", "/governance/workflows", reads) == "propose"
    assert required_scope("POST", "/governance/workflows/{wid}/approve", reads) == "write"   # deciding is not proposing
    assert required_scope("GET", "/admin/agents", reads) is None
    assert required_scope("POST", "/governance/policies", reads) is None
    assert required_scope("GET", "/governance/policies", reads) == "read"


def test_mcp_tools_respect_agent_scopes(stack):
    import asyncio

    from cairn.ai.mcp_server import _principal, build_mcp_server
    from cairn.common.security import Principal

    s, _ = stack
    server, _ = build_mcp_server(s)
    token = _principal.set(Principal(ANA, "agent", agent="agt_x", scopes=("read",)))
    try:
        out = asyncio.run(server.call_tool("generate_documentation", {"dataset_urn": DS})).structured_content
        assert out["error"] == "forbidden" and "propose" in out["message"]
        assert asyncio.run(server.call_tool("get_quality_status", {"dataset_urn": DS})).structured_content["dataset_urn"] == DS
    finally:
        _principal.reset(token)


def test_hub_pause_quotas_and_autonomy(stack):
    from cairn.ai.mcp_server import CairnTools, _principal
    from cairn.common.security import Principal

    s, c = stack
    admin = {"X-Cairn-Actor": ADMIN}
    made = c.post("/admin/agents", json={"name": "Tagger", "scopes": ["read", "propose"], "owner": ADMIN},
                  headers=admin).json()
    h = {"Authorization": f"Bearer {made['key']}"}
    assert made["autonomy"] == "suggest" and made["per_minute"] == 120

    # Kill switch: every agent credential stops, with a clear reason; people are unaffected.
    assert c.post("/admin/agents/pause", json={"paused": True}, headers=admin).json()["paused"] is True
    stopped = c.get("/me", headers=h)
    assert stopped.status_code == 401 and "paused" in stopped.text
    assert c.get("/me", headers=admin).status_code == 200
    c.post("/admin/agents/pause", json={"paused": False}, headers=admin)
    assert c.get("/me", headers=h).status_code == 200

    # Quotas: per minute, then counted per day.
    c.patch(f"/admin/agents/{made['id']}", json={"per_minute": 3}, headers=admin)
    codes = [c.get("/me", headers=h).status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and codes[-1] == 429
    today = next(a for a in c.get("/admin/agents", headers=admin).json() if a["id"] == made["id"])["requests_today"]
    assert today == 3                         # admitted requests only: refused ones do not use the daily quota

    # Autonomy: confident suggestions apply at once, as the person (who may make the change).
    c.patch(f"/admin/agents/{made['id']}", json={"autonomy": "auto", "auto_accept_at": 0.8, "per_minute": 100},
            headers=admin)
    s.graph.aspects[(DS, "schemaMetadata")] = {"fields": [{"fieldPath": "id"}]}
    token = _principal.set(Principal(ADMIN, "agent", agent=made["id"], scopes=("read", "propose")))
    try:
        tools = CairnTools(s)
        sure = tools.suggest_tags(DS, ["Gold"], confidence=0.95)
        unsure = tools.suggest_tags(DS, ["Silver"], confidence=0.6)
    finally:
        _principal.reset(token)
    assert sure["applied"] and sure["applied"][0]["state"] == "AUTO_ACCEPTED" and "Applied at once" in sure["note"]
    assert unsure["queued"] and not unsure["applied"]
    assert {t["tag"] for t in s.graph.get_aspect(DS, "globalTags")["tags"]} == {"urn:li:tag:Gold"}
    # A person without the privilege: the agent's confident suggestion still waits for review.
    token = _principal.set(Principal(ANA, "agent", agent=made["id"], scopes=("read", "propose")))
    try:
        s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="read", effect="allow", actions=["VIEW_ENTITY_PAGE"],
                                                       condition={"attr": "subject.urn", "op": "exists"})]))
        held = CairnTools(s).suggest_tags(DS, ["Bronze"], confidence=0.99)
    finally:
        _principal.reset(token)
    assert held["queued"] and not held["applied"]
    actions = [e["action"] for e in s.audit.export(category="agents")]
    assert "agents.paused" in actions and "agents.resumed" in actions
