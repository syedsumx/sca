"""Managed model serving on a real Kubernetes cluster (kind), queue row 20: a registered version deployed from
Cairn runs as a Deployment of MLflow's model server, applied with the chart's own Role, scaled by Cairn (from zero
on the first call, back to zero when idle), invoked as the caller, and reached by Cairn only.

Needs a kind cluster (CAIRN_KIND_KUBECONFIG; CAIRN_KIND_HOST_IP, where pods reach this host), helm, MLflow's image
loaded into the cluster (CAIRN_ML_SERVING_TEST_IMAGE) and CAIRN_MLFLOW_PYTHON (a Python with mlflow 3.x) to run the
tracking server the model servers fetch their model from."""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import tempfile
import textwrap
import threading
import time

import httpx
import pytest
import yaml

KUBECONFIG = os.environ.get("CAIRN_KIND_KUBECONFIG", "")
HOST = os.environ.get("CAIRN_KIND_HOST_IP", "")
HELM = os.environ.get("CAIRN_HELM", "helm")
NODE = os.environ.get("CAIRN_KIND_NODE", "cairn-control-plane")
PY = os.environ.get("CAIRN_MLFLOW_PYTHON", "")
IMAGE = os.environ.get("CAIRN_ML_SERVING_TEST_IMAGE", "ghcr.io/mlflow/mlflow:v3.5.0")
pytestmark = pytest.mark.skipif(not (KUBECONFIG and HOST and shutil.which(HELM) and PY and os.path.exists(PY)),
                                reason="needs a kind cluster (CAIRN_KIND_KUBECONFIG, CAIRN_KIND_HOST_IP), helm and "
                                       "CAIRN_MLFLOW_PYTHON")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NS = "cairn-models"
ANA, CAROL = "urn:li:corpuser:ana", "urn:li:corpuser:carol"
TOKENS = {"tok-ana": ANA, "tok-carol": CAROL}
# A model with no library beyond MLflow's own, so MLflow's image serves it as it is.
MODEL = textwrap.dedent('''
    import mlflow
    from mlflow.pyfunc import PythonModel

    class Doubler(PythonModel):
        def predict(self, context, model_input, params=None):
            return [float(x) * 2 for x in model_input["x"]]

    mlflow.models.set_model(Doubler())
''')


def kubectl(*args, check=True, stdin=None):
    r = subprocess.run(["kubectl", "--kubeconfig", KUBECONFIG, *args], capture_output=True, text=True, input=stdin, timeout=300)
    if check and r.returncode:
        raise AssertionError(f"kubectl {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def port() -> int:
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def wait(url: str, seconds: int = 90, proc: subprocess.Popen | None = None, log: str = "") -> None:
    end = time.time() + seconds
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"the server exited ({proc.returncode}):\n{open(log).read()[-4000:] if log else ''}")
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{url} did not come up" + (f":\n{open(log).read()[-4000:]}" if log else ""))


@pytest.fixture
def cluster(tmp_path):
    """The models namespace with the chart's Role bound to Cairn's account; a token and CA for that account."""
    kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=true")
    for ns in (NS, "cairn"):
        kubectl("create", "namespace", ns, check=False)
    kubectl("-n", "cairn", "create", "serviceaccount", "t-cairn", check=False)
    out = subprocess.run([HELM, "template", "t", "helm/cairn", "--namespace", "cairn", "-s", "templates/ml-serving-kubernetes.yaml",
                          "--set", "cairn.mlflow.enabled=true", "--set", "cairn.mlflow.database.secretName=x",
                          "--set", "cairn.mlflow.artifacts.credentialsSecret=x",
                          "--set", "cairn.mlflow.servingKubernetes.enabled=true",
                          "--set", f"cairn.mlflow.servingKubernetes.namespace={NS}"], cwd=ROOT, capture_output=True, text=True)
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
def mlflow():
    """A tracking server serving its artifacts, where the pods reach it (this host, at HOST), with a registered
    model in it."""
    d = tempfile.mkdtemp(prefix="mlk8s-")
    p = port()
    bindir = os.path.dirname(PY)
    server = subprocess.Popen([f"{bindir}/mlflow", "server", "--backend-store-uri", f"sqlite:///{d}/mlflow.db",
                               "--artifacts-destination", f"{d}/artifacts", "--serve-artifacts",
                               "--host", "0.0.0.0", "--port", str(p), "--workers", "1"],
                              stdout=open(f"{d}/mlflow.log", "w"), stderr=subprocess.STDOUT)
    url = f"http://{HOST}:{p}"
    wait(f"http://127.0.0.1:{p}/health", 240, server, f"{d}/mlflow.log")   # a fresh environment's first start is slow
    wait(f"{url}/health", 30, server, f"{d}/mlflow.log")                   # and where the pods reach it
    open(f"{d}/model.py", "w").write(MODEL)
    r = subprocess.run([PY, "-c", "import mlflow\nmlflow.set_experiment('doubler')\nwith mlflow.start_run():\n"
                                  " mlflow.pyfunc.log_model(name='model', python_model='model.py', registered_model_name='doubler')"],
                       cwd=d, env={**os.environ, "MLFLOW_TRACKING_URI": url}, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-3000:]
    yield {"url": url, "dir": d}
    server.terminate()
    server.wait(15)


def test_a_model_deployment_on_kubernetes(cluster, mlflow, monkeypatch):
    import uvicorn
    from fastapi import FastAPI, HTTPException
    from sqlalchemy import create_engine, update
    from starlette.requests import Request

    from cairn.common.audit import AuditLog
    from cairn.common.core import InMemoryGraph
    from cairn.common.db import init_db
    from cairn.common.security import Principal
    from cairn.ml.mlflow_gateway import MlflowGateway
    from cairn.ml.serving import KubeRuntime, ModelServing, ServingError, build_serving_router, model_deployments

    engine = create_engine(f"sqlite:///{mlflow['dir']}/cairn.db")
    init_db(engine)
    gw = MlflowGateway(mlflow["url"], engine, AuditLog(engine), InMemoryGraph(),
                       lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{'.'.join(ns)}.{t},PROD)",
                       lambda p, priv, urn: p.urn == ANA)            # ana may manage and use the model; carol nothing
    # Cairn reaches the node ports from this host (the kind network's gateway): only that address may.
    rt = KubeRuntime(NS, mlflow["url"], IMAGE, [{"ipBlock": {"cidr": f"{HOST}/32"}}], api=cluster["api"],
                     token_file=cluster["token"], ca_file=cluster["ca"], access="node-port", node_address=cluster["node"])
    ms = ModelServing(engine, rt, gw, AuditLog(engine))
    ms.tick_s, ms.cold_s = 1.0, 300.0

    def who(request: Request) -> Principal:
        urn = TOKENS.get(request.headers.get("authorization", "").removeprefix("Bearer "))
        if urn is None:
            raise HTTPException(401, "sign in")
        return Principal(urn, "token")
    app = FastAPI()
    app.include_router(build_serving_router(ms, who))
    cp = port()
    api = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cp, log_level="warning"))
    threading.Thread(target=api.run, daemon=True).start()
    wait(f"http://127.0.0.1:{cp}/docs")
    base = f"http://127.0.0.1:{cp}/v1/ml/deployments"

    def dep(token, method, path="", **kw):
        return httpx.request(method, base + path, headers={"Authorization": f"Bearer {token}"}, timeout=360, **kw)

    rows = {"dataframe_split": {"columns": ["x"], "data": [[1], [2.5]]}}
    body = {"name": "doubler", "model": "doubler", "version": "1", "min_replicas": 0, "max_replicas": 2,
            "target_concurrency": 1, "idle_minutes": 1}
    try:
        assert dep("tok-carol", "POST", json=body).status_code == 403
        r = dep("tok-ana", "POST", json=body)
        assert r.status_code == 201, r.text
        # Its objects, stopped until it is called; pods with no token, run as a user without privileges.
        assert kubectl("-n", NS, "get", "deployment", "cairn-model-doubler", "-o", "jsonpath={.spec.replicas}") == "0"
        pod = json.loads(kubectl("-n", NS, "get", "deployment", "cairn-model-doubler", "-o", "json"))["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False and pod["securityContext"]["runAsUser"] == 10001
        assert "models:/doubler/1" in pod["containers"][0]["command"]

        # The first call starts a replica on the cluster and waits for it; the answer is the model's.
        started = time.time()
        r = dep("tok-ana", "POST", "/doubler/invocations", json=rows)
        if r.status_code != 200:
            print(r.text, kubectl("-n", NS, "get", "pods", "-o", "wide"),
                  kubectl("-n", NS, "logs", "-l", "cairn.io/model-deployment=doubler", "--tail=60", check=False), sep="\n")
        assert r.status_code == 200, r.text
        assert r.json()["predictions"] == [2.0, 5.0]
        print(f"\ncold start on Kubernetes through Cairn: {time.time() - started:.1f} s")
        assert kubectl("-n", NS, "get", "deployment", "cairn-model-doubler", "-o", "jsonpath={.spec.replicas}") == "1"
        assert dep("tok-carol", "POST", "/doubler/invocations", json=rows).status_code == 403

        # Only Cairn reaches the model's pods.
        pod_ip = kubectl("-n", NS, "get", "pods", "-l", "cairn.io/model-deployment=doubler", "-o",
                         "jsonpath={.items[0].status.podIP}")
        probe = kubectl("run", "intruder", "--rm", "-i", "--restart=Never", f"--image={IMAGE}",
                        "--image-pull-policy=IfNotPresent", "--command", "--", "python", "-c",
                        f"import urllib.request\ntry:\n urllib.request.urlopen('http://{pod_ip}:8080/ping', timeout=5)\n"
                        " print('REACHED')\nexcept Exception as e:\n print('REFUSED', type(e).__name__)", check=False)
        assert "REFUSED" in probe and "REACHED" not in probe, probe

        # Load: Cairn sets two replicas, and both serve.
        with engine.begin() as conn:
            conn.execute(update(model_deployments).values(replicas=2))
        rt.scale(ms._get("doubler"), 2)
        end = time.time() + 300
        while time.time() < end and kubectl("-n", NS, "get", "deployment", "cairn-model-doubler", "-o",
                                            "jsonpath={.status.readyReplicas}") != "2":
            time.sleep(2)
        assert kubectl("-n", NS, "get", "deployment", "cairn-model-doubler", "-o", "jsonpath={.status.readyReplicas}") == "2"
        assert rt.running(ms._get("doubler")) == 2
        # Idle past idle_minutes: back to zero, on the cluster too.
        ms.last_request.clear()
        with engine.begin() as conn:
            conn.execute(update(model_deployments).values(last_request_ms=int(time.time() * 1000) - 120_000))
        ms.sample()
        assert ms.autoscale()["doubler"] == 0
        assert kubectl("-n", NS, "get", "deployment", "cairn-model-doubler", "-o", "jsonpath={.spec.replicas}") == "0"

        # Cairn's account manages models in their namespace only.
        other = KubeRuntime("default", mlflow["url"], IMAGE, [{"ipBlock": {"cidr": f"{HOST}/32"}}], api=cluster["api"],
                            token_file=cluster["token"], ca_file=cluster["ca"])
        with pytest.raises(ServingError, match="403"):
            other.scale({"name": "x", "model": "doubler", "version": "1"}, 0)

        assert dep("tok-ana", "DELETE", "/doubler").status_code == 204
        assert kubectl("-n", NS, "get", "deployment,service,networkpolicy", "-l", "cairn.io/model-deployment=doubler",
                       "-o", "name") == ""
    finally:
        api.should_exit = True
