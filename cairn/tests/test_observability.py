"""Monitoring: every metric the shipped alerts and dashboard use exists, the Helm copies match,
and the domain metrics move when the platform works."""

import json
import os
import re

import yaml
from prometheus_client import REGISTRY, generate_latest

import cairn.app  # noqa: F401  (HTTP metrics)
import cairn.coreproxy.proxy  # noqa: F401  (proxy metrics)
from cairn.common import metrics

ROOT = os.path.join(os.path.dirname(__file__), "..")
OBS = os.path.join(ROOT, "deploy", "observability")


def _known() -> set[str]:
    """Series names every registered metric exports, labelled or not (a family has no samples until
    a label value is used)."""
    suffixes = {"counter": ("_total",), "histogram": ("_bucket", "_count", "_sum"), "gauge": ("",),
                "summary": ("_count", "_sum", "")}
    names = set()
    for m in REGISTRY.collect():
        names |= {m.name + s for s in suffixes.get(m.type, ("",))}
    return names


def _referenced(text: str) -> set[str]:
    return set(re.findall(r"\bcairn_[a-z_]+", text))


def test_alerts_and_dashboard_only_use_real_metrics():
    known = _known()
    alerts = open(os.path.join(OBS, "cairn-alerts.yaml")).read()
    rules = [r for g in yaml.safe_load(alerts)["groups"] for r in g["rules"]]
    assert len(rules) == 11 and all(r["labels"]["severity"] in ("critical", "warning", "info") for r in rules)
    dash = json.load(open(os.path.join(OBS, "cairn-overview.json")))
    exprs = " ".join(t["expr"] for p in dash["panels"] for t in p["targets"])
    missing = (_referenced(alerts) | _referenced(exprs)) - known
    assert not missing, f"alerts or dashboard use metrics nobody exports: {sorted(missing)}"


def test_helm_copies_match():
    for f in ("cairn-alerts.yaml", "cairn-overview.json"):
        assert open(os.path.join(OBS, f)).read() == open(os.path.join(ROOT, "helm", "cairn", "files", f)).read(), f


def test_domain_metrics_move(engine):
    from cairn.common.audit import AuditLog

    before = generate_latest().decode()
    AuditLog(engine).record("governance", "test.event", "urn:li:corpuser:ana")
    metrics.worker_cycle("notifier", True)
    metrics.worker_cycle("scheduler", False)
    after = generate_latest().decode()

    def value(text, sample):
        m = re.search(rf"^{re.escape(sample)} ([0-9.e+]+)$", text, re.M)
        return float(m.group(1)) if m else 0.0

    assert value(after, 'cairn_audit_events_total{category="governance"}') == \
        value(before, 'cairn_audit_events_total{category="governance"}') + 1
    assert value(after, 'cairn_worker_last_success_timestamp_seconds{worker="notifier"}') > 1.7e9
    assert value(after, 'cairn_worker_errors_total{worker="scheduler"}') >= 1


def test_tracing_is_off_unless_configured(monkeypatch, caplog):
    from cairn.common import tracing

    monkeypatch.delenv("CAIRN_OTEL_ENDPOINT", raising=False)
    assert tracing.setup_tracing() is False
    monkeypatch.setenv("CAIRN_OTEL_ENDPOINT", "http://collector:4318")
    try:
        import opentelemetry.sdk  # noqa: F401
    except ImportError:
        assert tracing.setup_tracing() is False and "otel extra is not installed" in caplog.text
