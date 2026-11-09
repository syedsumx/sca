"""Egress rules on Kubernetes (L136), on a real cluster: a job's pod gets its workspace's NetworkPolicy (made by Cairn
with only the chart's Role) and Cairn's egress proxy. It reaches the host its rules allow through the proxy and the
address a CIDR rule allows directly; the other host is refused by the proxy (and logged), the other address is
dropped by the NetworkPolicy.

kind's default CNI (kindnet, kind v0.24 and later) enforces NetworkPolicy; the test fails, saying so, on a cluster
whose CNI does not.

Needs a cluster (CAIRN_KIND_KUBECONFIG), helm, kubectl, and a python image loaded into it (CAIRN_EGRESS_TEST_IMAGE)."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time

import pytest
import yaml

from cairn.common.db import get_engine, init_db
from cairn.network.egress import Egress, EgressRule
from cairn.pipelines.jobs import Jobs, KubeRunner

KUBECONFIG = os.environ.get("CAIRN_KIND_KUBECONFIG", "")
HELM = os.environ.get("CAIRN_HELM", "helm")
IMAGE = os.environ.get("CAIRN_EGRESS_TEST_IMAGE", "cairn-test/python:ci")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS, TARGETS, CP = "cairn-egress-jobs", "cairn-egress-targets", "cairn-egress-cp"
KEY = "egress-kind-key-" + "z" * 24
OWNER = "urn:li:corpuser:ann"
pytestmark = pytest.mark.skipif(not (KUBECONFIG and shutil.which(HELM) and shutil.which("kubectl")),
                                reason="needs a cluster (CAIRN_KIND_KUBECONFIG), helm and kubectl")


def kubectl(*args, check=True, stdin=None):
    r = subprocess.run(["kubectl", "--kubeconfig", KUBECONFIG, *args], capture_output=True, text=True, input=stdin)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def web(name: str) -> str:
    return yaml.safe_dump_all([
        {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": name, "namespace": TARGETS, "labels": {"app": name}},
         "spec": {"containers": [{"name": "web", "image": IMAGE, "imagePullPolicy": "Never",
                                  "command": ["python", "-m", "http.server", "8000"]}]}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": TARGETS},
         "spec": {"selector": {"app": name}, "ports": [{"port": 8000}]}}])


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("egress-kind")
    for ns in (JOBS, TARGETS, CP):
        kubectl("delete", "namespace", ns, "--ignore-not-found", "--wait=true")
        kubectl("create", "namespace", ns)
    kubectl("create", "namespace", "cairn", check=False)
    kubectl("-n", "cairn", "create", "serviceaccount", "t-cairn", check=False)
    # The chart's Role for jobs, with egress enforced (it may then manage NetworkPolicies there).
    out = subprocess.run([HELM, "template", "t", "helm/cairn", "--namespace", "cairn", "-s", "templates/jobs-kubernetes.yaml",
                          "--set", "cairn.jobs.runtime=kubernetes", "--set", f"cairn.jobs.namespace={JOBS}",
                          "--set", "cairn.egress.enforce=true", "--set", "cairn.egress.keySecret.name=k"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    kubectl("apply", "-f", "-", stdin=out.stdout)
    kubectl("apply", "-f", "-", stdin=web("allowed") + "---\n" + web("blocked"))
    # Cairn's egress proxy in "Cairn's namespace": its own module, run as a script from a ConfigMap, the key a Secret.
    kubectl("-n", CP, "create", "configmap", "egress-proxy", f"--from-file=proxy.py={ROOT}/src/cairn/network/proxy.py")
    (tmp / "key").write_text(KEY)
    kubectl("-n", CP, "create", "secret", "generic", "egress-key", f"--from-file=key={tmp}/key")
    proxy = [
        {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "egress-proxy", "namespace": CP, "labels": {"app": "egress-proxy"}},
         "spec": {"securityContext": {"runAsNonRoot": True, "runAsUser": 65534},
                  "containers": [{"name": "proxy", "image": IMAGE, "imagePullPolicy": "Never",
                                  "command": ["python", "/opt/proxy/proxy.py", "--listen", "0.0.0.0:3128",
                                              "--key-file", "/etc/egress/key"],
                                  "env": [{"name": "CAIRN_EGRESS_ALLOW_PRIVATE", "value": "true"}],
                                  "volumeMounts": [{"name": "code", "mountPath": "/opt/proxy"},
                                                   {"name": "key", "mountPath": "/etc/egress"}]}],
                  "volumes": [{"name": "code", "configMap": {"name": "egress-proxy"}},
                              {"name": "key", "secret": {"secretName": "egress-key"}}]}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "egress-proxy", "namespace": CP},
         "spec": {"selector": {"app": "egress-proxy"}, "ports": [{"port": 3128}]}}]
    kubectl("apply", "-f", "-", stdin=yaml.safe_dump_all(proxy))
    for ns, pod in ((TARGETS, "allowed"), (TARGETS, "blocked"), (CP, "egress-proxy")):
        kubectl("-n", ns, "wait", "--for=condition=Ready", f"pod/{pod}", "--timeout=180s")
    ips = {p: kubectl("-n", TARGETS, "get", "pod", p, "-o", "jsonpath={.status.podIP}") for p in ("allowed", "blocked")}
    token = tmp / "token"
    token.write_text(kubectl("-n", "cairn", "create", "token", "t-cairn", "--duration", "1h"))
    cfg = yaml.safe_load(open(KUBECONFIG))["clusters"][0]["cluster"]
    ca = tmp / "ca.crt"
    ca.write_bytes(base64.b64decode(cfg["certificate-authority-data"]))
    yield {"ips": ips, "server": cfg["server"], "token": str(token), "ca": str(ca), "tmp": tmp}
    for ns in (JOBS, TARGETS, CP):
        kubectl("delete", "namespace", ns, "--ignore-not-found", "--wait=false")


CODE = """
import json, os, socket, urllib.request, urllib.error
p = json.loads(os.environ["CAIRN_JOB_PARAMS"])
res = {}
for k in ("allowed", "blocked"):
    try:
        res[k] = urllib.request.urlopen(f"http://{k}.cairn-egress-targets.svc.cluster.local:8000/", timeout=15).status
    except urllib.error.HTTPError as e:
        res[k] = e.code
    except Exception as e:
        res[k] = type(e).__name__
for k in ("allowed", "blocked"):
    try:
        socket.create_connection((p[k], 8000), 5).close()
        res["direct_" + k] = "connected"
    except Exception as e:
        res["direct_" + k] = type(e).__name__
print(json.dumps(res))
"""


def test_a_jobs_pod_reaches_only_what_its_rules_allow(cluster, tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/e.db")
    init_db(engine)
    egress = Egress(engine, enforce=True, proxy=f"http://egress-proxy.{CP}.svc.cluster.local:3128", key=KEY.encode(),
                    cairn_namespace=CP)
    admin = "urn:li:corpuser:admin"
    egress.put(EgressRule(name="allowed-host", host=f"allowed.{TARGETS}.svc.cluster.local", ports=[8000]), admin)
    egress.put(EgressRule(name="allowed-ip", cidr=f"{cluster['ips']['allowed']}/32", ports=[8000]), admin)
    jobs = Jobs(engine, KubeRunner(JOBS, IMAGE, cluster["server"], cluster["token"], cluster["ca"], poll=1.0),
                lambda a: True, lambda a: False)
    jobs.egress = egress
    status, output, log, error = jobs.contained(OWNER, "python", CODE, cluster["ips"], 180, "egress", "kindrun1")
    assert status == "succeeded", (error, log)
    res = json.loads(output.strip().splitlines()[-1])
    if res["direct_blocked"] == "connected":
        cni = kubectl("-n", "kube-system", "logs", "-l", "app=kindnet", "--tail=80", check=False)
        raise AssertionError(f"the cluster's CNI does not enforce NetworkPolicy: {res}\nkindnet:\n{cni}")
    assert res == {"allowed": 200, "blocked": 403, "direct_allowed": "connected", "direct_blocked": res["direct_blocked"]}, res
    pol = json.loads(kubectl("-n", JOBS, "get", "networkpolicy", "cairn-egress-all", "-o", "json"))
    assert pol["spec"]["podSelector"]["matchLabels"] == {"cairn.io/user-code": "true", "cairn.io/workspace": "all"}
    assert {"ipBlock": {"cidr": f"{cluster['ips']['allowed']}/32"}} in [t for e in pol["spec"]["egress"] for t in e["to"]]
    end = time.time() + 30
    while True:
        logs = kubectl("-n", CP, "logs", "egress-proxy")
        denied = [json.loads(ln) for ln in logs.splitlines() if '"egress.denied"' in ln]
        if any(d["run"] == "kindrun1" and d["target"].startswith("blocked.") for d in denied):
            break
        assert time.time() < end, logs
        time.sleep(1)
