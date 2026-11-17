"""Agent credentials on the data apps and app chat routes, which resolve the caller themselves: they go through the
same narrowing as every other route (scope, intents, quota). Through the real services, decision point, database and
HTTP API, with a real upstream web app behind the proxy:
- an agent never creates, changes, deploys or deletes an app (an app is code Cairn runs), whatever its scopes;
- reading the app list needs 'read'; opening an app (HTTP and WebSocket) and asking app chat need 'query_data'
  (and app chat then refuses agents anyway);
- the agent's quota counts these requests; people keep working as before."""

import http.server
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.identity.agents import AgentIn, AgentPatch
from cairn.services import build_services

ADMIN = "urn:li:corpuser:admin"


class Upstream(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"hello {self.headers.get('X-Cairn-User')}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def stack(settings, graph, monkeypatch):
    monkeypatch.setenv("CAIRN_APPS_SECRET", "apps-secret-" + "a" * 24)
    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    c = TestClient(create_api_app(s))
    r = c.post("/v1/apps", json={"name": "orders", "url": f"http://127.0.0.1:{up.server_address[1]}"},
               headers={"X-Cairn-Actor": ADMIN})
    assert r.status_code == 201, r.text
    yield s, c
    up.shutdown()


def key(s, *scopes):
    return {"Authorization": "Bearer " + s.agents.create_key_agent(AgentIn(name="bot " + "-".join(scopes),
                                                                           scopes=list(scopes), owner=ADMIN), ADMIN)["key"]}


def test_agents_never_configure_apps(stack):
    s, c = stack
    for scopes in (["read"], ["read", "write", "query_data"]):
        h = key(s, *scopes)
        for method, path, body in (("POST", "/v1/apps", {"name": "vapp", "url": "http://vapp.invalid"}),
                                   ("POST", "/v1/apps", {"name": "vcmd", "command": ["/bin/true", "{port}"]}),
                                   ("PUT", "/v1/apps/orders", {"url": "http://vapp2.invalid"}),
                                   ("POST", "/v1/apps/orders/deploy", None),
                                   ("DELETE", "/v1/apps/orders", None)):
            r = c.request(method, path, json=body, headers=h)
            assert r.status_code == 403, (scopes, method, path, r.status_code, r.text)
            assert "this agent may not do this" in r.text
    assert [a["name"] for a in c.get("/v1/apps", params={"config": "true"},
                                     headers={"X-Cairn-Actor": ADMIN}).json()] == ["orders"]
    assert c.get("/v1/apps", params={"config": "true"}, headers={"X-Cairn-Actor": ADMIN}).json()[0]["url"].startswith(
        "http://127.0.0.1")
    # Reading the list is a read.
    assert [a["name"] for a in c.get("/v1/apps", headers=key(s, "read")).json()] == ["orders"]
    assert c.get("/v1/apps", headers=key(s, "propose")).status_code == 403
    # People keep configuring apps.
    assert c.put("/v1/apps/orders", json={"url": "http://127.0.0.1:1"}, headers={"X-Cairn-Actor": ADMIN}).status_code == 200


def test_opening_an_app_needs_query_data(stack):
    s, c = stack
    r = c.get("/apps/orders/", headers=key(s, "read"))
    assert r.status_code == 403 and "needs scope query_data" in r.text
    r = c.get("/apps/orders/", headers=key(s, "read", "query_data"))
    assert r.status_code == 200 and r.text == f"hello {ADMIN}"
    r = c.post("/apps/orders/x", headers=key(s, "query_data"))
    assert r.status_code == 403 and "needs scope write" in r.text
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect("/apps/orders/_stcore/stream", headers=key(s, "read")):
            pass
    assert e.value.code == 4403


def test_app_chat_needs_query_data(stack):
    s, c = stack
    r = c.post("/v1/apps/orders/chat", json={"question": "what is shown?"}, headers=key(s, "read"))
    assert r.status_code == 403 and "needs scope query_data" in r.text
    # Past the scope check, the chat itself is for people.
    r = c.post("/v1/apps/orders/chat", json={"question": "what is shown?"}, headers=key(s, "query_data"))
    assert r.status_code == 403 and "for the people using it" in r.text


def test_app_requests_count_against_the_agents_quota(stack):
    s, c = stack
    bot = s.agents.create_key_agent(AgentIn(name="quota bot", scopes=["read", "query_data"], owner=ADMIN), ADMIN)
    s.agents.update(bot["id"], AgentPatch(per_minute=2), ADMIN)
    h = {"Authorization": f"Bearer {bot['key']}"}
    assert [c.get("/apps/orders/", headers=h).status_code for _ in range(3)] == [200, 200, 429]
