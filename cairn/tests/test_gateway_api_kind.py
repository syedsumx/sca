"""The chart's Gateway API routes on a real cluster and a real Gateway implementation: kind (CI's spark-kind job),
the Gateway API CRDs and Envoy Gateway. The chart's Gateway and HTTPRoute (core.gateway) are applied in front of
an echo backend that answers on each Cairn Service's port with the path and headers it received, and requests
through Envoy reach the right Service with the right path: the API and MCP prefixes stripped (with
X-Forwarded-Prefix), OAuth discovery unchanged, the UI and docs to the consumer UI, the rest to the web UI, and
the engine's developer consoles nowhere.

Needs CAIRN_KIND_KUBECONFIG, kubectl, helm and network access to pull the CRDs and Envoy Gateway."""

import json
import os
import shutil
import socket
import subprocess
import time

import httpx
import pytest

KUBECONFIG = os.environ.get("CAIRN_KIND_KUBECONFIG", "")
IMAGE = os.environ.get("CAIRN_APPS_TEST_IMAGE", "python:3.12-slim")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRDS = "https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml"
NS = "cairn-gw"

pytestmark = pytest.mark.skipif(not (KUBECONFIG and shutil.which("kubectl") and shutil.which("helm")),
                                reason="needs CAIRN_KIND_KUBECONFIG, kubectl and helm")

ECHO = r"""
import json, http.server, threading
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"port": self.server.server_port, "path": self.path,
                           "xfp": self.headers.get("X-Forwarded-Prefix")}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
servers = [http.server.ThreadingHTTPServer(("", p), H) for p in (9002, 8800, 8830, 8080)]
for s in servers[1:]:
    threading.Thread(target=s.serve_forever, daemon=True).start()
servers[0].serve_forever()
"""


def k(*args, stdin=None, check=True, timeout=300):
    r = subprocess.run(["kubectl", *args], input=stdin, capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "KUBECONFIG": KUBECONFIG})
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr}")
    return r.stdout


def port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def backend() -> str:
    services = "".join(f"""---
apiVersion: v1
kind: Service
metadata: {{name: t-cairn-{name}, namespace: {NS}}}
spec:
  selector: {{app: echo}}
  ports: [{{name: http, port: {p}, targetPort: {p}}}]
""" for name, p in (("web", 9002), ("api", 8800), ("mcp", 8830), ("consumer-ui", 8080)))
    return f"""apiVersion: apps/v1
kind: Deployment
metadata: {{name: echo, namespace: {NS}}}
spec:
  replicas: 1
  selector: {{matchLabels: {{app: echo}}}}
  template:
    metadata: {{labels: {{app: echo}}}}
    spec:
      containers:
        - name: echo
          image: {IMAGE}
          imagePullPolicy: IfNotPresent
          command: ["python", "-c", {json.dumps(ECHO)}]
          readinessProbe: {{tcpSocket: {{port: 8800}}, periodSeconds: 2}}
{services}"""


@pytest.fixture(scope="module")
def gateway():
    k("apply", "--server-side", "-f", CRDS)
    r = subprocess.run(["helm", "upgrade", "--install", "eg", "oci://docker.io/envoyproxy/gateway-helm", "--version", "v1.2.6",
                        "-n", "envoy-gateway-system", "--create-namespace", "--wait", "--timeout", "8m"],
                       capture_output=True, text=True, env={**os.environ, "KUBECONFIG": KUBECONFIG}, timeout=600)
    assert r.returncode == 0, f"installing Envoy Gateway failed:\n{r.stdout}\n{r.stderr}"
    k("apply", "-f", "-", stdin="""apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata: {name: eg}
spec: {controllerName: gateway.envoyproxy.io/gatewayclass-controller}
""")
    k("create", "namespace", NS, check=False)
    k("apply", "-f", "-", stdin=backend())
    k("-n", NS, "rollout", "status", "deployment/echo", "--timeout=180s")
    rendered = subprocess.run(["helm", "template", "t", os.path.join(ROOT, "helm", "cairn"), "--namespace", NS,
                               "--set", "core.gateway.enabled=true", "--set", "core.gateway.create.enabled=true",
                               "--set", "core.gateway.create.className=eg", "--set", "core.gateway.hostnames[0]=cairn.test",
                               "--show-only", "templates/gateway-api.yaml"],
                              check=True, capture_output=True, text=True).stdout
    k("-n", NS, "apply", "-f", "-", stdin=rendered)
    k("-n", NS, "wait", "--for=condition=Programmed", "gateway/t-cairn-gateway", "--timeout=300s")
    svc = ""
    end = time.time() + 120
    while not svc and time.time() < end:
        svc = k("-n", "envoy-gateway-system", "get", "svc", "-o", "name", "-l",
                f"gateway.envoyproxy.io/owning-gateway-name=t-cairn-gateway,gateway.envoyproxy.io/owning-gateway-namespace={NS}").strip()
        time.sleep(2)
    assert svc, "Envoy Gateway made no Service for the Gateway"
    local = port()
    pf = subprocess.Popen(["kubectl", "-n", "envoy-gateway-system", "port-forward", svc.splitlines()[0], f"{local}:80"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, "KUBECONFIG": KUBECONFIG})
    url = f"http://127.0.0.1:{local}"
    end = time.time() + 120
    while True:                                  # until Envoy has the routes (it answers 200 for the web UI)
        try:
            if httpx.get(f"{url}/", headers={"Host": "cairn.test"}, timeout=5).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        assert time.time() < end, k("-n", NS, "get", "httproute", "-o", "yaml")
        time.sleep(2)
    yield url
    pf.terminate()
    k("delete", "namespace", NS, "--wait=false", check=False)


def get(url, path, host="cairn.test"):
    return httpx.get(url + path, headers={"Host": host}, timeout=10)


def test_requests_reach_the_right_service_with_the_right_path(gateway):
    seen = lambda path: get(gateway, path).json()  # noqa: E731
    assert seen("/cairn/api/v1/health") == {"port": 8800, "path": "/v1/health", "xfp": "/cairn/api"}
    assert seen("/cairn/api") == {"port": 8800, "path": "/", "xfp": "/cairn/api"}
    assert seen("/.well-known/oauth-protected-resource")["path"] == "/.well-known/oauth-protected-resource"
    assert seen("/cairn/mcp/mcp") == {"port": 8830, "path": "/mcp", "xfp": None}
    assert seen("/cairn-ui/remoteEntry.js") == {"port": 8080, "path": "/cairn-ui/remoteEntry.js", "xfp": None}
    assert seen("/docs/administration")["port"] == 8080
    assert seen("/search?q=orders") == {"port": 9002, "path": "/search?q=orders", "xfp": None}
    # Not part of the product surface: nothing behind them answers.
    for console in ("/swagger-ui/index.html", "/openapi/v3/api-docs", "/api/graphiql"):
        r = get(gateway, console)
        assert r.status_code >= 400 and "port" not in r.text, (console, r.status_code, r.text)
    assert get(gateway, "/", host="elsewhere.test").status_code == 404           # only the release's host
