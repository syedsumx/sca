"""Data apps on a real Kubernetes cluster (kind): Cairn applies each app's objects with the chart's own Role,
the app's code comes from git at the deployed commit, the first visit starts it and idleness stops it, and
only Cairn reaches its pods.

Needs a kind cluster (CAIRN_KIND_KUBECONFIG; CAIRN_KIND_HOST_IP, where pods reach this host; the kind
network's node named cairn-control-plane), helm, git, and the app image (CAIRN_APPS_TEST_IMAGE) and git image
(CAIRN_APPS_K8S_GIT_IMAGE) loaded into the cluster."""

import base64
import json
import os
import shutil
import socket
import subprocess
import time

import pytest
import yaml
from fastapi.testclient import TestClient

KUBECONFIG = os.environ.get("CAIRN_KIND_KUBECONFIG", "")
HOST = os.environ.get("CAIRN_KIND_HOST_IP", "")
HELM = os.environ.get("CAIRN_HELM", "helm")
NODE = os.environ.get("CAIRN_KIND_NODE", "cairn-control-plane")
pytestmark = pytest.mark.skipif(not (KUBECONFIG and HOST and shutil.which("git") and shutil.which(HELM)),
                                reason="needs a kind cluster (CAIRN_KIND_KUBECONFIG, CAIRN_KIND_HOST_IP), helm and git")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN, ANA = "urn:li:corpuser:admin", "urn:li:corpuser:ana"
NS = "cairn-apps"
IMAGE = os.environ.get("CAIRN_APPS_TEST_IMAGE", "python:3.12-slim")      # loaded into the cluster
SERVER = '''
import http.server, os, sys
VERSION = "{version}"

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.headers.get("X-Cairn-App-Key") == os.environ["CAIRN_APP_KEY"]
        body = f"{{VERSION}} viewer={{self.headers.get('X-Cairn-User')}} key={{'ok' if ok else 'wrong'}} uid={{os.getuid()}}"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):
        pass

http.server.HTTPServer(("0.0.0.0", int(sys.argv[1])), H).serve_forever()
'''


def kubectl(*args, check=True, stdin=None):
    r = subprocess.run(["kubectl", "--kubeconfig", KUBECONFIG, *args], capture_output=True, text=True, input=stdin)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@x", "GIT_COMMITTER_NAME": "dev",
                               "GIT_COMMITTER_EMAIL": "dev@x"}).stdout.strip()


def free_port():
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def commit(dev, version):
    (dev / "apps" / "orders").mkdir(parents=True, exist_ok=True)
    (dev / "apps" / "orders" / "server.py").write_text(SERVER.format(version=version))
    git(dev, "add", "-A")
    git(dev, "commit", "--quiet", "-m", version)
    git(dev, "push", "--quiet", "origin", "main")
    return git(dev, "rev-parse", "HEAD")


@pytest.fixture
def cluster(tmp_path):
    """The apps namespace with the chart's Role bound to Cairn's account; a token and CA for that account."""
    kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=true")
    for ns in (NS, "cairn"):
        kubectl("create", "namespace", ns, check=False)
    kubectl("-n", "cairn", "create", "serviceaccount", "t-cairn", check=False)
    out = subprocess.run([HELM, "template", "t", "helm/cairn", "--namespace", "cairn", "-s", "templates/apps-kubernetes.yaml",
                          "--set", "cairn.apps.kubernetes.enabled=true", "--set", f"cairn.apps.kubernetes.namespace={NS}"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    kubectl("apply", "-f", "-", stdin=out.stdout)
    token = tmp_path / "token"
    token.write_text(kubectl("-n", "cairn", "create", "token", "t-cairn", "--duration", "1h"))
    cfg = yaml.safe_load(open(KUBECONFIG))["clusters"][0]["cluster"]
    ca = tmp_path / "ca.crt"
    ca.write_bytes(base64.b64decode(cfg["certificate-authority-data"]))
    node_ip = subprocess.run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", NODE],
                             capture_output=True, text=True, check=True).stdout.strip()
    yield {"api": cfg["server"], "token": str(token), "ca": str(ca), "node": node_ip}
    kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=false")


@pytest.fixture
def repo(tmp_path):
    """A git repository served by git daemon, which the pods reach at HOST."""
    root, dev = tmp_path / "srv", tmp_path / "dev"
    root.mkdir()
    git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(root / "apps.git"))
    git(tmp_path, "clone", "--quiet", str(root / "apps.git"), str(dev))
    port = free_port()
    daemon = subprocess.Popen(["git", "daemon", "--export-all", "--reuseaddr", f"--base-path={root}", "--listen=0.0.0.0",
                               f"--port={port}", str(root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    yield {"url": f"git://{HOST}:{port}/apps.git", "dev": dev}
    daemon.terminate()


def test_an_app_on_kubernetes_from_git(cluster, repo, settings, graph, monkeypatch):
    from cairn.app import create_api_app
    from cairn.dataaccess.apps_k8s import KubeApps, KubeError
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.services import build_services

    v1 = commit(repo["dev"], "version-1")
    env = {"CAIRN_APP_GIT_URLS": f"git://{HOST}:", "CAIRN_APPS_K8S_NAMESPACE": NS, "CAIRN_APPS_K8S_API": cluster["api"],
           "CAIRN_APPS_K8S_TOKEN_FILE": cluster["token"], "CAIRN_APPS_K8S_CA_FILE": cluster["ca"],
           "CAIRN_APPS_K8S_ACCESS": "node-port", "CAIRN_APPS_K8S_NODE_ADDRESS": cluster["node"],
           # Cairn reaches the node from this host, at the kind network's gateway.
           "CAIRN_APPS_K8S_INGRESS_FROM": json.dumps([{"ipBlock": {"cidr": f"{HOST}/32"}}])}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="ana", effect="allow", actions=["USE_APP"], condition={"attr": "subject.urn", "op": "eq", "value": ANA})]))
    app = create_api_app(s)
    admin, ana = TestClient(app, headers={"X-Cairn-Actor": ADMIN}), TestClient(app, headers={"X-Cairn-Actor": ANA})

    r = admin.post("/v1/apps", json={"name": "orders", "title": "Orders", "runtime": "kubernetes", "image": IMAGE,
                                     "port": 8080, "command": ["python", "server.py", "{port}"], "idle_minutes": 1,
                                     "git": {"url": repo["url"], "branch": "main", "path": "apps/orders"}})
    assert r.status_code == 201, r.text
    assert r.json()["commit"] == v1
    # Its objects, stopped until someone visits; the key only in the Secret.
    assert kubectl("-n", NS, "get", "deployment", "cairn-app-orders", "-o", "jsonpath={.spec.replicas}") == "0"
    assert kubectl("-n", NS, "get", "networkpolicy", "cairn-app-orders", "-o", "name")
    dep = json.loads(kubectl("-n", NS, "get", "deployment", "cairn-app-orders", "-o", "json"))
    assert s.data_apps.key("orders") not in json.dumps(dep)

    # Ana's first visit starts it; the pod runs the deployed commit, as a user without privileges.
    started = time.time()
    page = ana.get("/apps/orders/", timeout=300)
    assert page.status_code == 200, (page.text, kubectl("-n", NS, "get", "pods", "-o", "wide"),
                                     kubectl("-n", NS, "get", "events", "--sort-by=.lastTimestamp"))
    assert page.text == f"version-1 viewer={ANA} key=ok uid=10001", page.text
    print(f"\ncold start through Cairn: {time.time() - started:.1f} s")
    assert kubectl("-n", NS, "get", "deployment", "cairn-app-orders", "-o", "jsonpath={.spec.replicas}") == "1"

    # Only Cairn reaches the app's pods: another pod in the cluster is refused by the network policy.
    pod_ip = kubectl("-n", NS, "get", "pods", "-l", "cairn.io/app=orders", "-o", "jsonpath={.items[0].status.podIP}")
    probe = kubectl("run", "intruder", "--rm", "-i", "--restart=Never", f"--image={IMAGE}", "--image-pull-policy=IfNotPresent",
                    "--command", "--", "python", "-c",
                    f"import urllib.request\ntry:\n urllib.request.urlopen('http://{pod_ip}:8080/', timeout=5)\n print('REACHED')\n"
                    "except Exception as e:\n print('REFUSED', type(e).__name__)", check=False)
    assert "REFUSED" in probe and "REACHED" not in probe, probe

    # A new commit is not live until it is deployed; then the pod rolls to it.
    v2 = commit(repo["dev"], "version-2")
    assert ana.get("/apps/orders/").text.startswith("version-1")
    r = admin.post("/v1/apps/orders/deploy")
    assert r.status_code == 200 and r.json()["commit"] == v2, r.text
    end = time.time() + 180
    text = ""
    while time.time() < end and not text.startswith("version-2"):
        try:
            text = ana.get("/apps/orders/", timeout=30).text
        except Exception:  # noqa: BLE001 - the rollout swaps pods
            text = ""
        time.sleep(2)
    assert text.startswith("version-2"), (text, kubectl("-n", NS, "get", "pods"))

    # Idle past idle_minutes: scaled back to zero, whichever Cairn served it.
    import asyncio

    assert asyncio.run(s.data_apps.kube.stop_idle({"orders": 1}, now=time.time() + 120)) == ["orders"]
    assert kubectl("-n", NS, "get", "deployment", "cairn-app-orders", "-o", "jsonpath={.spec.replicas}") == "0"

    # Cairn's account manages apps in their namespace only.
    other = KubeApps("default", api=cluster["api"], token_file=cluster["token"], ca_file=cluster["ca"],
                     ingress_from=[{"ipBlock": {"cidr": f"{HOST}/32"}}])
    with pytest.raises(KubeError, match="403"):
        asyncio.run(other.deploy({"name": "x", "image": IMAGE, "port": 8080, "command": ["python"]}, "k", None))

    # Deleting the app removes its objects.
    assert admin.delete("/v1/apps/orders").status_code == 204
    assert kubectl("-n", NS, "get", "deployment,service,secret,networkpolicy", "-l", "cairn.io/app=orders", "-o", "name") == ""
