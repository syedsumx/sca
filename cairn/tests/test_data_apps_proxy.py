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
