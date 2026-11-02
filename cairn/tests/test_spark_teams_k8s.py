"""Spark teams from the chart (cairn.spark.teams) on a real Kubernetes API server with its quota
controller: a team's pods get default requests, its quota caps what all its pods use together, a
container over the cap is refused, and its drivers' service account may manage pods in its namespace
only, and no secrets.

Needs CAIRN_K8S_BIN_DIR (kube-apiserver, kube-controller-manager, etcd, kubectl) and helm (CAIRN_HELM
or on PATH). No kubelet runs: what is checked is what the API admits."""

import json
import os
import shutil
import subprocess
import time

import pytest
import yaml

from test_workload_identity_e2e import BIN, k8s  # noqa: F401  (fixture)

HELM = os.environ.get("CAIRN_HELM") or shutil.which("helm")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pytestmark = pytest.mark.skipif(
    not (BIN and HELM and os.path.exists(os.path.join(BIN, "kube-controller-manager"))),
    reason="needs CAIRN_K8S_BIN_DIR with kube-controller-manager, and helm")
NS = "spark-finance"


@pytest.fixture(scope="module")
def quota_controller(k8s):  # noqa: F811
    d = k8s["dir"]
    p = subprocess.Popen([f"{BIN}/kube-controller-manager", f"--kubeconfig={d}/kubeconfig", "--controllers=resourcequota",
                          "--leader-elect=false", f"--cert-dir={d}/kcm", "--secure-port=0"],
                         stdout=open(f"{d}/kcm.log", "w"), stderr=subprocess.STDOUT)
    yield
    p.terminate()
    p.wait(20)


def team_objects(tmp_path):
    out = subprocess.run([HELM, "template", "t", "helm/cairn", "--set", "cairn.spark.enabled=true",
                          "--set", "cairn.workloadIdentity.issuers[0].name=k8s",
                          "--set", "cairn.workloadIdentity.issuers[0].issuer=https://kubernetes.default.svc",
                          "--set", "cairn.workloadIdentity.issuers[0].audience=cairn",
                          "--set", "cairn.workloadIdentity.issuers[0].jwks_url=https://kubernetes.default.svc/openid/v1/jwks",
                          "--set", "cairn.spark.teams[0].name=finance", "--set", "cairn.spark.teams[0].quota.cpu=4",
                          "--set", "cairn.spark.teams[0].quota.memory=16Gi", "--set", "cairn.spark.teams[0].quota.pods=3",
                          "--set", "cairn.spark.teams[0].maxCpu=2", "--show-only", "templates/spark.yaml"],
                         capture_output=True, text=True, check=True, cwd=ROOT).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    mine = [d for d in docs if d["metadata"].get("namespace") == NS or d["metadata"].get("name") == NS]
    f = tmp_path / "team.yaml"
    f.write_text(yaml.safe_dump_all(mine))
    return f, mine


def pod(name, cpu=None):
    c = {"name": "main", "image": "cairn/spark:0.1.0"}
    if cpu:
        c["resources"] = {"requests": {"cpu": cpu}}
    return json.dumps({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": NS},
                       "spec": {"serviceAccountName": "svc-spark-finance", "automountServiceAccountToken": False,
                                "containers": [c]}})


def test_a_teams_namespace_quota_and_driver_account(k8s, quota_controller, tmp_path):  # noqa: F811
    kubectl = k8s["kubectl"]
    f, mine = team_objects(tmp_path)
    assert {d["kind"] for d in mine} == {"Namespace", "ServiceAccount", "Role", "RoleBinding", "ResourceQuota", "LimitRange"}
    kubectl("apply", "-f", str(f))
    for _ in range(60):  # the quota controller fills in the quota's status before pods are admitted
        status = json.loads(kubectl("-n", NS, "get", "resourcequota", "spark", "-o", "json")).get("status") or {}
        if status.get("hard"):
            break
        time.sleep(1)
    else:
        pytest.fail(open(os.path.join(k8s["dir"], "kcm.log")).read()[-3000:])

    def create(name, cpu=None):
        (tmp_path / f"{name}.json").write_text(pod(name, cpu))
        return subprocess.run([f"{BIN}/kubectl", "--kubeconfig", f"{k8s['dir']}/kubeconfig", "create", "-f",
                               str(tmp_path / f"{name}.json")], capture_output=True, text=True)

    # A pod that states nothing gets the team's defaults (so the quota can count it).
    assert create("a").returncode == 0
    a = json.loads(kubectl("-n", NS, "get", "pod", "a", "-o", "json"))
    assert a["spec"]["containers"][0]["resources"]["requests"] == {"cpu": "1", "memory": "2Gi"}
    # Over the per-container cap: refused.
    r = create("big", "3")
    assert r.returncode != 0 and ("maximum cpu usage per Container is 2" in r.stderr or "cpu limit of 2" in r.stderr), r.stderr
    # Up to the quota, then refused: 1 + 2 cpu used of 4; one more cpu fits, the pod count does not.
    assert create("b", "2").returncode == 0
    r = create("c", "2")
    assert r.returncode != 0 and "exceeded quota" in r.stderr and "requests.cpu" in r.stderr, r.stderr
    assert create("c", "1").returncode == 0
    r = create("d", "100m")
    assert r.returncode != 0 and "exceeded quota" in r.stderr and "pods" in r.stderr, r.stderr

    # The drivers' account manages pods here, nowhere else, and no secrets.
    who = f"system:serviceaccount:{NS}:svc-spark-finance"
    can = lambda *a: subprocess.run([f"{BIN}/kubectl", "--kubeconfig", f"{k8s['dir']}/kubeconfig", "auth", "can-i",  # noqa: E731
                                     *a, "--as", who], capture_output=True, text=True).stdout.strip()
    assert can("create", "pods", "-n", NS) == "yes"
    assert can("delete", "configmaps", "-n", NS) == "yes"
    assert can("get", "secrets", "-n", NS) == "no"
    assert can("create", "pods", "-n", "default") == "no"
