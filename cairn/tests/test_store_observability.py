"""The Cairn Store dashboard and alert rules, checked without a running store: every metric they use is one the
store's source exports, and the dashboard does not add up cluster-wide gauges once per node. Runs under pytest
or as a script."""

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OBS = os.path.join(ROOT, "deploy", "observability")
STORE_SRC = os.path.join(ROOT, "store", "src")

# Gauges a store computes from its index. In a cluster every node shares one index (PostgreSQL), so each node
# reports the same cluster-wide value: adding them up across nodes multiplies it by the number of nodes.
INDEX_GAUGES = ("objects", "stored_bytes", "noncurrent_versions", "multipart_uploads", "buckets", "events_pending",
                "bucket_bytes", "bucket_objects")


def dashboard_exprs() -> list[str]:
    dash = json.load(open(os.path.join(OBS, "cairn-store.json")))
    return [t["expr"] for p in dash["panels"] for t in p.get("targets", [])]


def store_source() -> str:
    out = []
    for d, _, files in os.walk(STORE_SRC):
        out += [open(os.path.join(d, f)).read() for f in files if f.endswith(".rs")]
    return "\n".join(out)


def _matches(parts: list[str], name: str, literals: set[str]) -> bool:
    """parts alternate fixed text and placeholders (odd indexes); each placeholder must be a string literal."""
    if not parts:
        return name == ""
    head, rest = parts[0], parts[1:]
    if not name.startswith(head):
        return False
    name = name[len(head):]
    if not rest:
        return name == ""
    return any(name[:i] in literals and _matches(rest[1:], name[i:], literals) for i in range(1, len(name) + 1))


def exported(name: str, src: str) -> bool:
    """Whether the store writes `# TYPE <name>`, directly or from a loop over string literals
    (`cairn_store_tier_{name}_total` for ("transitions", ...))."""
    literals = set(re.findall(r'"([a-z_]+)"', src))
    for t in re.findall(r"# TYPE (cairn_store_[a-z_{}]+) (?:counter|gauge)", src):
        if t == name or ("{" in t and _matches(re.split(r"\{([a-z]+)\}", t), name, literals)):
            return True
    return False


def test_dashboard_and_alerts_only_use_metrics_the_store_exports():
    src = store_source()
    used = set(re.findall(r"\bcairn_store_[a-z_]+", " ".join(dashboard_exprs())))
    used |= set(re.findall(r"\bcairn_store_[a-z_]+", open(os.path.join(OBS, "cairn-store-alerts.yaml")).read()))
    assert len(used) > 30
    missing = sorted(n for n in used if not exported(n, src))
    assert not missing, f"the dashboard or alerts use metrics the store does not export: {missing}"
    # The check itself tells a typo apart.
    assert not exported("cairn_store_tier_transition_failure_total", src)
    assert not exported("cairn_store_request_total", src)


def test_dashboard_does_not_add_up_cluster_wide_gauges_per_node():
    summed = [e for e in dashboard_exprs() for g in INDEX_GAUGES
              if re.search(rf"\bsum\s*(by\s*\([^)]*\)\s*)?\(\s*cairn_store_{g}\b", e)]
    assert not summed, f"index gauges are the same on every node of a cluster; use max, not sum: {summed}"


def test_helm_copies_match():
    files = os.path.join(ROOT, "helm", "cairn", "files")
    for f in ("cairn-store.json", "cairn-store-alerts.yaml"):
        assert open(os.path.join(OBS, f)).read() == open(os.path.join(files, f)).read(), f


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("ok", _name)
