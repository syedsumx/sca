"""Managed Airflow (L144) on a real Kubernetes cluster (kind): Cairn renders the official Airflow chart (pinned) and
applies it with only the chart's ClusterRole; the deployment's pods come up (API server, scheduler, DAG processor,
triggerer, a CloudNativePG metadata database); its pods' projected token is exchanged at Cairn for the pipeline user;
a DAG-only deploy reaches the DAG processor through the sync sidecar (fetched from Cairn as the pipeline user) and is
parsed there; hibernation scales everything to zero and waking brings it back.

Needs a kind cluster (CAIRN_KIND_KUBECONFIG, CAIRN_KIND_HOST_IP: where pods reach this host), helm, the Airflow chart
(CAIRN_AIRFLOW_CHART), an Airflow image with cairn-airflow-provider loaded into the cluster
(CAIRN_AIRFLOW_KIND_IMAGE), CAIRN_STORE_BIN, and internet access for CloudNativePG (CI job spark-kind)."""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request

import pytest
from starlette.requests import Request  # module level: FastAPI resolves the handler's annotations here

KUBECONFIG = os.environ.get("CAIRN_KIND_KUBECONFIG", "")
HOST = os.environ.get("CAIRN_KIND_HOST_IP", "")
HELM = os.environ.get("CAIRN_HELM", "helm")
CHART = os.environ.get("CAIRN_AIRFLOW_CHART", "")
IMAGE = os.environ.get("CAIRN_AIRFLOW_KIND_IMAGE", "")
STORE = os.environ.get("CAIRN_STORE_BIN", "")
pytestmark = pytest.mark.skipif(not (KUBECONFIG and HOST and CHART and IMAGE and STORE and shutil.which(HELM)),
                                reason="needs a kind cluster, helm, CAIRN_AIRFLOW_CHART, CAIRN_AIRFLOW_KIND_IMAGE and "
                                       "CAIRN_STORE_BIN")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN = "urn:li:corpuser:admin"
NAME = "kindaf"
NS = f"cairn-af-{NAME}"
DAG = '''
from datetime import datetime

from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.sdk import DAG

with DAG("from_cairn", start_date=datetime(2026, 1, 1), schedule=None):
    EmptyOperator(task_id="noop")
'''


def kubectl(*args, check=True, stdin=None):
    r = subprocess.run(["kubectl", "--kubeconfig", KUBECONFIG, *args], capture_output=True, text=True, input=stdin)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(what, fn, timeout=900, every=5):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = fn()
            if last:
                return last
        except Exception as e:  # noqa: BLE001 - not yet
            last = e
        time.sleep(every)
    raise AssertionError(f"{what}: not within {timeout} s ({last})")


@pytest.fixture(scope="module")
def cairn(tmp_path_factory):
    """Cairn's managed Airflow in this process, served on this host (pods exchange their token and fetch DAGs here)."""
    import uvicorn
    from fastapi import FastAPI
    from sqlalchemy import create_engine

    from cairn.airflow_deploy.deployments import AirflowDeployments, DeployStore, build_airflow_router
    from cairn.airflow_deploy.identity import Keys
    from cairn.airflow_deploy.k8s_runtime import KubernetesRuntime
    from cairn.common.db import metadata
    from cairn.common.security import Principal
    from cairn.databases.cnpg import CNPG_MANIFEST
    from cairn.dataaccess.trino_auth import TrinoTokens
    from cairn.dataaccess.trino_oauth import TrinoOAuth
    from cairn.identity.workload import WorkloadIdentity, build_workload_router, parse_issuers

    tmp = tmp_path_factory.mktemp("afkind")
    manifest = tmp / "cnpg.yaml"
    for attempt in range(6):
        try:
            manifest.write_bytes(urllib.request.urlopen(CNPG_MANIFEST, timeout=60).read())
            break
        except OSError:
            time.sleep(10 * (attempt + 1))
    kubectl("apply", "--server-side", "-f", str(manifest))
    kubectl("-n", "cnpg-system", "rollout", "status", "deployment/cnpg-controller-manager", "--timeout=300s")
    kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=true")
    kubectl("create", "namespace", "cairn", check=False)
    kubectl("-n", "cairn", "create", "serviceaccount", "t-cairn", check=False)
    out = subprocess.run([HELM, "template", "t", "./helm/cairn", "--namespace", "cairn", "-s",
                          "templates/airflow-deployments.yaml", "--set", "cairn.airflowDeployments.runtime=kubernetes"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    kubectl("apply", "-f", "-", stdin=out.stdout)
    token = tmp / "token"
    token.write_text(kubectl("-n", "cairn", "create", "token", "t-cairn", "--duration", "3h"))
    ca = tmp / "ca.crt"
    cfg = json.loads(kubectl("config", "view", "--raw", "--minify", "-o", "json"))
    ca.write_bytes(base64.b64decode(cfg["clusters"][0]["cluster"]["certificate-authority-data"]))
    api = cfg["clusters"][0]["cluster"]["server"]
    # The cluster's service-account issuer, its keys from the API server (as Cairn's would read them in the cluster).
    oidc = json.loads(kubectl("get", "--raw", "/.well-known/openid-configuration"))
    (tmp / "jwks.json").write_text(kubectl("get", "--raw", "/openid/v1/jwks"))
    issuers = parse_issuers(json.dumps([{"name": "k8s", "issuer": oidc["issuer"], "audience": "cairn",
                                         "jwks_file": str(tmp / "jwks.json")}]))
    # Cairn Store for the bundles.
    sp = free_port()
    store = subprocess.Popen([STORE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **os.environ, "CAIRN_STORE_ACCESS_KEY": "af-kind", "CAIRN_STORE_SECRET_KEY": "af-kind-secret-" + "s" * 20,
        "CAIRN_STORE_DATA": str(tmp / "store"), "CAIRN_STORE_ADDR": f"127.0.0.1:{sp}", "CAIRN_STORE_BUCKETS": "",
        "CAIRN_STORE_FSYNC": "false", "CAIRN_STORE_META": ""})
    engine = create_engine(f"sqlite:///{tmp}/cairn.db", connect_args={"check_same_thread": False})
    metadata.create_all(engine)
    tokens = TrinoTokens(engine, "k" * 40, "cairn", 3600)
    wi = WorkloadIdentity(issuers, engine, tokens)
    p = free_port()
    rt = KubernetesRuntime(api=api, token_file=str(token), ca_file=str(ca), helm=HELM, chart=CHART,
                           images={"3.3.2": IMAGE}, url_template="http://{name}.airflow.test", cairn_namespace="cairn")
    svc = AirflowDeployments(engine, lambda a, priv, u: True, None, Keys(b"k" * 32), rt,
                             DeployStore(f"http://127.0.0.1:{sp}", "airflow-deploys", "af-kind", "af-kind-secret-" + "s" * 20),
                             f"http://{HOST}:{p}", "http://cairn.test", {"host": "cairn-trino"}, b"", None,
                             TrinoOAuth(engine, tokens, "", []), wi, "k8s")
    app = FastAPI()

    def current(request: Request):          # the pods call as the pipeline user (their exchanged token)
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            who = wi.principal_of(auth[7:])
            if who:
                return Principal(urn=who, via="workload")
        return Principal(urn=ADMIN, via="dev")
    app.include_router(build_airflow_router(svc, current))
    app.include_router(build_workload_router(wi, current, None))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=p, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait_for("Cairn", lambda: socket.create_connection(("127.0.0.1", p), 1).close() or True, 30, 0.5)
    yield svc
    server.should_exit = True
    try:
        svc.delete(ADMIN, NAME)
    except Exception:  # noqa: BLE001 - already gone
        pass
    store.terminate()


def test_a_deployment_runs_on_kubernetes(cairn):
    from cairn.airflow_deploy.bundle import pack

    svc = cairn
    out = svc.create(ADMIN, {"name": NAME, "environment": "dev",
                             "resources": {"scheduler": {"cpu": 0.5, "memory_mb": 1024},
                                           "api_server": {"cpu": 0.25, "memory_mb": 1024}}})
    assert out["state"] == "creating"
    try:
        st = wait_for("the deployment's pods", lambda: svc.runtime.state({"name": NAME})["ready"] and
                      svc.runtime.state({"name": NAME}), 1200, 10)
    except AssertionError:
        print(kubectl("-n", NS, "get", "pods,clusters.postgresql.cnpg.io,jobs", "-o", "wide", check=False))
        print(kubectl("-n", NS, "get", "events", "--sort-by=.lastTimestamp", check=False)[-6000:])
        raise
    assert {c["component"] for c in st["components"]} >= {"scheduler", "api-server", "dag-processor", "triggerer"}
    svc.reconcile(NAME)
    assert svc.get(ADMIN, NAME)["state"] == "ready"
    # A DAG-only deploy: the sidecars fetch it from Cairn as the pipeline user; the DAG processor parses it.
    d = tempfile.mkdtemp()
    os.makedirs(f"{d}/dags")
    open(f"{d}/dags/from_cairn.py", "w").write(DAG)
    svc.store.ensure_bucket()
    out = svc.deploy_dags(ADMIN, NAME, pack(f"{d}/dags"), note="kind")      # parsed first in a one-shot pod
    assert out["state"] == "live" and out["checked"]["dags"] == ["from_cairn"]
    proc = kubectl("-n", NS, "get", "pods", "-l", "component=dag-processor", "-o", "jsonpath={.items[0].metadata.name}")
    wait_for("the release in the DAG processor", lambda: "from_cairn.py" in kubectl(
        "-n", NS, "exec", proc, "-c", "dag-processor", "--", "ls", "/opt/airflow/dags/current/"), 180, 5)
    sched = kubectl("-n", NS, "get", "pods", "-l", "component=scheduler", "-o", "jsonpath={.items[0].metadata.name}")
    wait_for("the DAG in Airflow", lambda: "from_cairn" in kubectl(
        "-n", NS, "exec", sched, "-c", "scheduler", "--", "airflow", "dags", "list", "-o", "plain"), 300, 10)
    # Hibernation: everything to zero; waking brings it back.
    svc.hibernate(ADMIN, NAME)
    wait_for("no pods", lambda: not kubectl("-n", NS, "get", "pods", "-l", "component in (scheduler,api-server)",
                                            "-o", "name", check=False), 300, 5)
    svc.wake(ADMIN, NAME)
    wait_for("the pods again", lambda: svc.runtime.state({"name": NAME})["ready"], 900, 10)
