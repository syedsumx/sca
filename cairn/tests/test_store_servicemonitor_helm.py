"""How Prometheus scrapes Cairn Store as the chart renders it (cairn.serviceMonitor): each store pod once, at
/-/metrics, over TLS when the store serves TLS. Needs helm (CI's helm job runs this file as a script; under
pytest it skips without helm)."""

import os
import shutil
import subprocess

import yaml

try:
    import pytest

    pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="needs helm")
except ImportError:  # run as a script (CI's helm job has helm and PyYAML, not pytest)
    pass

CHART = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "helm", "cairn")
BASE = ["cairn.store.enabled=true", "cairn.serviceMonitor.enabled=true"]
CLUSTER = ["cairn.store.tlsSecret=store-tls", "cairn.store.cluster.enabled=true", "cairn.store.metadata.secretName=store-meta"]


def render(*sets: str) -> list[dict]:
    args = ["helm", "template", "t", CHART]
    for s in [*BASE, *sets]:
        args += ["--set", s]
    out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def selects(selector: dict, labels: dict) -> bool:
    if any(labels.get(k) != v for k, v in (selector.get("matchLabels") or {}).items()):
        return False
    for e in selector.get("matchExpressions") or []:
        v, op = labels.get(e["key"]), e["operator"]
        if (op == "In" and v not in e["values"]) or (op == "NotIn" and v in e["values"]) \
                or (op == "Exists" and v is None) or (op == "DoesNotExist" and v is not None):
            return False
    return True


def scraped(docs: list[dict]) -> list[tuple[str, str, dict]]:
    """(ServiceMonitor, Service, endpoint) for every Service port a ServiceMonitor scrapes."""
    out = []
    for sm in (d for d in docs if d["kind"] == "ServiceMonitor"):
        for svc in (d for d in docs if d["kind"] == "Service"):
            if not selects(sm["spec"]["selector"], svc["metadata"].get("labels") or {}):
                continue
            ports = {p.get("name") for p in svc["spec"]["ports"]}
            out += [(sm["metadata"]["name"], svc["metadata"]["name"], ep) for ep in sm["spec"]["endpoints"] if ep["port"] in ports]
    return out


def store_targets(docs):
    return [t for t in scraped(docs) if t[1].startswith("t-cairn-store")]


def test_a_single_store_is_scraped_once_at_its_metrics_path():
    [(sm, svc, ep)] = store_targets(render())
    assert (sm, svc, ep["path"]) == ("t-cairn-store", "t-cairn-store", "/-/metrics")
    assert "scheme" not in ep and "tlsConfig" not in ep


def test_a_cluster_is_scraped_once_per_node_not_also_through_its_headless_service():
    # The headless <name>-nodes Service selects the same pods; scraping it too doubles every series.
    targets = store_targets(render(*CLUSTER))
    assert [(sm, svc) for sm, svc, _ in targets] == [("t-cairn-store", "t-cairn-store")], targets


def test_a_tls_store_is_scraped_with_the_tls_settings_given():
    # A cluster always serves TLS, from a private CA as a rule: without a CA and a name to check, every scrape fails
    # certificate verification and CairnStoreDown fires for a healthy store.
    tls = ["cairn.serviceMonitor.storeTlsConfig.ca.secret.name=store-tls", "cairn.serviceMonitor.storeTlsConfig.ca.secret.key=ca.crt",
           "cairn.serviceMonitor.storeTlsConfig.serverName=t-cairn-store.default.svc"]
    [(_, _, ep)] = store_targets(render(*CLUSTER, *tls))
    assert ep["scheme"] == "https"
    assert ep["tlsConfig"] == {"ca": {"secret": {"name": "store-tls", "key": "ca.crt"}}, "serverName": "t-cairn-store.default.svc"}
    # Without TLS on the store the setting is not used.
    [(_, _, ep)] = store_targets(render(*tls))
    assert "scheme" not in ep and "tlsConfig" not in ep


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("ok", _name)
