"""A Cairn Store cluster as the chart renders it (cairn.store.cluster): voluntary disruptions (node drains,
upgrades) take one store node at a time, and settings the store would refuse at start are refused at render time.
Needs helm (CI's helm job runs it with pytest; it skips without helm)."""

import os
import shutil
import subprocess

import pytest
import yaml

pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="needs helm")

CHART = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "helm", "cairn")
CLUSTER = ["cairn.store.enabled=true", "cairn.store.tlsSecret=store-tls", "cairn.store.cluster.enabled=true",
           "cairn.store.metadata.secretName=store-meta"]


def helm(*sets: str) -> subprocess.CompletedProcess:
    args = ["helm", "template", "t", CHART]
    for s in sets:
        args += ["--set", s]
    return subprocess.run(args, capture_output=True, text=True)


def render(*sets: str) -> list[dict]:
    p = helm(*sets)
    assert p.returncode == 0, p.stderr
    return [d for d in yaml.safe_load_all(p.stdout) if d]


def store_pdbs(docs):
    return [d for d in docs if d["kind"] == "PodDisruptionBudget" and d["metadata"]["name"].startswith("t-cairn-store")]


def test_a_cluster_loses_at_most_one_node_to_voluntary_disruptions():
    # Without a budget a drain of a Kubernetes node (or several at once) can evict every store node holding a blob's
    # copies: reads of it fail, and writes fail with 503 while fewer nodes than copies are up.
    docs = render(*CLUSTER, "cairn.store.cluster.replicas=4")
    [pdb] = store_pdbs(docs)
    assert pdb["spec"] == {"maxUnavailable": 1, "selector": pdb["spec"]["selector"]}
    [sts] = [d for d in docs if d["kind"] == "StatefulSet" and d["metadata"]["name"] == "t-cairn-store"]
    labels = sts["spec"]["template"]["metadata"]["labels"]
    assert all(labels.get(k) == v for k, v in pdb["spec"]["selector"]["matchLabels"].items())
    # A single store has no budget: it would block every drain of its node.
    assert store_pdbs(render("cairn.store.enabled=true")) == []


@pytest.mark.parametrize("bad, message", [
    ("cairn.store.cluster.deadAfterSeconds=10", "deadAfterSeconds"),
    ("cairn.store.cluster.spread=host", "spread"),
    ("cairn.store.cluster.copies=0", "copies"),
])
def test_settings_the_store_refuses_fail_the_render(bad, message):
    # The store would refuse to start with these (a crash loop on every node); the chart says so up front.
    p = helm(*CLUSTER, bad)
    assert p.returncode != 0 and message in p.stderr, p.stderr
