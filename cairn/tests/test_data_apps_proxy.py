"""The data app proxy (/apps/<name>/): what passes between the viewer's browser and the app, both ways. A real
app process started by Cairn and reached through it; the app echoes what it was sent."""

import sys

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services

ADMIN, ANA = "urn:li:corpuser:admin", "urn:li:corpuser:ana"
ECHO = '''
import http.server, json, sys

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        if self.path.endswith("/set-cookies"):
            self.send_header("Set-Cookie", "app_a=1; Path=/apps/echo/")
            self.send_header("Set-Cookie", "app_b=2; Path=/apps/echo/")
            self.send_header("Set-Cookie", "PLAY_SESSION=planted; Path=/")
            self.send_header("Set-Cookie", "actor=urn:li:corpuser:mallory; Path=/")
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"path": self.path, "headers": dict(self.headers.items())}).encode())

    def log_message(self, *a):
        pass

http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''


@pytest.fixture
def world(settings, graph, tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "echo.py").write_text(ECHO)
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="ana", effect="allow", actions=["USE_APP"], condition={"attr": "subject.urn", "op": "eq", "value": ANA})]))
    app = create_api_app(s)
    admin = TestClient(app, headers={"X-Cairn-Actor": ADMIN})
    r = admin.post("/v1/apps", json={"name": "echo", "title": "Echo", "workdir": str(tmp_path),
                                     "command": [sys.executable, "echo.py", "{port}"]})
    assert r.status_code == 201, r.text
    yield {"s": s, "app": app, "ana": TestClient(app, headers={"X-Cairn-Actor": ANA})}
    for name in list(s.data_apps._running):
        s.data_apps.stop(name)


def seen(r) -> dict:
    assert r.status_code == 200, r.text
    return {k.lower(): v for k, v in r.json()["headers"].items()}


def test_cairn_session_cookies_never_reach_the_app(world):
    """App code is not Cairn: given the viewer's Cairn session it could act in Cairn as them, beyond the app."""
    ana = world["ana"]
    ana.cookies.set("PLAY_SESSION", "the-viewers-session")
    ana.cookies.set("actor", ANA)
    ana.cookies.set("app_pref", "dark")
    h = seen(ana.get("/apps/echo/"))
    assert h["x-cairn-user"] == ANA
    cookie = h.get("cookie", "")
    assert "the-viewers-session" not in cookie and "PLAY_SESSION" not in cookie and "actor=" not in cookie, cookie
    assert "app_pref=dark" in cookie          # the app's own cookies still reach it


def test_an_app_keeps_its_own_cookies_and_cannot_set_cairns(world):
    r = world["ana"].get("/apps/echo/set-cookies")
    assert r.status_code == 200
    cookies = r.headers.get_list("set-cookie")
    # Each of the app's cookies is its own header (joined into one, the browser keeps only the first) ...
    assert any(c.startswith("app_a=1") for c in cookies) and any(c.startswith("app_b=2") for c in cookies), cookies
    # ... and none of them replaces the viewer's Cairn sign-in on Cairn's origin.
    assert not any(c.lower().startswith(("play_session=", "actor=")) for c in cookies), cookies


@pytest.mark.parametrize("path", ["/apps/echo/%2e%2e/%2e%2e/secret", "/apps/echo/a/%2E%2E/%2e%2e/%2e%2e/x",
                                  "/apps/echo/..%2f..%2fsecret", "/apps/echo/.%2f..%2f..%2fsecret",
                                  "/apps/echo/%252e%252e/%252e%252e/secret", "/apps/echo/..%5c..%5csecret"])
def test_a_proxied_path_never_leaves_the_app(world, path):
    """The viewer may use this app, not whatever else answers at the app's address."""
    r = world["ana"].get(path)
    assert r.status_code == 400, (r.status_code, r.text[:300])


def test_ordinary_paths_still_reach_the_app(world):
    for path in ("/apps/echo/static/app..v2.js", "/apps/echo/files/a%20b.csv?x=..", "/apps/echo/"):
        r = world["ana"].get(path)
        assert r.status_code == 200 and r.json()["path"] == path, r.text


def test_identity_headers_spelled_with_underscores_are_removed(world):
    """Many servers read X_Cairn_User as X-Cairn-User (CGI-style names): a viewer must not choose who they are."""
    h = world["s"].data_apps.headers_for("echo", ANA, {"X_Cairn_User": ADMIN, "x_cairn-app-key": "forged",
                                                     "X-Cairn_Trino_Token": "forged", "accept": "*/*"})
    assert h["X-Cairn-User"] == ANA and h["accept"] == "*/*"
    assert not [k for k in h if k.lower().replace("_", "-").startswith("x-cairn-") and "_" in k], h


def test_a_websocket_from_another_site_is_refused(world):
    """A page on another site can open a WebSocket to Cairn with the viewer's cookies: only Cairn's own pages may."""
    pytest.importorskip("websockets")
    from starlette.websockets import WebSocketDisconnect

    ana = world["ana"]
    with pytest.raises(WebSocketDisconnect) as e:
        with ana.websocket_connect("/apps/echo/_stcore/stream", headers={"Origin": "https://evil.example.com"}) as ws:
            ws.receive_text()
    assert e.value.code == 4403


@pytest.mark.parametrize("headers", [{"Origin": "http://testserver"}, {}, {"Origin": "https://cairn.example.com",
                                                                          "X-Forwarded-Host": "cairn.example.com"}])
def test_a_websocket_from_cairns_own_pages_goes_on_to_the_app(world, headers):
    """Cairn's own origin (or a client that is not a browser) is let through to the app, which here has no
    WebSocket: whatever happens next, it is not Cairn refusing the origin."""
    pytest.importorskip("websockets")
    code = None
    try:
        with world["ana"].websocket_connect("/apps/echo/_stcore/stream", headers=headers) as ws:
            ws.receive_text()
    except Exception as e:  # noqa: BLE001 - the echo app does not speak WebSocket
        code = getattr(e, "code", None)
    assert code != 4403


def test_auth_proxy_identity_headers_are_removed(world):
    """An app behind an auth proxy (Grafana auth.proxy, Superset REMOTE_USER) takes these as the signed-in person;
    they were forwarded as the viewer sent them, so a viewer could be anyone to such an app."""
    sent = {"X-Forwarded-User": ADMIN, "Remote-User": ADMIN, "X_Forwarded_Email": "boss@x", "X-Auth-Request-User": ADMIN,
            "X-WEBAUTH-USER": ADMIN, "accept": "*/*"}
    h = world["s"].data_apps.headers_for("echo", ANA, sent)
    assert h["X-Cairn-User"] == ANA and h["accept"] == "*/*"
    assert not [k for k in h if k in sent and k != "accept"], h
