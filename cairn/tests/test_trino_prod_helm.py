"""The chart's Trino production settings (``cairn.trino.resourceGroups``, ``autoscaling``, ``gracefulShutdownSeconds``),
as rendered by ``helm template``. ``tests/test_trino_helm.py`` runs the same render in Docker.

* The source a client sends (``X-Trino-Source``) is its own choice: a resource group picked by source is still
  split per person, so naming another source never takes a person past a per-person limit.

Needs helm (CAIRN_HELM, or helm on PATH)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
HELM = os.environ.get("CAIRN_HELM") or shutil.which("helm")
pytestmark = pytest.mark.skipif(not HELM, reason="needs helm (CAIRN_HELM, or helm on PATH)")
BASE = ["cairn.store.enabled=true", "cairn.catalog.enabled=true", "cairn.trino.enabled=true",
        "cairn.trino.tls.secretName=trino-tls", "cairn.trino.workers=2"]


def helm(*sets: str):
    return subprocess.run([HELM, "template", "t", "helm/cairn", *[a for kv in (*BASE, *sets) for a in ("--set", kv)]],
                          cwd=ROOT, capture_output=True, text=True, timeout=300)


def docs(*sets: str) -> list[dict]:
    out = helm(*sets)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def resource_groups() -> dict:
    cm = next(d for d in docs() if d["kind"] == "ConfigMap" and "resource-groups.json" in (d.get("data") or {}))
    return json.loads(cm["data"]["resource-groups.json"])


def test_compose_runs_the_chart_resource_groups():
    with open(os.path.join(ROOT, "deploy", "trino", "resource-groups.json")) as f:
        assert json.load(f) == resource_groups()


def test_every_selected_group_is_split_per_person():
    """A client names its own source, so a group picked by source must not be shared by everyone who claims
    it: each selector leads to a per-person group under a bounded parent."""
    rg = resource_groups()
    groups = {}

    def walk(gs, prefix=""):
        for g in gs:
            groups[prefix + g["name"]] = g
            walk(g.get("subGroups", []), prefix + g["name"] + ".")

    walk(rg["rootGroups"])
    for s in rg["selectors"]:
        assert s["group"].endswith(".${USER}"), s
        leaf, parent = groups[s["group"]], groups[s["group"].rsplit(".", 1)[0]]
        assert leaf["hardConcurrencyLimit"] < parent["hardConcurrencyLimit"], s
        assert leaf["maxQueued"] < parent["maxQueued"], s
    assert "source" not in rg["selectors"][-1]               # whatever the source, a query lands in a group


AS = ("cairn.trino.autoscaling.enabled=true",)


@pytest.mark.parametrize("bad, message", [
    ("cairn.trino.autoscaling.minWorkers=0", "minWorkers"),                     # minReplicas 0: refused by the API
    ("cairn.trino.autoscaling.targetCPUUtilizationPercentage=0", "targetCPUUtilizationPercentage"),
    ("cairn.trino.resources.requests.cpu=null", "requests.cpu"),                # no CPU request: the HPA never scales
    ("cairn.trino.gracefulShutdownSeconds=0", "gracefulShutdownSeconds"),
    ("cairn.trino.gracefulShutdownSeconds=-5", "gracefulShutdownSeconds"),
    ("cairn.trino.gracefulShutdownSeconds=2m", "gracefulShutdownSeconds"),      # rendered 2ms, and a 30 s grace period
])
def test_autoscaling_and_draining_values_are_checked(bad, message):
    """A value that renders a manifest the API server refuses, or a worker that cannot drain, fails the render."""
    out = helm(*AS, bad)
    assert out.returncode != 0 and message in out.stderr, out.stderr


@pytest.mark.parametrize("bad", ["cairn.trino.resourceGroups.selectors=null", "cairn.trino.resourceGroups.rootGroups=null"])
def test_resource_groups_need_groups_and_selectors(bad):
    """Trino does not start with a resource-groups file without groups or selectors."""
    out = helm(bad)
    assert out.returncode != 0 and "resourceGroups" in out.stderr, out.stderr
    assert "resource-groups.json" not in "".join(
        str(d.get("data")) for d in docs("cairn.trino.resourceGroups=null") if d["kind"] == "ConfigMap")


def test_a_long_drain_still_renders_a_valid_autoscaler():
    """The HPA's scale-down period is at most 1800 seconds (the API refuses more); a long drain is capped there."""
    hpa = next(d for d in docs(*AS, "cairn.trino.gracefulShutdownSeconds=1200")
               if d["kind"] == "HorizontalPodAutoscaler" and d["metadata"]["name"].endswith("trino-worker"))
    assert [p["periodSeconds"] for p in hpa["spec"]["behavior"]["scaleDown"]["policies"]] == [1800]
    assert hpa["spec"]["minReplicas"] >= 1
    dep = next(d for d in docs(*AS, "cairn.trino.gracefulShutdownSeconds=1200")
               if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("trino-worker"))
    assert dep["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] == 2430
    assert "replicas" not in dep["spec"]


def test_only_trino_nodes_reach_the_workers():
    """A worker has no sign-in of its own: its drain rule trusts the ``X-Trino-User`` header. So only the
    coordinator and the other workers reach it, not the clients allowed to reach the coordinator."""
    client = {"namespaceSelector": {"matchLabels": {"name": "bi"}}}
    pols = [d for d in docs("cairn.networkPolicy.enabled=true", "cairn.networkPolicy.trinoIngressFrom[0]."
                            "namespaceSelector.matchLabels.name=bi") if d["kind"] == "NetworkPolicy"]

    def selects(pol, component):
        sel = pol["spec"]["podSelector"]
        if sel.get("matchLabels", {}).get("app.kubernetes.io/component") == component:
            return True
        return any(e["key"] == "app.kubernetes.io/component" and component in e["values"]
                   for e in sel.get("matchExpressions", []))

    workers = [p for p in pols if selects(p, "trino-worker")]
    assert len(workers) == 1
    sources = [s for rule in workers[0]["spec"]["ingress"] for s in rule["from"]]
    assert client not in sources
    assert {s["podSelector"]["matchLabels"]["app.kubernetes.io/component"] for s in sources} == {"trino", "trino-worker"}
    coordinator = [p for p in pols if selects(p, "trino")]
    assert len(coordinator) == 1 and client in coordinator[0]["spec"]["ingress"][0]["from"]


def test_the_tls_private_key_is_mounted_owner_only():
    # The Secret holding Trino's TLS private key is read by the init container that builds the keystore; with no
    # defaultMode it was mounted 0644, readable by every user in the container. 0440 keeps it readable by the pod's
    # fsGroup, which the non-root Trino user (1000) relies on.
    vols = [v for d in docs() if d.get("kind") in ("Deployment", "StatefulSet")
            for v in d["spec"]["template"]["spec"].get("volumes", []) if v.get("name") == "tls-src"]
    assert vols, "no tls-src volume rendered"
    for v in vols:
        assert v["secret"].get("defaultMode") == 0o440, v
