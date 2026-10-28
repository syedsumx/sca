"""Domain metrics for Prometheus. Every name here is used by the shipped alert rules and
dashboards (``deploy/observability``); keep them stable. Without ``prometheus_client`` they are no-ops.

Workers without an HTTP API (notifier, scheduler, context miner, executor) expose ``/metrics`` on
``CAIRN_METRICS_PORT`` when it is set (see :func:`serve_worker_metrics`).
"""

from __future__ import annotations

import os
import time
from typing import Optional


class _Noop:
    def labels(self, *a, **k):
        return self

    def inc(self, *a, **k):
        pass

    def set(self, *a, **k):
        pass

    def observe(self, *a, **k):
        pass

    def set_to_current_time(self, *a, **k):
        pass


try:
    from prometheus_client import Counter, Gauge, Histogram

    PDP_DECISIONS = Counter("cairn_pdp_decisions_total", "Policy decisions", ["decision", "mode"])
    PDP_SECONDS = Histogram("cairn_pdp_decision_seconds", "Policy decision latency",
                            buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1))
    QUALITY_EVALUATIONS = Counter("cairn_quality_evaluations_total", "Monitor evaluations", ["type", "status"])
    QUALITY_INCIDENTS = Counter("cairn_quality_incidents_total", "Quality incidents raised and resolved", ["event"])
    NOTIFICATIONS = Counter("cairn_notifications_total", "Notification deliveries", ["channel", "outcome"])
    NOTIFICATIONS_PENDING = Gauge("cairn_notifications_pending", "Notifications waiting to be delivered")
    AI_TOKENS = Counter("cairn_ai_tokens_total", "LLM tokens", ["task", "direction"])
    AI_COST = Counter("cairn_ai_cost_usd_total", "Estimated LLM spend in USD", ["task"])
    AI_BUDGET_REFUSALS = Counter("cairn_ai_budget_refusals_total", "LLM calls refused by the budget")
    AUDIT_EVENTS = Counter("cairn_audit_events_total", "Audit log entries", ["category"])
    WORKER_LAST_SUCCESS = Gauge("cairn_worker_last_success_timestamp_seconds",
                                "When a background worker last finished a cycle without error", ["worker"])
    WORKER_ERRORS = Counter("cairn_worker_errors_total", "Background worker cycles that failed", ["worker"])
    ENABLED = True
except ImportError:  # pragma: no cover
    PDP_DECISIONS = PDP_SECONDS = QUALITY_EVALUATIONS = QUALITY_INCIDENTS = NOTIFICATIONS = _Noop()
    NOTIFICATIONS_PENDING = AI_TOKENS = AI_COST = AI_BUDGET_REFUSALS = AUDIT_EVENTS = _Noop()
    WORKER_LAST_SUCCESS = WORKER_ERRORS = _Noop()
    ENABLED = False


def worker_cycle(worker: str, ok: bool) -> None:
    if ok:
        WORKER_LAST_SUCCESS.labels(worker).set(time.time())
    else:
        WORKER_ERRORS.labels(worker).inc()


def serve_worker_metrics(port: Optional[int] = None) -> Optional[int]:
    """Start a /metrics listener for a worker process (``CAIRN_METRICS_PORT``); returns the port."""
    port = port or int(os.environ.get("CAIRN_METRICS_PORT") or 0)
    if not port or not ENABLED:
        return None
    from prometheus_client import start_http_server

    start_http_server(port)
    return port
