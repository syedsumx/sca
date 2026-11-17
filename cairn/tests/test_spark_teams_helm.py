"""The chart's Spark teams as rendered (`helm template` only): which objects each team's namespace gets. Needs helm
(CAIRN_HELM, or helm on PATH). What the API server admits is in tests/test_spark_teams_k8s.py."""

import os
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
HELM = os.environ.get("CAIRN_HELM") or shutil.which("helm")
pytestmark = pytest.mark.skipif(not HELM, reason="needs helm")
BASE = ["cairn.spark.enabled=true", "cairn.spark.namespace=spark", "cairn.workloadIdentity.issuers[0].name=k8s",
        "cairn.workloadIdentity.issuers[0].issuer=https://kubernetes.default.svc",
        "cairn.workloadIdentity.issuers[0].audience=cairn"]


def render(*sets, release="t", namespace="cairn"):
    args = [a for kv in (*BASE, *sets) for a in ("--set", kv)]
    return subprocess.run([HELM, "template", release, "helm/cairn", "--namespace", namespace,
                           "--show-only", "templates/spark.yaml", *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)


def docs(out):
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def test_a_team_namespace_made_elsewhere_is_left_alone():
    """Review finding: ``createNamespace: false`` was read through ``default true``, which takes false for unset, so
    the chart still rendered the Namespace: installing failed on a namespace that already exists, or the release
    took it over (and uninstalling deleted it, with everything in it)."""
    kinds = lambda out: [(d["kind"], d["metadata"]["name"]) for d in docs(out)]  # noqa: E731
    assert ("Namespace", "spark-finance") in kinds(render("cairn.spark.teams[0].name=finance"))
    off = kinds(render("cairn.spark.teams[0].name=finance", "cairn.spark.teams[0].createNamespace=false"))
    assert ("Namespace", "spark-finance") not in off
    assert ("ServiceAccount", "svc-spark-finance") in off and ("ResourceQuota", "spark") in off


def test_a_teams_namespace_is_its_own():
    """Review finding: a team's ``namespace`` was taken as given. Set to the release's namespace, the team's driver
    account (which creates pods) could start a pod there mounting Cairn's own Secrets or running as Cairn's service
    account, and the team's NetworkPolicy would cut Cairn's pods off from everything outside it; set to the shared
    Spark namespace or another team's, the teams were not apart (and two teams rendered the same objects)."""
    ok = docs(render("cairn.spark.teams[0].name=finance", "cairn.spark.teams[1].name=risk",
                     "cairn.spark.teams[1].namespace=risk-jobs"))
    assert {d["metadata"]["name"] for d in ok if d["kind"] == "Namespace"} == {"spark-finance", "risk-jobs"}
    for sets, why in (
            (["cairn.spark.teams[0].name=finance", "cairn.spark.teams[0].namespace=cairn"], "release"),
            (["cairn.spark.namespace=spark", "cairn.spark.teams[0].name=finance",
              "cairn.spark.teams[0].namespace=spark"], "shared"),
            (["cairn.spark.teams[0].name=finance", "cairn.spark.teams[1].name=risk",
              "cairn.spark.teams[1].namespace=spark-finance"], "another team")):
        out = render(*sets)
        assert out.returncode != 0 and "cairn.spark.teams" in out.stderr and why in out.stderr, (sets, out.stderr)
    # With no shared namespace set, the shared one is the release's: refused the same way.
    out = render("cairn.spark.teams[0].name=finance", "cairn.spark.teams[0].namespace=other", namespace="other")
    assert out.returncode != 0 and "release" in out.stderr


def test_spark_drivers_do_not_run_in_cairns_namespace():
    """Review finding: ``cairn.spark.namespace`` defaulted to the release's namespace, where the drivers' Role lets
    whatever runs in a driver (the job's own code) create, patch and delete pods, Services and ConfigMaps: delete
    Cairn's API pods, or start a pod that mounts Cairn's own Secrets (no RBAC on Secrets is needed to mount one) or
    runs as Cairn's service account. The chart now refuses it, unless the operator says so."""
    out = render("cairn.spark.namespace=")
    assert out.returncode != 0 and "cairn.spark.namespace" in out.stderr
    roles = [d for d in docs(render("cairn.spark.namespace=spark")) if d["kind"] == "Role"]
    assert roles and all(r["metadata"]["namespace"] == "spark" for r in roles)
    assert docs(render("cairn.spark.namespace=", "cairn.spark.allowReleaseNamespace=true"))


def test_a_teams_pods_take_connections_only_from_each_other():
    """Review finding: team namespaces had no NetworkPolicy, so any pod in the cluster (another team's job) could
    reach a team's driver. Spark's RPC has no sign-in here: an executor asks the driver for the application's
    settings, which hold the session's lakehouse credential and Cairn token, and block managers serve cached data."""
    out = docs(render("cairn.spark.teams[0].name=finance", "cairn.spark.teams[1].name=risk",
                      "cairn.spark.teams[1].ingressFrom[0].namespaceSelector.matchLabels.name=monitoring"))
    pols = {d["metadata"]["namespace"]: d for d in out if d["kind"] == "NetworkPolicy"}
    assert set(pols) == {"spark-finance", "spark-risk"}
    for ns, p in pols.items():
        assert p["spec"]["podSelector"] == {} and p["spec"]["policyTypes"] == ["Ingress"]
        peers = [peer for rule in p["spec"]["ingress"] for peer in rule["from"]]
        assert {"podSelector": {}} in peers                       # its own pods, in its own namespace
        assert all("namespaceSelector" not in peer or ns == "spark-risk" for peer in peers)
    assert {"namespaceSelector": {"matchLabels": {"name": "monitoring"}}} in pols["spark-risk"]["spec"]["ingress"][0]["from"]
