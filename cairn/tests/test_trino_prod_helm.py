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
