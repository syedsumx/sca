"""Managed Airflow and managed NiFi on Kubernetes act as accounts of their own, never as the API's.

Their admission policies refuse every change outside their namespace prefix (``cairn-af-``, ``cairn-nifi-``). The
API's account is shared by managed Postgres, jobs, data apps, agent apps, model serving and Trino federation, so a
policy that matched it would refuse all of those (and with both on, everything). Rendered with ``helm template`` for
each combination (Airflow on Kubernetes on/off x NiFi on Kubernetes on/off, every other Kubernetes runtime on):

* each policy matches its runtime's own ServiceAccount, never the API's;
* each runtime's ClusterRole is bound to that account alone, and the other features' Roles stay bound to the API's;
* the API and scheduler pods mount that account's token and point the runtime at it (and nothing when it is off).

Needs helm (CAIRN_HELM, or helm on PATH)."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
HELM = os.environ.get("CAIRN_HELM") or shutil.which("helm")
pytestmark = pytest.mark.skipif(not HELM, reason="needs helm (CAIRN_HELM, or helm on PATH)")
NS = "data"
API_SA = "t-cairn"
OTHERS = ["cairn.scheduler.enabled=true", "cairn.databases.instances.runtime=kubernetes", "cairn.jobs.runtime=kubernetes",
          "cairn.apps.kubernetes.enabled=true", "cairn.agentApps.runtime=kubernetes", "cairn.mlflow.enabled=true",
          "cairn.mlflow.database.secretName=mlflow-db", "cairn.mlflow.artifacts.credentialsSecret=mlflow-art",
          "cairn.mlflow.servingKubernetes.enabled=true", "cairn.store.enabled=true", "cairn.catalog.enabled=true",
          "cairn.trino.enabled=true", "cairn.trino.tls.secretName=trino-tls", "cairn.trino.externalCatalogs.enabled=true",
          "cairn.trino.externalCatalogs.snowflake.enabled=true"]
# The other features' bindings (all on in every render), which must stay the API's.
OTHER_BINDINGS = {"t-cairn-postgres", "t-cairn-jobs", "t-cairn-apps", "t-cairn-agent-apps", "t-cairn-models",
                  "t-cairn-trino-federation"}
RUNTIMES = {"airflow": ("cairn.airflowDeployments.runtime", "t-cairn-airflow-deployments", "CAIRN_AIRFLOW_K8S"),
            "nifi": ("cairn.nifiDeployments.runtime", "t-cairn-nifi-deployments", "CAIRN_NIFI_K8S")}


def render(airflow: bool, nifi: bool) -> list[dict]:
    sets = [*OTHERS, f"cairn.airflowDeployments.runtime={'kubernetes' if airflow else ''}",
            f"cairn.nifiDeployments.runtime={'kubernetes' if nifi else ''}"]
    out = subprocess.run([HELM, "template", "t", "helm/cairn", "--namespace", NS,
                          *[a for kv in sets for a in ("--set", kv)]], cwd=ROOT, capture_output=True, text=True,
                         timeout=300)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def username(sa: str) -> str:
    return f"system:serviceaccount:{NS}:{sa}"


@pytest.mark.parametrize("airflow,nifi", [(True, True), (True, False), (False, True), (False, False)])
def test_each_runtime_acts_as_its_own_account_and_the_apis_stays_free(airflow, nifi):
    docs = render(airflow, nifi)
    by = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    on = {"airflow": airflow, "nifi": nifi}

    policies = [d for d in docs if d["kind"] == "ValidatingAdmissionPolicy"]
    for d in policies:                           # no policy of the chart's holds the API's account to anything
        conds = " ".join(c["expression"] for c in d["spec"].get("matchConditions") or [])
        assert f'"{username(API_SA)}"' not in conds, (d["metadata"]["name"], conds)
        assert d["spec"].get("matchConditions"), f"{d['metadata']['name']} matches every user"

    for key, (_, sa, _env) in RUNTIMES.items():
        pol = by.get(("ValidatingAdmissionPolicy", sa))
        crb = by.get(("ClusterRoleBinding", sa))
        if not on[key]:
            assert pol is None and crb is None and ("ServiceAccount", sa) not in by
            continue
        # Its own account and the token the runtime signs in with.
        assert by[("ServiceAccount", sa)]["metadata"]["namespace"] == NS
        tok = by[("Secret", f"{sa}-token")]
        assert tok["type"] == "kubernetes.io/service-account-token"
        assert tok["metadata"]["annotations"]["kubernetes.io/service-account.name"] == sa
        # The policy matches that account only.
        assert [c["expression"] for c in pol["spec"]["matchConditions"]] == [
            f'request.userInfo.username == "{username(sa)}"']
        assert by[("ValidatingAdmissionPolicyBinding", sa)]["spec"]["policyName"] == sa
        # Its RBAC is bound to that account alone.
        assert crb["roleRef"]["name"] == sa
        assert crb["subjects"] == [{"kind": "ServiceAccount", "name": sa, "namespace": NS}]

    # The other features keep the API's account.
    rbs = {d["metadata"]["name"]: d for d in docs if d["kind"] == "RoleBinding"}
    assert OTHER_BINDINGS <= set(rbs), sorted(rbs)
    for name in OTHER_BINDINGS:
        assert [s["name"] for s in rbs[name]["subjects"]] == [API_SA], (name, rbs[name]["subjects"])
    for d in docs:                               # and only the runtimes' ClusterRoleBindings name their accounts
        if d["kind"] in ("RoleBinding", "ClusterRoleBinding") and d["metadata"]["name"] not in (
                RUNTIMES["airflow"][1], RUNTIMES["nifi"][1]):
            assert not {s["name"] for s in d.get("subjects") or []} & {RUNTIMES["airflow"][1], RUNTIMES["nifi"][1]}

    # The API and the scheduler (both run deployments) mount each runtime's token and point it there.
    for comp in ("t-cairn-api", "t-cairn-scheduler"):
        pod = by[("Deployment", comp)]["spec"]["template"]["spec"]
        assert pod["serviceAccountName"] == API_SA and pod["automountServiceAccountToken"] is False
        c = pod["containers"][0]
        env = {e["name"]: e.get("value") for e in c.get("env") or []}
        vols = {v["name"]: v for v in pod.get("volumes") or []}
        mounts = {m["name"]: m for m in c.get("volumeMounts") or []}
        for key, (_, sa, prefix) in RUNTIMES.items():
            vol = f"{key}-k8s-token"
            if not on[key]:
                assert vol not in vols and f"{prefix}_TOKEN_FILE" not in env
                continue
            src = vols[vol]["projected"]["sources"]
            assert src == [{"secret": {"name": f"{sa}-token", "items": [{"key": "token", "path": "token"},
                                                                         {"key": "ca.crt", "path": "ca.crt"}]}}]
            assert mounts[vol]["readOnly"] is True
            assert env[f"{prefix}_TOKEN_FILE"] == f"{mounts[vol]['mountPath']}/token"
            assert env[f"{prefix}_CA_FILE"] == f"{mounts[vol]['mountPath']}/ca.crt"


def test_the_chart_lints_with_both_runtimes_on():
    sets = [*OTHERS, "cairn.airflowDeployments.runtime=kubernetes", "cairn.nifiDeployments.runtime=kubernetes"]
    out = subprocess.run([HELM, "lint", "helm/cairn", *[a for kv in sets for a in ("--set", kv)]], cwd=ROOT,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr
