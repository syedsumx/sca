"""Managed NiFi's Kubernetes runtime against a real API server (kube-apiserver and etcd; nothing runs pods here):
Cairn renders its StatefulSet, Services, NetworkPolicy and Secret of files and applies them as its own account, with
the chart's RBAC for it (templates/nifi-deployments.yaml) and its admission policy:

* a deployment gets its namespace (Pod Security ``restricted``), a ServiceAccount whose token is not mounted, a
  StatefulSet of the upstream image as NiFi's user (non-root, no privilege escalation, no capabilities, the runtime's
  seccomp profile), its keys as files from a Secret (never in the pod spec), Services and a NetworkPolicy admitting
  only Cairn's namespace;
* scaling applies the render again (the claim template kept as it was made);
* Cairn's account is refused outside its namespaces (the admission policy) and cannot read Secrets;
* deletion deletes the namespace.

Needs CAIRN_K8S_BIN_DIR (kube-apiserver, etcd, kubectl) and helm (CAIRN_HELM)."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time

import pytest

from test_workload_identity_e2e import BIN, k8s  # noqa: F401  (fixture)

HELM = os.environ.get("CAIRN_HELM", "helm")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def _helm() -> bool:
    try:
        return subprocess.run([HELM, "version"], capture_output=True).returncode == 0
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not (BIN and os.path.exists(os.path.join(BIN, "kube-apiserver"))) or not _helm(),
                                reason="needs CAIRN_K8S_BIN_DIR and helm")
ADMIN = "urn:li:corpuser:admin"
IMAGE = "registry.example.com/apache/nifi:2.12.0"


@pytest.fixture(scope="module")
def cluster(k8s, tmp_path_factory):  # noqa: F811
    """Cairn's account in namespace cairn with the chart's ClusterRole and admission policy for managed NiFi."""
    import httpx

    kubectl, d = k8s["kubectl"], k8s["dir"]
    work = tmp_path_factory.mktemp("nfk8s")
    for ns in ("cairn", "kube-other"):
        if subprocess.run([f"{BIN}/kubectl", "--kubeconfig", f"{d}/kubeconfig", "get", "namespace", ns],
                          capture_output=True).returncode != 0:
            kubectl("create", "namespace", ns)
    out = subprocess.run([HELM, "template", "t", "./helm/cairn", "--namespace", "cairn", "-s",
                          "templates/nifi-deployments.yaml", "--set", "cairn.nifiDeployments.runtime=kubernetes"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    (work / "rbac.yaml").write_text(out.stdout)
    kubectl("apply", "-f", str(work / "rbac.yaml"))
    sa = kubectl("get", "clusterrolebinding", "-o", "jsonpath={.items[?(@.roleRef.name=='t-cairn-nifi-deployments')]"
                                                       ".subjects[0].name}")
    if subprocess.run([f"{BIN}/kubectl", "--kubeconfig", f"{d}/kubeconfig", "-n", "cairn", "get", "serviceaccount", sa],
                      capture_output=True).returncode != 0:
        kubectl("-n", "cairn", "create", "serviceaccount", sa)
    token = work / "token"
    token.write_text(kubectl("-n", "cairn", "create", "token", sa, "--duration", "2h"))
    for _ in range(30):                          # the admission policy is in force once the API server has it
        r = httpx.post(f"{k8s['issuer']}/api/v1/namespaces/kube-other/services", verify=f"{d}/ca.crt", trust_env=False,
                       headers={"Authorization": f"Bearer {token.read_text().strip()}"},
                       json={"apiVersion": "v1", "kind": "Service", "metadata": {"name": "probe"},
                             "spec": {"ports": [{"port": 80}]}})
        if r.status_code in (403, 422) and "only in namespaces named" in r.text:
            break
        if r.status_code < 300:
            kubectl("-n", "kube-other", "delete", "service", "probe")
        time.sleep(1)
    else:
        pytest.fail(f"the admission policy did not refuse Cairn outside its namespaces: {r.status_code} {r.text}")
    yield {"kubectl": kubectl, "token": str(token), "ca": f"{d}/ca.crt", "api": k8s["issuer"]}


@pytest.fixture()
def svc(cluster, tmp_path):
    from sqlalchemy import create_engine

    from cairn.common.db import metadata
    from cairn.nifi.k8s_runtime import KubernetesRuntime
    from cairn.nifi.keys import Keys
    from cairn.nifi.service import NiFiService, deployments_table, flows_table

    engine = create_engine(f"sqlite:///{tmp_path}/c.db")
    metadata.create_all(engine, tables=[deployments_table, flows_table])
    rt = KubernetesRuntime(api=cluster["api"], token_file=cluster["token"], ca_file=cluster["ca"],
                           images={"2.12.0": IMAGE}, cairn_namespace="cairn")
    return NiFiService(engine, lambda a, p, u: True, None, Keys(b"n" * 32), rt,
                       "http://t-cairn-api.cairn.svc:8800", "https://cairn.test")


def items(kubectl, ns, kind):
    return json.loads(kubectl("-n", ns, "get", kind, "-o", "json"))["items"]


def test_a_deployment_is_rendered_and_applied_with_the_charts_rbac(svc, cluster):
    kubectl = cluster["kubectl"]
    out = svc.create(ADMIN, {"name": "kdev", "environment": "dev", "team": "crm",
                             "resources": {"cpu": 2, "memory_mb": 6144, "heap_mb": 4096, "storage_gb": 30}})
    assert out["state"] == "creating"                # ready once its pod is (nothing runs pods here)
    ns = "cairn-nifi-kdev"
    meta = json.loads(kubectl("get", "namespace", ns, "-o", "json"))["metadata"]
    assert meta["labels"]["cairn.io/nifi-deployment"] == "kdev"
    assert meta["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    sts = items(kubectl, ns, "statefulsets")
    assert [s["metadata"]["name"] for s in sts] == ["nifi"]
    pod = sts[0]["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert c["image"] == IMAGE and c["command"] == ["bash", "/opt/cairn/start.sh"]
    assert c["resources"]["requests"] == {"cpu": "2", "memory": "6Gi"}
    assert c["securityContext"]["allowPrivilegeEscalation"] is False and c["securityContext"]["runAsNonRoot"] is True
    assert pod["automountServiceAccountToken"] is False and pod["securityContext"]["fsGroup"] == 1000
    assert {"name": "NIFI_JVM_HEAP_MAX", "value": "4096m"} in c["env"]
    assert sts[0]["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"] == "30Gi"
    keys = svc.keys.secrets("kdev", svc._row("kdev").uid)
    assert not any(v in json.dumps(sts[0]["spec"]) for v in keys.values())       # no secret in a pod spec
    pols = items(kubectl, ns, "networkpolicies")
    assert [p["metadata"]["name"] for p in pols] == ["cairn-nifi-ingress"]
    assert pols[0]["spec"]["ingress"][0]["from"] == [
        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "cairn"}}}]
    assert {s["metadata"]["name"] for s in items(kubectl, ns, "services")} == {"nifi", "nifi-hl"}
    sec = json.loads(kubectl("-n", ns, "get", "secret", "cairn-nifi-files", "-o", "json"))
    secrets_file = base64.b64decode(sec["data"]["nifi.secrets"]).decode()
    assert keys["sensitive_key"] in secrets_file and keys["password"] not in json.dumps(sec)   # a hash, not it
    st = svc.status(ADMIN, "kdev")
    assert st["state"] == "creating" and st["runtime_state"]["ready"] is False

    # ---- scaling: the render again, the claim template as it was made -------------------------------------------
    svc._set("kdev", state="ready")                     # as if its pod had come up
    svc.update(ADMIN, "kdev", {"resources": {"cpu": 4, "memory_mb": 8192, "heap_mb": 6144, "storage_gb": 40}})
    sts = items(kubectl, ns, "statefulsets")[0]
    c = sts["spec"]["template"]["spec"]["containers"][0]
    assert c["resources"]["requests"] == {"cpu": "4", "memory": "8Gi"}
    assert {"name": "NIFI_JVM_HEAP_MAX", "value": "6144m"} in c["env"]
    assert sts["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"] == "30Gi"

    # ---- Cairn's account: nothing outside its namespaces, no reading Secrets -------------------------------------
    from cairn.databases.docker_runtime import RuntimeError_

    rt = svc.runtime
    with pytest.raises(RuntimeError_, match="403|only in namespaces"):
        rt._call("POST", "/api/v1/namespaces/kube-other/services",
                 {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "x"}, "spec": {"ports": [{"port": 80}]}})
    with pytest.raises(RuntimeError_, match="403"):
        rt._call("GET", f"/api/v1/namespaces/{ns}/secrets/cairn-nifi-files")

    # ---- deletion ------------------------------------------------------------------------------------------------
    svc.delete(ADMIN, "kdev")
    gone = json.loads(kubectl("get", "namespace", ns, "-o", "json", "--ignore-not-found") or "{}")
    assert not gone or gone["metadata"].get("deletionTimestamp")      # the namespace controller finishes it
