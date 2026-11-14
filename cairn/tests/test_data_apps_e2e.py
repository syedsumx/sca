"""Data apps, end to end: a real Streamlit app, started by Cairn on the first visit, opened in a real browser
(Chromium) through Cairn. The viewer is signed in by Cairn and needs USE_APP; the app is told who is
viewing (over Streamlit's WebSocket too) and gets a Trino token for the viewer; headers a browser forges
never reach it. Needs Streamlit (CAIRN_STREAMLIT_BIN) and Playwright's Chromium."""

import os
import shutil
import socket
import threading
import time

import httpx
import pytest

STREAMLIT = os.environ.get("CAIRN_STREAMLIT_BIN") or shutil.which("streamlit") or ""


def _ready():
    if not STREAMLIT:
        return False
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


pytestmark = pytest.mark.skipif(not _ready(), reason="needs Streamlit (CAIRN_STREAMLIT_BIN) and Playwright")
ANA, BOB = "urn:li:corpuser:ana", "urn:li:corpuser:bob"
SESSIONS = {"ana-session": ANA, "bob-session": BOB, "admin-session": "urn:li:corpuser:admin"}
APP = '''
import os
import streamlit as st

h = st.context.headers
st.title("Orders")
st.write("viewer:", h.get("X-Cairn-User"))
st.write("app key:", "ok" if h.get("X-Cairn-App-Key") == os.environ.get("CAIRN_APP_KEY") else "wrong")
st.write("trino token:", "present" if h.get("X-Cairn-Trino-Token") else "absent")
st.write("forged header:", h.get("X-Cairn-Forged") or "none")
st.write("cairn secrets:", "leaked" if os.environ.get("CAIRN_APPS_SECRET") else "none")
'''


def _port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def cairn(settings, graph, tmp_path, monkeypatch):
    import uvicorn

    from cairn.app import create_api_app
    from cairn.governance.abac import Policy, PolicyBundle, Target
    from cairn.services import build_services

    monkeypatch.setenv("CAIRN_APPS_SECRET", "apps-secret-" + "a" * 24)
    monkeypatch.setenv("CAIRN_TRINO_ACCESS_KEY", "k" * 32)
    monkeypatch.setenv("CAIRN_TRINO_CATALOGS", '{"lakehouse": {"platform": "iceberg", "database": ""}}')
    settings.dev_trust_actor_header = False
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    # Browser sessions as Cairn Core's sign-in gives them (the session cookie names the person).
    s.identity.frontend_url = "http://core.invalid"
    s.identity._me_lookup = lambda url, headers, cookies: SESSIONS.get(cookies.get("PLAY_SESSION", ""))
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": "urn:li:corpuser:admin"}),
        Policy(id="ana-orders", effect="allow", actions=["USE_APP"], target=Target(resource_urns=["urn:li:dataApp:orders"]),
               condition={"attr": "subject.urn", "op": "eq", "value": ANA})]))
    (tmp_path / "app.py").write_text(APP)
    p = _port()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=p, log_level="warning", ws="websockets",
                                           timeout_keep_alive=75))
    threading.Thread(target=server.run, daemon=True).start()
    end = time.time() + 30
    while True:
        try:
            socket.create_connection(("127.0.0.1", p), 0.3).close()
            break
        except OSError:
            assert time.time() < end
            time.sleep(0.1)
    yield s, f"http://127.0.0.1:{p}", tmp_path
    for name in list(s.data_apps._running):
        s.data_apps.stop(name)
    server.should_exit = True


def test_a_streamlit_app_is_served_to_its_viewers_as_themselves(cairn):
    from playwright.sync_api import sync_playwright

    s, url, d = cairn
    admin = {"Cookie": "PLAY_SESSION=admin-session"}
    r = httpx.post(f"{url}/v1/apps", headers=admin, json={
        "name": "orders", "title": "Orders", "workdir": str(d),
        "command": [STREAMLIT, "run", "app.py", "--server.port", "{port}", "--server.baseUrlPath", "{base}",
                    "--server.headless", "true", "--browser.gatherUsageStats", "false"]})
    assert r.status_code == 201, r.text
    assert httpx.post(f"{url}/v1/apps", headers={"Cookie": "PLAY_SESSION=ana-session"},
                      json={"name": "mine", "url": "http://x.invalid"}).status_code == 403     # operators define apps
    # Strangers and people without USE_APP never reach the app (it is not even started for them).
    assert httpx.get(f"{url}/apps/orders/").status_code == 401
    assert httpx.get(f"{url}/apps/orders/", headers={"Cookie": "PLAY_SESSION=bob-session"}).status_code == 403
    assert not s.data_apps.get("orders")["running"]
    assert [a["name"] for a in httpx.get(f"{url}/v1/apps", headers={"Cookie": "PLAY_SESSION=bob-session"}).json()] == []
    assert [a["name"] for a in httpx.get(f"{url}/v1/apps", headers={"Cookie": "PLAY_SESSION=ana-session"}).json()] == ["orders"]
    # Ana opens it in a browser: started on her first visit, the page renders over the app's WebSocket.
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(extra_http_headers={"X-Cairn-Forged": "evil", "X-Cairn-User": "urn:li:corpuser:root"})
        ctx.add_cookies([{"name": "PLAY_SESSION", "value": "ana-session", "domain": "127.0.0.1", "path": "/"}])
        page = ctx.new_page()
        page.goto(f"{url}/apps/orders/", wait_until="domcontentloaded", timeout=90_000)
        page.get_by_text("viewer:").wait_for(timeout=90_000)
        text = page.inner_text("body")
        browser.close()
    print("\n--- the app as Ana sees it ---\n" + text)
    assert "Orders" in text and ANA in text and "urn:li:corpuser:root" not in text, text
    assert "app key: ok" in text.replace("\n", " ") or "ok" in text.split("app key:")[1][:20], text
    assert "present" in text.split("trino token:")[1][:20], text
    assert "none" in text.split("forged header:")[1][:20], text                 # what the browser forged is gone
    assert "none" in text.split("cairn secrets:")[1][:20], text
    assert s.data_apps.get("orders")["running"]
    # Its WebSocket is refused to someone without USE_APP, as its pages are.
    import websockets.sync.client as wsc
    from websockets.exceptions import InvalidStatus, WebSocketException
    with pytest.raises((InvalidStatus, WebSocketException, ConnectionError)):
        with wsc.connect(f"{url.replace('http', 'ws')}/apps/orders/_stcore/stream",
                         additional_headers={"Cookie": "PLAY_SESSION=bob-session"}) as w:
            w.recv(timeout=5)
    # Idle apps are stopped; the next visit starts them again.
    s.data_apps._running["orders"].last -= 31 * 60
    assert s.data_apps.stop_idle() == ["orders"] and not s.data_apps.get("orders")["running"]
    assert httpx.get(f"{url}/apps/orders/_stcore/health", headers={"Cookie": "PLAY_SESSION=ana-session"},
                     timeout=90).text == "ok"
    ev = [e["action"] for e in s.audit.entries("apps", limit=10)]
    assert "app.created" in ev


def test_app_definitions_are_checked(cairn):
    from cairn.dataaccess.dataapps import AppError

    s, _, _ = cairn
    for body, msg in [({"name": "Bad Name", "url": "http://x"}, "name"), ({"name": "a"}, "url .* or command"),
                      ({"name": "a", "url": "http://x", "command": ["y"]}, "not both"),
                      ({"name": "a", "url": "ftp://x"}, "http"), ({"name": "a", "command": "streamlit run"}, "list of strings"),
                      ({"name": "a", "url": "http://x", "idle_minutes": 0}, "idle_minutes")]:
        with pytest.raises(AppError, match=msg):
            s.data_apps.create("urn:li:corpuser:admin", body)
    # Each app has its own key, derived from the secret: nothing stored, the same on every replica.
    assert s.data_apps.key("a") != s.data_apps.key("b") and s.data_apps.key("a") == s.data_apps.key("a")
