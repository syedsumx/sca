"""The status page against real components: Cairn Store (the real binary) is probed while it runs, while it is
stopped and once it is back; a slow component reads as degraded; operators' incidents and maintenance show on the
public page, in its JSON and in its Atom feed. Needs CAIRN_STORE_BIN."""

import os
import socket
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

STORE = os.environ.get("CAIRN_STORE_BIN", "")
pytestmark = pytest.mark.skipif(not (STORE and os.path.exists(STORE)), reason="needs CAIRN_STORE_BIN")


def port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Store:
    def __init__(self):
        self.dir, self.port, self.proc = tempfile.mkdtemp(), port(), None

    def start(self):
        self.proc = subprocess.Popen([STORE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
            **os.environ, "CAIRN_STORE_ACCESS_KEY": "status-key", "CAIRN_STORE_SECRET_KEY": "status-secret-0123456789",
            "CAIRN_STORE_DATA": self.dir, "CAIRN_STORE_ADDR": f"127.0.0.1:{self.port}", "CAIRN_STORE_META": ""})
        end = time.time() + 30
        while time.time() < end:
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.5).close()
                return
            except OSError:
                time.sleep(0.2)
        raise RuntimeError("the store did not start")

    def stop(self):
        self.proc.terminate()
        self.proc.wait(15)


class Slow(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        time.sleep(0.4)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


@pytest.fixture
def status(settings, graph, monkeypatch):
    import json

    from cairn.app import create_api_app
    from cairn.governance.abac import Policy, PolicyBundle, Target
    from cairn.services import build_services

    store = Store()
    store.start()
    slow = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=slow.serve_forever, daemon=True).start()
    monkeypatch.setenv("CAIRN_STATUS", json.dumps({
        "title": "Cairn status", "public_url": "https://status.cairn.test", "timeout_ms": 2000,
        "components": [{"name": "Lakehouse storage", "url": f"http://127.0.0.1:{store.port}/-/healthy"},
                       {"name": "Search", "url": f"http://127.0.0.1:{slow.server_port}/", "slow_ms": 200}]}))
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="ops", effect="allow", actions=["MANAGE_SYSTEM_OPERATIONS"],
                                                   target=Target(), condition={"attr": "subject.urn", "op": "eq",
                                                                               "value": "urn:li:corpuser:ops"})]))
    app = create_api_app(s)
    yield s, TestClient(app), TestClient(app, headers={"X-Cairn-Actor": "urn:li:corpuser:ops"}), store
    try:
        store.stop()
    except Exception:  # noqa: BLE001
        pass
    slow.shutdown()


def states(c) -> dict:
    return {x["name"]: x["state"] for x in c.get("/status.json").json()["components"]}


def test_components_are_probed_and_incidents_announced(status):
    s, public, ops, store = status
    page = s.status_page
    page.probe(force=True)
    assert states(public) == {"Lakehouse storage": "operational", "Search": "degraded"}   # answers, too slowly
    # The store stops: one failed check is degraded, two are an outage; back, it recovers over three checks.
    store.stop()
    page.probe(force=True)
    assert states(public)["Lakehouse storage"] == "degraded"
    page.probe(force=True)
    summary = public.get("/status.json").json()
    assert summary["state"] == "outage" and states(public)["Lakehouse storage"] == "outage"
    store.start()
    page.probe(force=True)
    assert states(public)["Lakehouse storage"] == "degraded"
    page.probe(force=True)
    page.probe(force=True)
    assert states(public)["Lakehouse storage"] == "operational"
    up = next(x for x in public.get("/status.json").json()["components"] if x["name"] == "Lakehouse storage")["uptime_90d"]
    assert up == round(100 * 4 / 6, 3)                                             # 4 of 6 checks answered
    assert page.probe() == {}                                                      # not due again for a minute

    # Operators announce an incident; the public cannot.
    body = {"title": "Slow searches", "impact": "major", "components": ["Search"], "message": "Searches take seconds."}
    assert public.post("/v1/status/incidents", json=body).status_code in (401, 403)
    for bad, msg in [({**body, "components": ["Nope"]}, "components"), ({**body, "impact": "huge"}, "impact"),
                     ({**body, "message": ""}, "message")]:
        r = ops.post("/v1/status/incidents", json=bad)
        assert r.status_code == 400 and msg in r.json()["detail"], r.text
    inc = ops.post("/v1/status/incidents", json=body).json()
    assert states(public)["Search"] == "partial_outage"
    ops.post(f"/v1/status/incidents/{inc['id']}/updates", json={"phase": "identified", "message": "An index is rebuilding."})
    html = public.get("/status").text
    assert "Slow searches" in html and "An index is rebuilding." in html and "Partial outage" in html
    assert "<script" not in html.lower()
    ops.post(f"/v1/status/incidents/{inc['id']}/updates", json={"phase": "resolved", "message": "Rebuilt."})
    assert ops.post(f"/v1/status/incidents/{inc['id']}/updates",
                    json={"phase": "monitoring", "message": "x"}).status_code == 409
    j = public.get("/status.json").json()
    assert j["incidents"] == [] and j["history"][0]["title"] == "Slow searches"
    assert states(public)["Search"] == "degraded"                                  # back to what is measured

    # Maintenance: shown ahead, sets the state while it runs.
    now = int(time.time() * 1000)
    m = ops.post("/v1/status/incidents", json={"title": "Storage upgrade", "impact": "maintenance",
                                                "components": ["Lakehouse storage"], "message": "Rolling restart.",
                                                "starts_ms": now + 3_600_000, "ends_ms": now + 7_200_000}).json()
    j = public.get("/status.json").json()
    assert [x["title"] for x in j["maintenance"]] == ["Storage upgrade"] and states(public)["Lakehouse storage"] == "operational"
    assert page.summary(now + 4_000_000)["components"][0]["state"] == "maintenance"
    assert ops.post("/v1/status/incidents", json={**body, "impact": "maintenance"}).status_code == 400   # needs its window

    # The feed carries every incident and its updates; the page escapes what operators write.
    feed = ET.fromstring(public.get("/status/feed.atom").text)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    assert {e.find("a:title", ns).text for e in feed.findall("a:entry", ns)} == {"Slow searches", "Storage upgrade"}
    ops.post(f"/v1/status/incidents/{m['id']}/updates", json={"phase": "in_progress", "message": "<b>restarting</b>"})
    assert "&lt;b&gt;restarting&lt;/b&gt;" in page.html(now + 4_000_000)
    assert {e["action"] for e in s.audit.entries("status", limit=10)} >= {"status.incident_opened", "status.incident_updated"}
