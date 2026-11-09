"""The ``http`` flow step (L138) end to end against a real HTTP service in a container (``python:3.12-slim`` running
a partner-style REST API over TLS with a private CA): a POST with a JSON body built from the flow's parameters and a
bearer token read from a secret scope as the owner, then polling the import's status URL (from the first answer) until
it is done, its id extracted with a JSONPath for the next step; basic and header-key auth; a failed import failing the
step; an unexpected status failing it with the service's message. TLS is always verified: the private CA kept in the
scope is trusted, and without it the same service is refused. With egress enforced, the request goes through Cairn's
real egress proxy (cairn.network.proxy): a host an egress rule allows is reached (TLS still verified end to end), a
host no rule allows is refused there, and an enforced egress without a proxy sends nothing. No secret is in the run's
output or logs, and a missing secret fails the step before any request.

Needs docker and the image CAIRN_FLOWS_TEST_HTTP_IMAGE (default python:3.12-slim)."""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time

import pytest

from cairn.common.db import get_engine, init_db
from cairn.flows.engine import Flows
from cairn.flows.steps import Backends
from platform_standins import tls_files

IMAGE = os.environ.get("CAIRN_FLOWS_TEST_HTTP_IMAGE", "python:3.12-slim")
ANA = "urn:li:corpuser:ana"


def _docker() -> bool:
    return bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


pytestmark = pytest.mark.skipif(not _docker(), reason="needs docker")

SERVER = r'''
import base64, json, ssl, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
port = int(sys.argv[1])
token, password, key = (open("/srv/" + f).read().strip() for f in ("token", "password", "apikey"))
imports, lock = {}, threading.Lock()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        sys.stderr.write("%s %s\n" % (self.command, self.path))
    def send(self, code, doc, headers=()):
        raw = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def bearer(self):
        if self.headers.get("Authorization") != "Bearer " + token:
            self.send(401, {"error": "unauthorized", "message": "a valid bearer token is required"})
            return False
        return True
    def do_POST(self):
        if not self.bearer():
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/v1/imports":
            with lock:
                iid = "imp-%d" % (len(imports) + 1)
                imports[iid] = {"looks": 0, "dataset": body.get("dataset"), "day": body.get("day"), "fail": body.get("dataset") == "broken"}
            return self.send(202, {"id": iid, "state": "queued", "received": body}, [("Location", "/v1/imports/" + iid)])
        if self.path == "/v1/teapot":
            return self.send(418, {"message": "I'm a teapot, not an importer"})
        self.send(404, {"message": "no such endpoint"})
    def do_GET(self):
        if self.path.startswith("/v1/imports/"):
            if not self.bearer():
                return
            iid = self.path.rsplit("/", 1)[1]
            with lock:
                imp = imports.get(iid)
                if imp is None:
                    return self.send(404, {"message": "no import " + iid})
                imp["looks"] += 1
                state = "queued" if imp["looks"] < 2 else "running" if imp["looks"] < 3 else ("failed" if imp["fail"] else "done")
            return self.send(200, {"id": iid, "state": state, "dataset": imp["dataset"], "day": imp["day"], "rows": 42})
        if self.path == "/v1/basic":
            if self.headers.get("Authorization") != "Basic " + base64.b64encode(("cairn:" + password).encode()).decode():
                return self.send(401, {"message": "basic auth required"})
            return self.send(200, {"who": "cairn"})
        if self.path == "/v1/key":
            if self.headers.get("X-Api-Key") != key:
                return self.send(403, {"message": "a key is required"})
            return self.send(200, {"ok": True, "items": [{"id": 7}, {"id": 8}]})
        self.send(404, {"message": "no such endpoint"})

ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain("/srv/server.pem", "/srv/server-key.pem")
srv = ThreadingHTTPServer(("127.0.0.1", port), H)
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
print("ready", flush=True)
srv.serve_forever()
'''


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def service(tmp_path_factory):
    d = tmp_path_factory.mktemp("partner")
    ca, cert, key = tls_files(str(d), ("127.0.0.1", "localhost", "partner.test", "other.test"))
    creds = {"token": "tok-" + secrets.token_urlsafe(24), "password": "pw-" + secrets.token_urlsafe(16),
             "apikey": "key-" + secrets.token_urlsafe(16)}
    for k, v in creds.items():
        (d / k).write_text(v)
        os.chmod(d / k, 0o644)
    (d / "server.py").write_text(SERVER)
    port = _port()
    name = f"cairn-flows-http-{os.getpid()}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", name, "--network", "host", "-v", f"{d}:/srv:ro", IMAGE,
                    "python", "/srv/server.py", str(port)], check=True, capture_output=True, timeout=120)
    end = time.time() + 60
    while "ready" not in subprocess.run(["docker", "logs", name], capture_output=True, text=True).stdout:
        if time.time() > end:
            raise AssertionError(subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr[-2000:])
        time.sleep(0.3)
    yield {"port": port, "url": f"https://127.0.0.1:{port}", "ca": open(ca).read(), "name": name, **creds}
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


@pytest.fixture
def env(tmp_path, monkeypatch, service):
    from cairn.identity.secret_scopes import SecretScopes

    monkeypatch.setenv("CAIRN_SECRETS_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("CAIRN_FLOWS_ALLOW_PRIVATE_NETWORKS", "true")   # the service listens on 127.0.0.1
    meta = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(meta)
    ss = SecretScopes(meta, lambda a: [], lambda a: False)
    ss.create(ANA, {"name": "partner"})
    for k, v in {"token": service["token"], "password": service["password"], "apikey": service["apikey"],
                 "ca": service["ca"]}.items():
        ss.put(ANA, "partner", k, v)
    f = Flows(meta, Backends(secrets=ss, notify=lambda *a: None), None, lambda *a: None, lambda a: False, lambda a: [],
              None, None, None, None, "one")
    stop = threading.Event()

    def tick():
        while not stop.wait(0.3):
            try:
                f.run_due()
            except Exception:  # noqa: BLE001 - shown by the assertions
                pass
    threading.Thread(target=tick, daemon=True).start()
    f.meta = meta
    yield f
    stop.set()


def run(env, flow, timeout=60):
    env.create(ANA, flow, publish=True)
    return env.wait_for(env.run(ANA, flow["name"])["id"], timeout)


def steps_of(r):
    return {s["key"]: s for s in r["steps"]}


def why(r):
    return [(s["key"], s["status"], s["error"]) for s in r["steps"]]


AUTH = {"auth": "bearer", "secret_scope": "partner", "secret_key": "token", "ca_key": "ca"}


def test_a_call_polled_to_done_with_a_token_from_a_secret_scope(env, service):
    url = service["url"]
    flow = {"name": "partner", "params": [{"name": "day", "default": "2026-11-09"}], "steps": [
        {"name": "send", "type": "http", "method": "POST", "url": f"{url}/v1/imports", **AUTH,
         "body": {"dataset": "order_scores", "day": "{{ params.day }}"}, "poll_url": f"{url}/v1/imports/{{$.id}}",
         "status_path": "$.state", "success_values": ["done"], "failure_values": ["failed"], "poll_s": 1,
         "output_path": "$.rows"},
        {"name": "located", "type": "http", "after": ["send"], "method": "POST", "url": f"{url}/v1/imports", **AUTH,
         "body": {"dataset": "again"}, "status_path": "$.state", "success_values": ["done"], "poll_s": 1},
        {"name": "basic", "type": "http", "after": ["send"], "url": f"{url}/v1/basic", "auth": "basic",
         "username": "cairn", "secret_scope": "partner", "secret_key": "password", "ca_key": "ca",
         "output_path": "$.who"},
        {"name": "key", "type": "http", "after": ["send"], "url": f"{url}/v1/key", "auth": "header",
         "auth_header": "X-Api-Key", "secret_scope": "partner", "secret_key": "apikey", "ca_key": "ca",
         "output_path": "$.items[*].id"},
        {"name": "use", "type": "branch", "after": ["send", "key"], "condition": "steps.send.result == 42",
         "then": ["ok"]},
        {"name": "ok", "type": "wait", "after": ["use"], "seconds": 1}]}
    done = run(env, flow)
    assert done["status"] == "succeeded", why(done)
    s = steps_of(done)
    assert s["send"]["output"]["result"] == 42 and s["send"]["output"]["polls"] == 3, s["send"]["output"]
    assert s["send"]["output"]["body"] == {"id": "imp-1", "state": "done", "dataset": "order_scores", "day": "2026-11-09",
                                           "rows": 42}
    assert s["located"]["output"]["body"]["id"] == "imp-2"          # its status URL from the Location header
    assert s["basic"]["output"]["result"] == "cairn" and s["key"]["output"]["result"] == [7, 8]
    assert s["use"]["output"]["chosen"] == "then"
    log = env.step_log(ANA, done["id"], "send")["log"]
    assert f"POST {url}/v1/imports: 202" in log and "looking at" in log and "$.state is done after 3 looks" in log
    text = json.dumps([env.run_detail(ANA, done["id"])] + [env.step_log(ANA, done["id"], k) for k in s], default=str)
    for v in (service["token"], service["password"], service["apikey"]):
        assert v not in text
    stored = json.dumps(env.get(ANA, "partner"), default=str)
    assert service["token"] not in stored and "partner" in stored


def test_failures_tls_and_missing_secrets(env, service):
    url = service["url"]
    done = run(env, {"name": "broken", "steps": [
        {"name": "send", "type": "http", "method": "POST", "url": f"{url}/v1/imports", **AUTH, "body": {"dataset": "broken"},
         "status_path": "$.state", "success_values": ["done"], "failure_values": ["failed"], "poll_s": 1}]})
    assert "$.state is failed: the remote work failed" in steps_of(done)["send"]["error"], why(done)
    done = run(env, {"name": "teapot", "steps": [{"name": "t", "type": "http", "method": "POST",
                                                  "url": f"{url}/v1/teapot", **AUTH, "body": {}}]})
    assert "answered 418: I'm a teapot" in steps_of(done)["t"]["error"], why(done)
    done = run(env, {"name": "accepted", "steps": [{"name": "t", "type": "http", "method": "POST",
                                                    "url": f"{url}/v1/teapot", **AUTH, "body": {}, "ok_status": [418]}]})
    assert done["status"] == "succeeded" and steps_of(done)["t"]["output"]["status"] == 418
    # TLS is verified: without the private CA the service is not trusted.
    done = run(env, {"name": "untrusted", "steps": [{"name": "t", "type": "http", "url": f"{url}/v1/basic"}]})
    assert "TLS certificate is not trusted" in steps_of(done)["t"]["error"], why(done)
    # A wrong token: the service's 401, nothing leaked.
    env.b.secrets.put(ANA, "partner", "stale", "not-the-token")
    done = run(env, {"name": "stale", "steps": [{"name": "t", "type": "http", "url": f"{url}/v1/imports/imp-1",
                                                 **{**AUTH, "secret_key": "stale"}}]})
    assert "answered 401" in steps_of(done)["t"]["error"] and "not-the-token" not in json.dumps(done)
    # A missing secret: the step fails before any request reaches the service.
    seen = subprocess.run(["docker", "logs", service["name"]], capture_output=True, text=True).stderr.count("\n")
    done = run(env, {"name": "nokey", "steps": [{"name": "t", "type": "http", "url": f"{url}/v1/key", "auth": "header",
                                                 "auth_header": "X-Api-Key", "secret_scope": "partner",
                                                 "secret_key": "gone", "ca_key": "ca"}]})
    assert "cannot read 'gone'" in steps_of(done)["t"]["error"], why(done)
    time.sleep(0.5)
    assert subprocess.run(["docker", "logs", service["name"]], capture_output=True, text=True).stderr.count("\n") == seen


def test_egress_rules_apply_through_the_egress_proxy(env, service, monkeypatch):
    from cairn.network import proxy as P
    from cairn.network.egress import Egress, EgressRule

    key = secrets.token_bytes(32)
    names = {"partner.test": "127.0.0.1", "other.test": "127.0.0.1"}
    # The real proxy; names resolve to the service (allow_private: an allowed host on a private network).
    px = P.EgressProxy("127.0.0.1:0", key, [], resolve=lambda h, p: [names.get(h, h)], allow_private=True).start()
    try:
        eg = Egress(env.meta, enforce=True, proxy=f"http://127.0.0.1:{px.port}", key=key)
        eg.put(EgressRule(name="partner", host="partner.test", ports=[service["port"]]), "urn:li:corpuser:admin")
        env.b.egress = eg
        port = service["port"]
        done = run(env, {"name": "egress", "steps": [
            {"name": "allowed", "type": "http", "url": f"https://partner.test:{port}/v1/key", "auth": "header",
             "auth_header": "X-Api-Key", "secret_scope": "partner", "secret_key": "apikey", "ca_key": "ca"},
            {"name": "refused", "type": "http", "url": f"https://other.test:{port}/v1/key", "auth": "header",
             "auth_header": "X-Api-Key", "secret_scope": "partner", "secret_key": "apikey", "ca_key": "ca"}]})
        s = steps_of(done)
        assert s["allowed"]["status"] == "ok" and s["allowed"]["output"]["body"]["ok"] is True, why(done)
        assert s["refused"]["status"] == "failed" and "egress refused other.test" in s["refused"]["error"], why(done)
        assert px.denied >= 1
        # Enforced without a proxy: nothing goes out.
        env.b.egress = Egress(env.meta, enforce=True, proxy="", key=key)
        done = run(env, {"name": "noproxy", "steps": [{"name": "t", "type": "http", "url": f"{service['url']}/v1/basic",
                                                       "ca_key": "ca", "secret_scope": "partner"}]})
        assert "egress is enforced and the egress proxy cannot be used" in steps_of(done)["t"]["error"], why(done)
    finally:
        env.b.egress = None
        px.close()
