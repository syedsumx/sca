"""The data plane chart (helm/cairn-dataplane, L135) on a real cluster: the connector, installed by Helm with only the
chart's Role, enrols with its token and connects out to a control plane running on this host; a job of a workspace
that computes there runs as a Kubernetes Job in the data plane's namespace; nothing outside the release reaches the
connector (its NetworkPolicy); a revoked connector is refused and goes unready, and the workspace's jobs with it.

Needs a cluster (CAIRN_KIND_KUBECONFIG, pods reach this host at CAIRN_KIND_HOST_IP), helm, kubectl, the connector image
(CAIRN_DATAPLANE_TEST_IMAGE, from docker/dataplane.Dockerfile) and a python image (CAIRN_APPS_TEST_IMAGE) loaded."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time

import httpx
import pytest

from cairn.governance.abac import Policy, PolicyBundle

KUBECONFIG = os.environ.get("CAIRN_KIND_KUBECONFIG", "")
HOST_IP = os.environ.get("CAIRN_KIND_HOST_IP", "")
HELM = os.environ.get("CAIRN_HELM", "helm")
IMAGE = os.environ.get("CAIRN_DATAPLANE_TEST_IMAGE", "cairn/dataplane-connector:ci")
PY = os.environ.get("CAIRN_APPS_TEST_IMAGE", "cairn-test/python:ci")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NS = "cairn-dp"
ADMIN = {"X-Cairn-Actor": "urn:li:corpuser:admin"}
BOB = "urn:li:corpuser:bob"
pytestmark = pytest.mark.skipif(not (KUBECONFIG and HOST_IP and shutil.which(HELM) and shutil.which("kubectl")),
                                reason="needs a cluster (CAIRN_KIND_KUBECONFIG, CAIRN_KIND_HOST_IP), helm and kubectl")


def kubectl(*args, check=True, stdin=None, timeout=300):
    r = subprocess.run(["kubectl", "--kubeconfig", KUBECONFIG, *args], capture_output=True, text=True, input=stdin,
                       timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def cp(tmp_path_factory):
    import uvicorn

    from cairn.app import create_api_app, create_gateway_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.workspaces import WorkspaceIn
    from cairn.services import build_services

    tmp = tmp_path_factory.mktemp("cp")
    os.environ["CAIRN_MANAGED_COMPUTE"] = "false"
    try:
        s = build_services(Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-secret",
                                    dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")}),
                           graph=InMemoryGraph(), with_ai=False, with_gateway=True)
    finally:
        os.environ.pop("CAIRN_MANAGED_COMPUTE")
    s.dataplanes.offline_ms = 6000
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq",
                                                                    "value": "urn:li:corpuser:admin"}),
        Policy(id="jobs", effect="allow", actions=["RUN_JOBS"], condition={"attr": "subject.urn", "op": "eq", "value": BOB})]))
    s.workspaces.put(WorkspaceIn(id="sales", name="Sales", domains=["urn:li:domain:sales"], members=[BOB]),
                     "urn:li:corpuser:admin", True)
    api_port, gw_port = free_port(), free_port()
    servers = []
    for app, p in ((create_api_app(s), api_port), (create_gateway_app(s), gw_port)):
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=p, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        servers.append(server)
    end = time.time() + 30
    while True:
        try:
            httpx.get(f"http://127.0.0.1:{api_port}/health", timeout=2)
            break
        except httpx.HTTPError:
            assert time.time() < end
            time.sleep(0.2)
    yield {"api": f"http://127.0.0.1:{api_port}", "gw_port": gw_port, "s": s}
    for server in servers:
        server.should_exit = True


@pytest.fixture(scope="module")
def release(cp, tmp_path_factory):
    api = cp["api"]
    assert httpx.post(f"{api}/v1/dataplanes", json={"name": "kind"}, headers=ADMIN).status_code == 201
    token = httpx.post(f"{api}/v1/dataplanes/kind/enrollment-tokens", headers=ADMIN).json()["token"]
    kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=true")
    kubectl("create", "namespace", NS)
    kubectl("-n", NS, "create", "secret", "generic", "enrol", f"--from-literal=token={token}")
    repo, tag = IMAGE.rsplit(":", 1)
    values = {"image": {"repository": repo, "tag": tag, "pullPolicy": "Never"},
              "controlPlane": {"gatewayUrl": f"http://{HOST_IP}:{cp['gw_port']}"},
              "enrollment": {"existingSecret": "enrol"}, "connector": {"pollSeconds": 2},
              "trino": {"enabled": False}, "jobs": {"image": PY},
              "networkPolicy": {"gatewayCIDRs": [f"{HOST_IP}/32"], "gatewayPort": cp["gw_port"]}}
    vf = tmp_path_factory.mktemp("dp") / "values.json"
    vf.write_text(json.dumps(values))
    r = subprocess.run([HELM, "--kubeconfig", KUBECONFIG, "install", "dp", "helm/cairn-dataplane", "-n", NS, "-f", str(vf),
                        "--wait", "--timeout", "240s"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + kubectl("-n", NS, "get", "pods,events", check=False)
    end = time.time() + 120
    while httpx.get(f"{api}/v1/dataplanes/kind", headers=ADMIN).json()["tunnel"] != "up":
        assert time.time() < end, kubectl("-n", NS, "logs", "deploy/dp-cairn-dataplane-connector", check=False)
        time.sleep(1)
    yield
    subprocess.run([HELM, "--kubeconfig", KUBECONFIG, "uninstall", "dp", "-n", NS], capture_output=True)
    kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=false")


def test_a_workspace_job_runs_in_the_data_plane(cp, release):
    s, api = cp["s"], cp["api"]
    assert httpx.put(f"{api}/v1/compute/workspaces/sales", json={"compute": "customer", "dataplane": "kind"},
                     headers=ADMIN).status_code == 200
    s.jobs.save(BOB, {"name": "where", "kind": "python",
                      "code": "import os, socket\nprint(socket.gethostname(), os.environ['CAIRN_USER'], os.getuid())"})
    r = s.jobs.run_now(BOB, "where", wait=True)
    assert r["status"] == "succeeded", r
    host, user, uid = s.jobs.run_detail(BOB, "where", r["run"])["output"].split()
    assert host.startswith("cairn-dp-job-") and user == "bob" and uid == "1000"
    assert kubectl("-n", NS, "get", "jobs,secrets", "-l", "app.kubernetes.io/component=dataplane-job", "-o", "name") == ""


def test_nothing_outside_reaches_the_connector(release):
    probe = kubectl("run", "dp-probe", "-n", "default", "--rm", "-i", "--restart=Never", f"--image={PY}",
                    "--image-pull-policy=Never", "--command", "--", "python", "-c",
                    "import socket\ntry:\n socket.create_connection(('dp-cairn-dataplane-connector.cairn-dp.svc', 8181), 5)"
                    "\n print('connected')\nexcept Exception as e:\n print(type(e).__name__)", check=False, timeout=180)
    assert "connected" not in probe.splitlines(), probe


def test_a_revoked_connector_is_refused(cp, release):
    s, api = cp["s"], cp["api"]
    cid = httpx.get(f"{api}/v1/dataplanes/kind/connectors", headers=ADMIN).json()[0]["connector_id"]
    assert httpx.post(f"{api}/v1/dataplanes/kind/connectors/{cid}/revoke", headers=ADMIN).status_code == 204
    r = s.jobs.run_now(BOB, "where", wait=True)
    assert r["status"] == "failed" and "tunnel to data plane kind is down" in r["error"], r
    end = time.time() + 120
    while "false" not in kubectl("-n", NS, "get", "pods", "-l", "app.kubernetes.io/name=cairn-dataplane-connector",
                                 "-o", "jsonpath={.items[0].status.containerStatuses[0].ready}"):
        assert time.time() < end, "the revoked connector stayed ready"
        time.sleep(2)
    assert "this connector was revoked" in kubectl("-n", NS, "logs", "deploy/dp-cairn-dataplane-connector")
