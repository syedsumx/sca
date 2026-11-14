"""The JavaScript and Java clients (L65) against a real Cairn API over HTTP, and the deprecation policy.

Cairn runs in this process on a real port with a SQLite warehouse; the clients run as their own processes (node,
java) and call it with ana's personal access token (checked by a stand-in for Cairn Core's ``me`` query), an agent
key acting for her, and as ana in dev mode. They see governed answers (a PII column
masked, a table denied), get Deprecation and Sunset headers from a deprecated route and 410 from a retired one."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from cairn.common import deprecation
from cairn.common.deprecation import Deprecated

ROOT = Path(__file__).resolve().parent.parent
ANA = "urn:li:corpuser:ana"
EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"
USERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.users,PROD)"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"
PAT = "pat-" + "a" * 32
SECRETS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.secrets,PROD)"


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _CoreMe(BaseHTTPRequestHandler):
    """Stands in for Cairn Core's ``me`` query, which is how Cairn checks a personal access token."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        ok = self.headers.get("Authorization") == f"Bearer {PAT}"
        body = json.dumps({"data": {"me": {"corpUser": {"urn": ANA, "username": "ana"}}}} if ok else
                          {"errors": [{"message": "unauthorized"}]}).encode()
        self.send_response(200 if ok else 401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def cairn(graph, tmp_path, monkeypatch):
    import uvicorn

    from cairn.common.config import Settings

    from cairn.app import create_api_app
    from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target
    from cairn.identity.agents import AgentIn
    from cairn.quality.collectors import ConnectionRegistry
    from cairn.services import build_services
    from test_custom_agents import Model, text as said, tool

    wh = f"sqlite:///{tmp_path}/wh.db"
    with create_engine(wh).begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER, email TEXT)"))
        c.execute(text("CREATE TABLE orders (id INTEGER, user_id INTEGER)"))
        c.execute(text("CREATE TABLE secrets (k TEXT)"))
        c.execute(text("INSERT INTO users VALUES (1, 'kim@acme.com'), (2, 'lee@acme.com')"))
        c.execute(text("INSERT INTO orders VALUES (1, 1), (2, 1), (3, 2)"))
    graph.aspects[(USERS, "schemaMetadata")] = {"fields": [
        {"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}}]}
    graph.aspects[(ORDERS, "schemaMetadata")] = {"fields": [{"fieldPath": "id"}, {"fieldPath": "user_id"}]}
    core = ThreadingHTTPServer(("127.0.0.1", 0), _CoreMe)
    threading.Thread(target=core.serve_forever, daemon=True).start()
    settings = Settings(database_url=f"sqlite:///{tmp_path}/cairn.db", service_token="svc-secret",
                        dev_trust_actor_header=True, gateway={"key_dir": str(tmp_path / "keys")},
                        core={"url": f"http://127.0.0.1:{core.server_address[1]}"})
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.data_access.registry = ConnectionRegistry({"wh": {"url": wh, "catalog": {"platform": "sqlite"}}})
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"],
               condition={"attr": "subject.urn", "op": "eq", "value": "urn:li:corpuser:admin"}),
        Policy(id="read", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               condition={"attr": "subject.urn", "op": "exists"}),
        Policy(id="secrets", effect="deny", actions=["VIEW_DATASET_PROFILE"], target=Target(resource_urns=[SECRETS])),
        Policy(id="mask-pii", effect="obligation", actions=["VIEW_DATASET_PROFILE"],
               obligations=[Obligation(type="mask_columns", params={"labels": ["PII"], "strategy": "partial"})]),
        Policy(id="use", effect="allow", actions=["USE_AGENT"], condition={"attr": "subject.urn", "op": "eq", "value": ANA}),
    ]))
    key = s.agents.create_key_agent(AgentIn(name="Reporting job", scopes=["read", "query_data"], owner=ANA),
                                    "urn:li:corpuser:admin")["key"]
    model = Model()
    s.assistant.llm = model
    s.custom_agents.create("urn:li:corpuser:admin", {
        "name": "steward", "instructions": "Report on quality.", "tools": ["get_quality_status"], "scopes": ["read"]})
    s.custom_agents.deploy("urn:li:corpuser:admin", "steward")
    model.script = [[tool("get_quality_status", dataset_urn=ORDERS)], [said("fine")]] * 2
    monkeypatch.setattr(deprecation, "DEPRECATIONS", [
        Deprecated("GET", "/sql/saved", date(2026, 1, 1), date(2099, 1, 1), "/v2/sql/saved", "test"),
        Deprecated("GET", "/v1/datasets/changes", date(2025, 1, 1), date(2025, 12, 1), None, "test")])
    app = create_api_app(s)
    port = _port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    env = {**os.environ, "CAIRN_TEST_URL": f"http://127.0.0.1:{port}", "CAIRN_TEST_TOKEN": PAT, "CAIRN_TEST_AGENT_KEY": key,
           "CAIRN_TEST_PERSON_HEADER": f"X-Cairn-Actor:{ANA}", "CAIRN_TEST_DATASET": ORDERS}
    yield env
    server.should_exit = True
    core.shutdown()
    core.server_close()


def _run(cmd: list[str], cwd: Path, env: dict) -> str:
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, f"{' '.join(cmd)} failed:\n{r.stdout[-4000:]}\n{r.stderr[-4000:]}"
    return r.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_javascript_client(cairn):
    out = _run(["node", "--test", "test/client.test.mjs", "test/live.test.mjs"], ROOT / "sdks" / "js", cairn)
    assert "# fail 0" in out and "# skipped 0" in out, out


@pytest.mark.skipif(shutil.which("javac") is None, reason="needs a JDK")
def test_the_java_client(cairn, tmp_path):
    java = ROOT / "sdks" / "java"
    sources = [str(p) for p in java.rglob("*.java")]
    _run(["javac", "-Xlint:all", "-Werror", "--release", "17", "-d", str(tmp_path / "classes"), *sources], java, cairn)
    out = _run(["java", "-cp", str(tmp_path / "classes"), "io.cairn.client.ClientCheck"], java, cairn)
    assert "(live included)" in out, out


def test_the_deprecation_policy(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    with pytest.raises(ValueError, match="180 days"):
        Deprecated("GET", "/x", date(2026, 1, 1), date(2026, 3, 1))
    app = FastAPI()

    @app.get("/things/{tid}")
    def thing(tid: str):
        return {"id": tid}

    @app.get("/old")
    def old():
        return {"ok": True}

    deprecation.install(app, [Deprecated("GET", "/things/{tid}", date(2026, 1, 1), date(2099, 6, 30), "/v2/things/{tid}"),
                              Deprecated("*", "/old", date(2025, 1, 1), date(2025, 7, 1))])
    c = TestClient(app)
    r = c.get("/things/7")
    assert r.json() == {"id": "7"} and r.headers["deprecation"] == "@1767225600"
    assert r.headers["sunset"] == "Tue, 30 Jun 2099 00:00:00 GMT"
    assert r.headers["link"] == '</v2/things/{tid}>; rel="successor-version"'
    gone = c.get("/old")
    assert gone.status_code == 410 and "retired on 2025-07-01" in gone.json()["detail"] and "sunset" in gone.headers
    assert "deprecation" not in c.get("/deprecations").headers
    op = c.get("/openapi.json").json()["paths"]["/things/{tid}"]["get"]
    assert op["deprecated"] is True and op["x-sunset"] == "2099-06-30"
    # Before its deprecation day a route is untouched.
    monkeypatch.setattr(deprecation, "_today", lambda: date(2025, 12, 31))
    assert "deprecation" not in c.get("/things/7").headers
