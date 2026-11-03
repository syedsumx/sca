"""Data apps deployed from git: a real repository and the git command, the app a real process started by
Cairn and reached through it. The commit an app runs is the one deployed, until it is deployed again; a
deployment that fails leaves nothing behind. Also the Kubernetes runtime's objects (applied for real in
tests/test_data_apps_k8s.py)."""

import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services

ADMIN, ANA = "urn:li:corpuser:admin", "urn:li:corpuser:ana"
SERVER = '''
import http.server, os, sys
VERSION = "{version}"

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.headers.get("X-Cairn-App-Key") == os.environ["CAIRN_APP_KEY"]
        cwd = os.path.basename(os.getcwd())
        body = f"{{VERSION}} viewer={{self.headers.get('X-Cairn-User')}} key={{'ok' if ok else 'wrong'}} cwd={{cwd}}"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass

http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@x", "GIT_COMMITTER_NAME": "dev",
                               "GIT_COMMITTER_EMAIL": "dev@x"}).stdout.strip()


def commit_version(dev, version):
    (dev / "apps" / "orders").mkdir(parents=True, exist_ok=True)
    (dev / "apps" / "orders" / "server.py").write_text(SERVER.format(version=version))
    git(dev, "add", "-A")
    git(dev, "commit", "--quiet", "-m", version)
    git(dev, "push", "--quiet", "origin", "main")
    return git(dev, "rev-parse", "HEAD")


@pytest.fixture
def world(settings, graph, tmp_path, monkeypatch):
    bare, dev = tmp_path / "apps.git", tmp_path / "dev"
    git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(bare))
    git(tmp_path, "clone", "--quiet", str(bare), str(dev))
    monkeypatch.setenv("CAIRN_APP_GIT_URLS", "file://")
    monkeypatch.setenv("CAIRN_DATA_DIR", str(tmp_path / "data"))
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="ana-orders", effect="allow", actions=["USE_APP"], condition={"attr": "subject.urn", "op": "eq", "value": ANA})]))
    app = create_api_app(s)
    yield {"s": s, "bare": bare, "dev": dev, "admin": TestClient(app, headers={"X-Cairn-Actor": ADMIN}),
           "ana": TestClient(app, headers={"X-Cairn-Actor": ANA})}
    for name in list(s.data_apps._running):
        s.data_apps.stop(name)


def test_an_app_runs_the_commit_it_was_deployed_at(world):
    s, admin, ana, bare = world["s"], world["admin"], world["ana"], world["bare"]
    v1 = commit_version(world["dev"], "version-1")
    body = {"name": "orders", "title": "Orders", "git": {"url": f"file://{bare}", "branch": "main", "path": "apps/orders"},
            "command": [sys.executable, "server.py", "{port}"]}
    # Only URLs the operator allows; a branch that is not there leaves no app behind.
    assert admin.post("/v1/apps", json={**body, "git": {**body["git"], "url": "https://evil.example.com/x.git"}}).status_code == 403
    r = admin.post("/v1/apps", json={**body, "git": {**body["git"], "branch": "nope"}})
    assert r.status_code == 502 and "could not be deployed" in r.json()["detail"], r.text
    assert admin.get("/v1/apps").json() == []
    assert ana.post("/v1/apps", json=body).status_code == 403                  # operators define apps
    r = admin.post("/v1/apps", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["commit"] == v1 and r.json()["git"]["path"] == "apps/orders"
    # Ana opens it: it runs in git.path of the deployed commit, told who she is and given its key.
    page = ana.get("/apps/orders/")
    assert page.status_code == 200, page.text
    assert page.text == f"version-1 viewer={ANA} key=ok cwd=orders"
    assert "git" not in ana.get("/v1/apps").json()[0]                        # how it runs is not hers to see
    # A new commit on the branch changes nothing until the app is deployed again.
    v2 = commit_version(world["dev"], "version-2")
    assert ana.get("/apps/orders/").text.startswith("version-1")
    assert ana.post("/v1/apps/orders/deploy").status_code == 403
    r = admin.post("/v1/apps/orders/deploy")
    assert r.status_code == 200 and r.json()["commit"] == v2, r.text
    assert ana.get("/apps/orders/").text.startswith("version-2")
    deployed = [e for e in s.audit.entries(limit=50) if e["action"] == "app.deployed"]
    assert [e["details"]["commit"] for e in deployed][:2] == [v2, v1]
    # The clone holds nothing the app wrote last time but what git has (a clean checkout).
    assert "server.py" in os.listdir(os.path.join(s.data_apps.workdir, "orders", "apps", "orders"))


def test_runtimes_and_sources_are_checked(world):
    from cairn.dataaccess.dataapps import AppError

    s = world["s"]
    for body, msg in [({"name": "a", "url": "http://x", "git": {"url": "file:///r"}}, "neither git"),
                      ({"name": "a", "command": ["x"], "runtime": "lambda"}, "runtime"),
                      ({"name": "a", "command": ["x"], "runtime": "kubernetes", "image": "python:3.12"}, "Kubernetes is not set up"),
                      ({"name": "a", "command": ["x"], "image": "python:3.12"}, "image is for runtime kubernetes"),
                      ({"name": "a", "command": ["x"], "git": {"url": "file:///r", "path": "../etc"}}, "git.path"),
                      ({"name": "a", "command": ["x"], "git": {"url": "file:///r", "branch": "-x"}}, "git.branch"),
                      ({"name": "a", "command": ["x"], "git": {"url": "file:///r", "repo": "r"}}, "not both"),
                      ({"name": "a", "command": ["x"], "git": {"url": "file:///r"}, "workdir": "/tmp"}, "workdir")]:
        with pytest.raises(AppError, match=msg):
            s.data_apps.create(ADMIN, body)


def test_the_kubernetes_objects_hold_no_secret_in_the_open_and_pin_the_commit():
    from cairn.dataaccess.apps_k8s import KubeApps, KubeError

    with pytest.raises(KubeError, match="INGRESS_FROM"):
        KubeApps("apps", api="https://k8s.invalid")
    k = KubeApps("apps", api="https://k8s.invalid", ingress_from=[{"ipBlock": {"cidr": "10.0.0.5/32"}}])
    app = {"name": "orders", "image": "python:3.12-slim", "port": 8080, "command": ["python", "server.py", "{port}", "{base}"]}
    git = {"url": "https://github.com/acme/apps.git", "branch": "main", "commit": "a" * 40, "path": "apps/orders",
           "header": "Authorization: Basic c2VjcmV0"}
    secret, dep, svc, pol = k.manifests(app, "the-app-key", git, 0)
    assert secret["stringData"] == {"CAIRN_APP_KEY": "the-app-key", "GIT_CONFIG_COUNT": "1",
                                    "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_CONFIG_VALUE_0": "Authorization: Basic c2VjcmV0"}
    text = json.dumps([dep, svc, pol])
    assert "c2VjcmV0" not in text and "the-app-key" not in text                # only references to the Secret
    pod = dep["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False and pod["securityContext"]["runAsUser"] == 10001
    [init], [main] = pod["initContainers"], pod["containers"]
    assert {e["name"]: e.get("value") for e in init["env"]}["CAIRN_GIT_COMMIT"] == "a" * 40
    assert main["command"] == ["python", "server.py", "8080", "apps/orders"] and main["workingDir"] == "/srv/app/apps/orders"
    assert main["securityContext"]["allowPrivilegeEscalation"] is False and main["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert dep["spec"]["replicas"] == 0 and svc["spec"]["type"] == "ClusterIP"
    assert pol["spec"]["ingress"] == [{"from": [{"ipBlock": {"cidr": "10.0.0.5/32"}}], "ports": [{"port": 8080, "protocol": "TCP"}]}]
    # A public repository needs no sign-in: no git config in the Secret or the pod.
    secret, dep, _, _ = k.manifests(app, "k", {**git, "header": None}, 1)
    assert set(secret["stringData"]) == {"CAIRN_APP_KEY"} and "GIT_CONFIG" not in json.dumps(dep)
