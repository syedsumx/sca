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
BASE = ["cairn.spark.enabled=true", "cairn.workloadIdentity.issuers[0].name=k8s",
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
