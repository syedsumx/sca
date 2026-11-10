"""Managed Airflow's Kubernetes runtime against a real API server (kube-apiserver and etcd; nothing runs pods here):
Cairn renders the official Apache Airflow chart (pinned) with its values and applies it, with the chart's own RBAC for
Cairn's account and its admission policy:

* a deployment gets its namespace, its ServiceAccount (its pods' projected token is bound to the pipeline user and
  exchanged at Cairn), the chart's API server, scheduler, DAG processor and triggerer (as that account, with the DAG
  sync sidecar, settings from a Secret: secret values never in a pod spec), its metadata database (a CloudNativePG
  Cluster), the chart's NetworkPolicies and Cairn's own, an Ingress at its address, a migration job, no user job;
* a change re-renders and applies (a setting changes, a resource no longer rendered is pruned);
* hibernation scales everything to zero and waking applies again;
* Cairn's account is refused outside its namespaces (the admission policy), and cannot read Secrets;
* deletion deletes the namespace and the workload binding.

Needs CAIRN_K8S_BIN_DIR (kube-apiserver, etcd, kubectl), helm (CAIRN_HELM) and the Airflow chart (CAIRN_AIRFLOW_CHART,
the airflow-1.20.0.tgz release)."""

from __future__ import annotations

import base64
import json
import os
import subprocess

import pytest

from test_workload_identity_e2e import BIN, k8s  # noqa: F401  (fixture)

HELM = os.environ.get("CAIRN_HELM", "helm")
CHART = os.environ.get("CAIRN_AIRFLOW_CHART", "")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
pytestmark = pytest.mark.skipif(not (BIN and os.path.exists(os.path.join(BIN, "kube-apiserver"))) or not CHART or
                                subprocess.run([HELM, "version"], capture_output=True).returncode != 0,
                                reason="needs CAIRN_K8S_BIN_DIR, helm and CAIRN_AIRFLOW_CHART")
ADMIN = "urn:li:corpuser:admin"
IMAGE = "registry.example.com/cairn/airflow:0.1.0"
# The CloudNativePG CRD's name and version (the operator itself is the kind test's: nothing reconciles here).
CNPG_CRD = {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
            "metadata": {"name": "clusters.postgresql.cnpg.io"},
            "spec": {"group": "postgresql.cnpg.io", "scope": "Namespaced",
                     "names": {"plural": "clusters", "singular": "cluster", "kind": "Cluster", "listKind": "ClusterList"},
                     "versions": [{"name": "v1", "served": True, "storage": True, "schema": {"openAPIV3Schema": {
                         "type": "object", "x-kubernetes-preserve-unknown-fields": True}}}]}}


@pytest.fixture(scope="module")
def cluster(k8s, tmp_path_factory):  # noqa: F811
    """Cairn's account in namespace cairn with the chart's ClusterRole and admission policy for managed Airflow."""
    kubectl, d = k8s["kubectl"], k8s["dir"]
    work = tmp_path_factory.mktemp("afk8s")
    (work / "crd.json").write_text(json.dumps(CNPG_CRD))
    kubectl("apply", "-f", str(work / "crd.json"))
    kubectl("create", "namespace", "cairn")
    kubectl("create", "namespace", "kube-other")
    out = subprocess.run([HELM, "template", "t", "./helm/cairn", "--namespace", "cairn", "-s",
                          "templates/airflow-deployments.yaml", "--set", "cairn.airflowDeployments.runtime=kubernetes"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    (work / "rbac.yaml").write_text(out.stdout)
    kubectl("apply", "-f", str(work / "rbac.yaml"))
    sa = kubectl("get", "clusterrolebinding", "-o", "jsonpath={.items[?(@.roleRef.name=='t-cairn-airflow-deployments')]"
                                                       ".subjects[0].name}")
    kubectl("-n", "cairn", "create", "serviceaccount", sa)
    token = work / "token"
    token.write_text(kubectl("-n", "cairn", "create", "token", sa, "--duration", "2h"))
    import httpx
    import time

    for _ in range(30):                          # the admission policy is in force once the API server has it
        r = httpx.post(f"{k8s['issuer']}/api/v1/namespaces/kube-other/configmaps", verify=f"{d}/ca.crt", trust_env=False,
                       headers={"Authorization": f"Bearer {token.read_text().strip()}"},
                       json={"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "probe"}})
        if r.status_code in (403, 422) and "only in namespaces named" in r.text:
            break
        if r.status_code < 300:
            kubectl("-n", "kube-other", "delete", "configmap", "probe")
        time.sleep(1)
    else:
        pytest.fail(f"the admission policy did not refuse Cairn outside its namespaces: {r.status_code} {r.text}")
    yield {"kubectl": kubectl, "token": str(token), "ca": f"{d}/ca.crt", "api": k8s["issuer"], "dir": d}


@pytest.fixture()
def svc(cluster, tmp_path):
    from sqlalchemy import create_engine

    from cairn.airflow_deploy.deployments import AirflowDeployments
    from cairn.airflow_deploy.identity import Keys
    from cairn.airflow_deploy.k8s_runtime import KubernetesRuntime
    from cairn.common.db import metadata
    from cairn.dataaccess.trino_auth import TrinoTokens
    from cairn.dataaccess.trino_oauth import TrinoOAuth
    from cairn.identity.secret_scopes import SecretScopes
    from cairn.identity.workload import WorkloadIdentity, parse_issuers

    engine = create_engine(f"sqlite:///{tmp_path}/c.db")
    metadata.create_all(engine)
    os.environ["CAIRN_SECRETS_KEY"] = base64.b64encode(b"k" * 32).decode()
    try:
        scopes = SecretScopes(engine, lambda a: [], lambda a: False)
    finally:
        os.environ.pop("CAIRN_SECRETS_KEY")
    scopes.create(ADMIN, {"name": "team-secrets"})
    scopes.put(ADMIN, "team-secrets", "warehouse", "k8s-secret-value")
    scopes.set_acl(ADMIN, "team-secrets", "urn:li:corpuser:svc-af-kdev", "READ")
    tokens = TrinoTokens(engine, "k" * 40, "cairn", 3600)
    wi = WorkloadIdentity(parse_issuers(os.environ["CAIRN_WORKLOAD_ISSUERS"]), engine, tokens)
    rt = KubernetesRuntime(api=cluster["api"], token_file=cluster["token"], ca_file=cluster["ca"], helm=HELM, chart=CHART,
                           images={"3.3.2": IMAGE}, url_template="https://{name}.airflow.test",
                           ingress_class="nginx", cairn_namespace="cairn")
    s = AirflowDeployments(engine, lambda a, p, u: True, None, Keys(b"q" * 32), rt, None,
                           "http://t-cairn-api.cairn.svc:8800", "https://cairn.test", {"host": "t-cairn-trino.cairn.svc"},
                           b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n", scopes,
                           TrinoOAuth(engine, tokens, "", []), wi, "k8s")
    s.exchange = wi.exchange
    return s


def items(kubectl, ns, kind):
    return json.loads(kubectl("-n", ns, "get", kind, "-o", "json"))["items"]


def test_a_deployment_is_rendered_and_applied_with_the_charts_rbac(svc, cluster):
    kubectl = cluster["kubectl"]
    out = svc.create(ADMIN, {"name": "kdev", "environment": "dev", "roles": {"data-engineers": "User"},
                             "env": {"REPORT_REGION": "EU"},
                             "secrets": [{"env": "WAREHOUSE_PASSWORD", "scope": "team-secrets", "key": "warehouse"}],
                             "resources": {"scheduler": {"cpu": 2, "memory_mb": 3072}}})
    assert out["state"] == "creating" and out["url"] == "https://kdev.airflow.test"
    ns = "cairn-af-kdev"
    assert json.loads(kubectl("get", "namespace", ns, "-o", "json"))["metadata"]["labels"][
        "cairn.io/airflow-deployment"] == "kdev"
    deps = {d["metadata"]["name"]: d for d in items(kubectl, ns, "deployments")}
    assert {"airflow-api-server", "airflow-scheduler", "airflow-dag-processor"} <= set(deps), sorted(deps)
    sts = {x["metadata"]["name"] for x in items(kubectl, ns, "statefulsets")}
    assert "airflow-triggerer" in sts | set(deps), (sts, sorted(deps))
    sched = deps["airflow-scheduler"]["spec"]["template"]["spec"]
    assert sched["serviceAccountName"] == "airflow-cairn"
    names = [c["name"] for c in sched["containers"]]
    assert "scheduler" in names and "cairn-dag-sync" in names, names
    main = next(c for c in sched["containers"] if c["name"] == "scheduler")
    assert main["image"] == IMAGE and main["resources"]["requests"] == {"cpu": "2", "memory": "3Gi"}
    assert {"secretRef": {"name": "cairn-af-env"}} in main.get("envFrom", [])
    pod_specs = json.dumps([d["spec"]["template"] for d in deps.values()])
    keys = svc.keys.secrets("kdev", svc._row("kdev").uid)
    assert "k8s-secret-value" not in pod_specs and keys["fernet_key"] not in pod_specs and keys["db_password"] not in pod_specs
    vols = {v["name"]: v for v in sched["volumes"]}
    assert vols["cairn-identity"]["projected"]["sources"][0]["serviceAccountToken"]["audience"] == "cairn"
    # The settings Secret holds the resolved secret reference and the deployment's wiring.
    sec = json.loads(kubectl("-n", ns, "get", "secret", "cairn-af-env", "-o", "json"))
    env = {k: base64.b64decode(v).decode() for k, v in sec["data"].items()}
    assert env["WAREHOUSE_PASSWORD"] == "k8s-secret-value" and env["AIRFLOW__CORE__EXECUTOR"] == "KubernetesExecutor"
    assert env["CAIRN_AIRFLOW_CLIENT_ID"] == "airflow-kdev" and env["AIRFLOW__API__BASE_URL"] == "https://kdev.airflow.test"
    assert json.loads(env["CAIRN_AIRFLOW_ROLES"]) == {"data-engineers": ["User"]}
    assert env["CAIRN_CA_FILE"] == "/opt/cairn/ca/ca.crt"
    assert env["AIRFLOW__CORE__EXECUTION_API_SERVER_URL"] == f"http://airflow-api-server.{ns}.svc:8080/execution/"
    # Its database, network policies, ingress, migration job; no user job (no local admin).
    assert [c["metadata"]["name"] for c in items(kubectl, ns, "clusters.postgresql.cnpg.io")] == ["cairn-af-db"]
    pols = {p["metadata"]["name"] for p in items(kubectl, ns, "networkpolicies")}
    assert "cairn-af-ingress" in pols and len(pols) >= 4, pols
    ing = items(kubectl, ns, "ingresses")
    assert ing and ing[0]["spec"]["rules"][0]["host"] == "kdev.airflow.test"
    jobs = [j["metadata"]["name"] for j in items(kubectl, ns, "jobs")]
    assert any("migrations" in j for j in jobs) and not any("create-user" in j for j in jobs), jobs
    # Workload identity: the account's projected token is exchanged for the pipeline user's Cairn token.
    tok = kubectl("-n", ns, "create", "token", "airflow-cairn", "--audience", "cairn")
    assert svc.exchange(tok)["user"] == "svc-af-kdev"
    kubectl("-n", "cairn", "create", "serviceaccount", "someone-else")
    other = kubectl("-n", "cairn", "create", "token", "someone-else", "--audience", "cairn")
    from cairn.identity.workload import WorkloadRefused

    with pytest.raises(WorkloadRefused):
        svc.exchange(other)
    # Status before pods run: the components are there, not ready.
    st = svc.status(ADMIN, "kdev")
    assert st["state"] == "creating" and st["runtime_state"]["ready"] is False

    # ---- a change: re-rendered and applied, what is no longer rendered pruned --------------------------------
    svc._set("kdev", state="ready")                     # as if its pods had come up
    svc.update(ADMIN, "kdev", {"env": {"REPORT_REGION": "US"}, "executor": "LocalExecutor"})
    sec = json.loads(kubectl("-n", ns, "get", "secret", "cairn-af-env", "-o", "json"))
    assert base64.b64decode(sec["data"]["REPORT_REGION"]).decode() == "US"
    assert base64.b64decode(sec["data"]["AIRFLOW__CORE__EXECUTOR"]).decode() == "LocalExecutor"

    # ---- hibernation and waking -------------------------------------------------------------------------------
    svc.hibernate(ADMIN, "kdev")
    reps = [w["spec"]["replicas"] for w in items(kubectl, ns, "deployments") + items(kubectl, ns, "statefulsets")]
    assert reps and all(r == 0 for r in reps), reps
    db = items(kubectl, ns, "clusters.postgresql.cnpg.io")[0]
    assert db["metadata"]["annotations"]["cnpg.io/hibernation"] == "on"
    svc.wake(ADMIN, "kdev")
    assert all(w["spec"]["replicas"] >= 1 for w in items(kubectl, ns, "deployments"))
    assert items(kubectl, ns, "clusters.postgresql.cnpg.io")[0]["metadata"]["annotations"]["cnpg.io/hibernation"] == "off"

    # ---- Cairn's account: nothing outside its namespaces, no reading Secrets ---------------------------------
    from cairn.databases.docker_runtime import RuntimeError_

    rt = svc.runtime
    with pytest.raises(RuntimeError_, match="403|only in namespaces"):
        rt._call("POST", "/api/v1/namespaces/kube-other/configmaps",
                 {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "x"}})
    with pytest.raises(RuntimeError_, match="403"):
        rt._call("GET", f"/api/v1/namespaces/{ns}/secrets/cairn-af-env")

    # ---- a secret it can no longer read: no change applied ------------------------------------------------------
    svc.secret_scopes.set_acl(ADMIN, "team-secrets", "urn:li:corpuser:svc-af-kdev", None)
    from cairn.airflow_deploy.deployments import DeploymentError

    with pytest.raises(DeploymentError, match="team-secrets/warehouse"):
        svc.update(ADMIN, "kdev", {"env": {"REPORT_REGION": "APAC"}})
    sec = json.loads(kubectl("-n", ns, "get", "secret", "cairn-af-env", "-o", "json"))
    assert base64.b64decode(sec["data"]["REPORT_REGION"]).decode() == "US"

    # ---- deletion ---------------------------------------------------------------------------------------------
    svc.delete(ADMIN, "kdev")
    gone = json.loads(kubectl("get", "namespace", ns, "-o", "json", "--ignore-not-found") or "{}")
    assert not gone or gone["metadata"].get("deletionTimestamp")      # the namespace controller finishes it
    assert not [b for b in svc.workload.bindings() if b["user"] == "urn:li:corpuser:svc-af-kdev"]
