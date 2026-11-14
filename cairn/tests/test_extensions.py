"""Extensions: manifests, the scoped token, signed events and workspace sections."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.extensions.api import parse_manifest
from cairn.extensions.service import ExtensionError, sign, verify_signature
from cairn.governance.abac import Policy, PolicyBundle
from cairn.notify.channels import WebhookSender
from cairn.notify.hub import Event
from cairn.services import build_services

ADMIN = "urn:li:corpuser:admin"
FIN = "urn:li:domain:finance"
LEDGER = "urn:li:dataset:(urn:li:dataPlatform:snowflake,fin.public.ledger,PROD)"
WEB = "urn:li:dataset:(urn:li:dataPlatform:snowflake,web.public.visits,PROD)"
MANIFEST = f"""
name: owner-coverage
version: 1.0.0
publisher: Acme Data Platform
description: Nags owners of uncertified tables.
permissions: [VIEW_ENTITY_PAGE, EDIT_ENTITY_DOCS]
scope: {{attr: resource.domains, op: contains, value: "{FIN}"}}
events:
  - url: https://hooks.acme.example/events
    types: [quality.incident.raised]
ui:
  - label: Owner coverage
    url: https://owner-coverage.acme.example/ui
"""


def test_manifest_rules():
    m = parse_manifest(MANIFEST)
    assert m.permissions == ["EDIT_ENTITY_DOCS", "VIEW_ENTITY_PAGE"] and m.ui[0].placement == "workspace"
    for bad, why in (({"permissions": ["MANAGE_POLICIES"]}, "cannot be granted MANAGE_POLICIES"),
                     ({"name": "Bad Name"}, "name"), ({"version": "1.0"}, "version"),
                     ({"scope": {"attr": "resource.x", "op": "teleport", "value": 1}}, "scope"),
                     ({"ui": [{"label": "x", "url": "javascript:alert(1)"}]}, "url"),
                     ({"events": [{"url": "https://x.example", "types": ["teleport"]}]}, "unknown event types")):
        doc = {**parse_manifest(MANIFEST).model_dump(), **bad}
        with pytest.raises(ExtensionError, match=why):
            parse_manifest(doc)


def test_signatures_verify_and_expire():
    body = b'{"type":"quality.incident"}'
    header = sign("s3cret", body)
    assert verify_signature("s3cret", body, header)
    assert not verify_signature("other", body, header)
    assert not verify_signature("s3cret", body + b" ", header)
    assert not verify_signature("s3cret", body, sign("s3cret", body, ts=1_000_000_000))      # too old
    assert not verify_signature("s3cret", body, "garbage")


@pytest.fixture
def stack(settings, graph):
    graph.aspects[(LEDGER, "domains")] = {"domains": [FIN]}
    graph.aspects[(WEB, "domains")] = {"domains": ["urn:li:domain:web"]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="admin", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    return s, TestClient(create_api_app(s))


def test_install_gives_a_token_limited_to_the_manifest(stack):
    s, c = stack
    admin = {"X-Cairn-Actor": ADMIN}
    assert c.put("/admin/extensions", content=MANIFEST, headers={"X-Cairn-Actor": "urn:li:corpuser:ana"}).status_code == 403
    assert c.put("/admin/extensions", content="permissions: [MANAGE_POLICIES]", headers=admin).status_code == 422
    out = c.put("/admin/extensions", content=MANIFEST, headers=admin).json()
    assert out["actor"] == "urn:li:corpuser:cairn-ext-owner-coverage" and out["token"].startswith("cx_owner-coverage.")
    ext = {"Authorization": f"Bearer {out['token']}"}

    assert c.get("/me", headers=ext).json() == {"urn": out["actor"], "via": "extension"}
    assert s.authorize(s.identity.resolve(ext, {}), "VIEW_ENTITY_PAGE", LEDGER, raise_on_deny=False)
    assert not s.authorize(s.identity.resolve(ext, {}), "VIEW_ENTITY_PAGE", WEB, raise_on_deny=False)   # scope
    assert not s.authorize(s.identity.resolve(ext, {}), "EDIT_ENTITY_OWNERS", LEDGER, raise_on_deny=False)
    assert c.get("/admin/extensions", headers=ext).status_code == 403          # never an admin
    assert c.get("/admin/licence", headers=ext).json()["status"] == "evaluation"
    assert c.get("/me", headers={"Authorization": "Bearer cx_owner-coverage.wrong"}).status_code == 401

    again = c.put("/admin/extensions", content=MANIFEST.replace("1.0.0", "1.1.0"), headers=admin).json()
    assert again["upgraded_from"] == "1.0.0" and "token" not in again          # an upgrade keeps the token
    assert c.get("/me", headers=ext).status_code == 200
    assert c.post("/admin/extensions/owner-coverage/disable", headers=admin).json()["enabled"] is False
    assert c.get("/me", headers=ext).status_code == 401
    c.post("/admin/extensions/owner-coverage/enable", headers=admin)
    new = c.post("/admin/extensions/owner-coverage/rotate", headers=admin).json()
    assert c.get("/me", headers=ext).status_code == 401
    assert c.get("/me", headers={"Authorization": f"Bearer {new['token']}"}).status_code == 200
    assert [e["name"] for e in c.get("/admin/extensions", headers=admin).json()] == ["owner-coverage"]
    assert c.delete("/admin/extensions/owner-coverage", headers=admin).status_code == 204
    assert c.get("/me", headers={"Authorization": f"Bearer {new['token']}"}).status_code == 401
    audit = [e["action"] for e in s.audit.export(category="extension")]
    assert audit[:2] == ["extension.installed", "extension.upgraded"] and audit[-1] == "extension.removed"


def test_events_reach_the_extension_signed(stack):
    s, c = stack
    secret = c.put("/admin/extensions", content=MANIFEST, headers={"X-Cairn-Actor": ADMIN}).json()["signing_secret"]
    hub = s.notify
    hub.publish(Event(type="quality.incident.raised", title="Ledger is late", body="…", entity_urn=LEDGER))
    hub.publish(Event(type="access.requested", title="not subscribed", body="…", entity_urn=LEDGER))
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200)

    hub.channels["webhook"] = WebhookSender(httpx.Client(transport=httpx.MockTransport(handler)),
                                            resolve=lambda h: ["203.0.113.7"],
                                            signing_secret=s.extensions.signing_secret_for_route)
    hub.dispatch_pending()
    assert len(seen) == 1 and str(seen[0].url) == "https://hooks.acme.example/events"
    assert json.loads(seen[0].content)["type"] == "quality.incident.raised"
    assert verify_signature(secret, seen[0].content, seen[0].headers["Cairn-Signature"])


def test_workspace_sections(stack):
    s, c = stack
    c.put("/admin/extensions", content=MANIFEST, headers={"X-Cairn-Actor": ADMIN})
    sections = c.get("/extensions/ui", headers={"X-Cairn-Actor": "urn:li:corpuser:ana"}).json()
    assert sections == [{"extension": "owner-coverage", "publisher": "Acme Data Platform", "label": "Owner coverage",
                         "url": "https://owner-coverage.acme.example/ui", "placement": "workspace"}]
    c.post("/admin/extensions/owner-coverage/disable", headers={"X-Cairn-Actor": ADMIN})
    assert c.get("/extensions/ui", headers={"X-Cairn-Actor": "urn:li:corpuser:ana"}).json() == []


def test_example_extension_accepts_only_signed_events(tmp_path):
    import importlib.util
    import threading
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

    folder = Path(__file__).resolve().parent.parent / "examples/extensions/incident-log"
    m = parse_manifest((folder / "cairn-extension.yaml").read_text())
    assert m.permissions == ["VIEW_ENTITY_PAGE"] and {u.placement for u in m.ui} == {"workspace", "asset"}

    seen = []

    class FakeCairn(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers["Authorization"]))
            body = json.dumps({LEDGER: {"quality_status": "FAILING"}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    cairn = ThreadingHTTPServer(("127.0.0.1", 0), FakeCairn)
    threading.Thread(target=cairn.serve_forever, daemon=True).start()
    spec = importlib.util.spec_from_file_location("incident_log", folder / "app.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    app.API, app.TOKEN, app.SECRET = f"http://127.0.0.1:{cairn.server_port}", "cx_incident-log.t", "s3cret"
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def post(body: bytes, headers: dict) -> int:
        req = urllib.request.Request(f"{base}/events", data=body, method="POST", headers=headers)
        try:
            return urllib.request.urlopen(req).status
        except urllib.error.HTTPError as e:
            return e.code

    event = json.dumps({"type": "quality.incident.raised", "title": "Ledger late", "entity_urn": LEDGER}).encode()
    assert post(event, {}) == 401
    assert post(event, {"Cairn-Signature": sign("wrong", event)}) == 401
    assert post(event, {"Cairn-Signature": sign("s3cret", event)}) == 204       # Cairn's signer, the example's check
    assert seen == [("/collab/enrich", "Bearer cx_incident-log.t")]
    page = urllib.request.urlopen(f"{base}/ui?urn={LEDGER}").read().decode()
    assert "Ledger late" in page and "FAILING" in page
    assert "No events yet" in urllib.request.urlopen(f"{base}/ui?urn=urn:li:dataset:other").read().decode()
    server.shutdown()
    cairn.shutdown()
