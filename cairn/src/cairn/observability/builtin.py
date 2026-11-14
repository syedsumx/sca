"""The built-in ops dashboards, as code: seeded at start (``seed``), idempotently (a dashboard is written only when its
definition here changed), owned by Cairn (``urn:li:corpuser:cairn-observability``), listed only to operators
(``VIEW_OPERATIONS`` or ``MANAGE_SYSTEM_OPERATIONS``). Nobody changes them in place: **Copy** makes one's own.

The metrics they read: the span metrics and service graph the bundled Collector derives from every component's traces
(``traces_span_metrics_*``, ``traces_service_graph_*``), Cairn's own (``cairn_*`` on ``/metrics``:
``cairn.common.metrics``), Cairn Store's (``cairn_store_*``), CloudNativePG's exporter (``cnpg_*``) and Loki and
Tempo for logs and traces.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SERVER = 'span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"'
RI = "[$__rate_interval]"
ERR = [{"value": None, "color": "green"}, {"value": 0.01, "color": "orange"}, {"value": 0.05, "color": "red"}]
TIME = {"id": "time", "kind": "time", "label": "Time range", "default": "1h", "refresh": 30}
SERVICE = {"id": "service", "kind": "variable", "label": "Service", "default": "cairn-api",
           "query": {"source": "label_values", "label": "service_name", "match": "traces_span_metrics_calls_total"}}


def prom(title: str, *queries: tuple[str, str], view: str = "timeseries", width: int = 1, unit: str = "",
         thresholds=None, instant: bool = False, **display) -> dict:
    d = {**({"unit": unit} if unit else {}), **({"thresholds": thresholds} if thresholds else {}), **display}
    return {"title": title, "width": width, "view": view, "display": d,
            "promql": {"queries": [{"expr": e, "legend": lg} for e, lg in queries], "instant": instant, "step": None}}


def _rate(sel: str) -> str:
    return f"sum by (service_name) (rate(traces_span_metrics_calls_total{{{sel}}}{RI}))"


def _errors(sel: str, by: str = "service_name") -> str:
    g = f"sum by ({by})" if by else "sum"
    return (f'{g} (rate(traces_span_metrics_calls_total{{{sel},status_code="STATUS_CODE_ERROR"}}{RI})) / '
            f"clamp_min({g} (rate(traces_span_metrics_calls_total{{{sel}}}{RI})), 1e-9)")


def _p(q: float, sel: str, by: str = "service_name") -> str:
    return (f"histogram_quantile({q}, sum by ({by + ', ' if by else ''}le) "
            f"(rate(traces_span_metrics_duration_milliseconds_bucket{{{sel}}}{RI})))")


PLATFORM = {
    "id": "ops-platform-overview", "name": "Platform overview",
    "description": "Every component's requests, errors and latency (from their traces), policy decisions and the "
                   "decision cache, Trino, the store and catalog, jobs and flows, and what is firing.",
    "controls": [TIME],
    "tiles": [
        prom("Requests / s by service", (_rate(SERVER), "{{service_name}}"), width=2, unit="reqps"),
        prom("Error ratio by service", (_errors(SERVER), "{{service_name}}"), unit="percentunit", thresholds=ERR),
        prom("p95 latency by service", (_p(0.95, SERVER), "{{service_name}}"), unit="ms"),
        prom("Requests / s now", (f"sum(rate(traces_span_metrics_calls_total{{{SERVER}}}[5m]))", "requests/s"),
             view="stat", unit="reqps", instant=True),
        prom("Errors now", (_errors(SERVER, by="").replace(RI, "[5m]"), "error ratio"), view="gauge",
             unit="percentunit", thresholds=ERR, instant=True, min=0, max=0.1),
        prom("Policy decisions / s", (f"sum by (decision) (rate(cairn_pdp_decisions_total{RI}))", "{{decision}}"),
             unit="ops"),
        prom("Decision cache hit rate", (f'sum(rate(cairn_pdp_cache_lookups_total{{result="hit"}}{RI})) / '
                                         f"clamp_min(sum(rate(cairn_pdp_cache_lookups_total{RI})), 1e-9)", "hit rate"),
             view="gauge", unit="percentunit", min=0, max=1,
             thresholds=[{"value": None, "color": "red"}, {"value": 0.5, "color": "orange"},
                         {"value": 0.8, "color": "green"}]),
        prom("Policy decision p99", (f"histogram_quantile(0.99, sum by (le) (rate(cairn_pdp_decision_seconds_bucket{RI})))",
                                     "p99"), unit="s"),
        prom("Trino queries / s and errors",
             (f'sum(rate(traces_span_metrics_calls_total{{service_name="trino",{SERVER}}}{RI}))', "calls/s"),
             (f'sum(rate(traces_span_metrics_calls_total{{service_name="trino",{SERVER},'
              f'status_code="STATUS_CODE_ERROR"}}{RI}))', "errors/s"), unit="reqps"),
        prom("Store requests / s by operation", (f"topk(10, sum by (op) (rate(cairn_store_requests_total{RI})))",
                                                 "{{op}}"), unit="reqps"),
        prom("Calls into the store and catalog",
             (f'sum by (server) (rate(traces_service_graph_request_total{{server=~".*(store|catalog).*"}}{RI}))',
              "{{server}}"), unit="reqps"),
        prom("Job and flow runs", ("sum by (kind, status) (increase(cairn_runs_total[$__interval]))",
                                   "{{kind}} {{status}}"), view="bars"),
        prom("Failed runs in range", ('sum(increase(cairn_runs_total{status="failed"}[$__range]))', "failed"),
             view="stat", instant=True, thresholds=[{"value": None, "color": "green"}, {"value": 1, "color": "red"}]),
        {"title": "Firing alerts", "width": 2, "view": "table", "display": {},
         "alerts": {"state": "firing", "labels": {}, "severity": None}},
    ],
}

SERVICE_BOARD = {
    "id": "ops-service", "name": "Service detail",
    "description": "One component ($service): requests, errors, latency and its distribution, its error logs, and "
                   "its slowest and failing requests, each linked to its journey.",
    "controls": [TIME, SERVICE],
    "tiles": [
        prom("Requests / s", (f'sum by (span_name) (rate(traces_span_metrics_calls_total{{service_name="$service",'
                              f"{SERVER}}}{RI}))", "{{span_name}}"), width=2, unit="reqps"),
        prom("Error ratio", (_errors(f'service_name="$service",{SERVER}', by=""), "error ratio"),
             unit="percentunit", thresholds=ERR),
        prom("Latency", (_p(0.5, f'service_name="$service",{SERVER}', by=""), "p50"),
             (_p(0.95, f'service_name="$service",{SERVER}', by=""), "p95"),
             (_p(0.99, f'service_name="$service",{SERVER}', by=""), "p99"),
             unit="ms"),
        prom("Latency distribution", ('sum by (le) (increase(traces_span_metrics_duration_milliseconds_bucket'
                                      f'{{service_name="$service",{SERVER}}}[$__interval]))', "{{le}}"),
             view="heatmap", width=2, unit="ms"),
        {"title": "Error logs", "width": 2, "view": "logs", "display": {},
         "logql": {"queries": [{"expr": '{service_name="$service"} | severity_text=~"(?i)error|critical"',
                                "legend": ""}], "mode": "logs", "limit": 100, "step": None}},
        {"title": "Log lines / s by level", "width": 1, "view": "bars", "display": {},
         "logql": {"queries": [{"expr": 'sum by (severity_text) (count_over_time({service_name="$service"}'
                                        '[$__interval]))', "legend": "{{severity_text}}"}],
                   "mode": "metric", "limit": 100, "step": None}},
        {"title": "Slowest requests", "width": 1, "view": "table", "display": {},
         "traces": {"query": '{ resource.service.name = "$service" && duration > 500ms }', "limit": 20}},
        {"title": "Failed requests", "width": 2, "view": "table", "display": {},
         "traces": {"query": '{ resource.service.name = "$service" && status = error }', "limit": 20}},
    ],
}

CNPG = 'pod=~"$instance-[0-9]+"'
POSTGRES = {
    "id": "ops-postgres", "name": "PostgreSQL instance",
    "description": "A managed PostgreSQL instance ($instance): its numbers as Cairn reads them (connections, cache "
                   "hits, commits, locks, size), and over time from CloudNativePG's exporter where Prometheus scrapes "
                   "it (Kubernetes).",
    "controls": [TIME, {"id": "instance", "kind": "variable", "label": "Instance", "default": "default",
                        "query": {"source": "postgres_instances"}}],
    "tiles": [
        {"title": "Now", "width": 2, "view": "stat", "display": {}, "postgres": {"instance": "$instance"}},
        {"title": "Databases", "width": 2, "view": "table", "display": {}, "postgres": {"instance": "$instance"}},
        prom("Connections", (f"sum by (state) (cnpg_backends_total{{{CNPG}}})", "{{state}}")),
        prom("Transactions / s", (f"sum(rate(cnpg_pg_stat_database_xact_commit{{{CNPG}}}{RI}))", "commits"),
             (f"sum(rate(cnpg_pg_stat_database_xact_rollback{{{CNPG}}}{RI}))", "rollbacks"), unit="ops"),
        prom("Replication lag", (f"max by (pod) (cnpg_pg_replication_lag{{{CNPG}}})", "{{pod}}"), unit="s",
             thresholds=[{"value": None, "color": "green"}, {"value": 30, "color": "orange"},
                         {"value": 300, "color": "red"}]),
        prom("Database size", (f"sum by (datname) (cnpg_pg_database_size_bytes{{{CNPG}}})", "{{datname}}"),
             unit="bytes"),
    ],
}

SLOS = {
    "id": "ops-slos-alerts", "name": "SLOs & alerts",
    "description": "An SLO ($slo): its SLI, error budget and burn, and every alert that is firing or pending.",
    "controls": [{**TIME, "default": "24h", "refresh": 60},
                 {"id": "slo", "kind": "variable", "label": "SLO", "default": None, "query": {"source": "slos"}}],
    "tiles": [
        {"title": "SLO", "width": 1, "view": "stat", "display": {}, "slo": {"id": "$slo"}},
        {"title": "Budget burn (1h)", "width": 1, "view": "timeseries", "display": {
            "thresholds": [{"value": None, "color": "green"}, {"value": 1, "color": "orange"},
                           {"value": 14.4, "color": "red"}]}, "slo": {"id": "$slo"}},
        {"title": "Critical, firing", "width": 2, "view": "table", "display": {},
         "alerts": {"state": "firing", "labels": {}, "severity": "critical"}},
        {"title": "Firing and pending", "width": 2, "view": "table", "display": {},
         "alerts": {"state": "open", "labels": {}, "severity": None}},
    ],
}

STORE = {
    "id": "ops-store", "name": "Cairn Store",
    "description": "The object store: requests, errors, latency, bytes in, objects and disk.",
    "controls": [TIME],
    "tiles": [
        prom("Requests / s by operation", (f"topk(10, sum by (op) (rate(cairn_store_requests_total{RI})))", "{{op}}"),
             width=2, unit="reqps"),
        prom("Server errors", (f'sum(rate(cairn_store_requests_total{{status=~"5.."}}{RI})) / '
                               f"clamp_min(sum(rate(cairn_store_requests_total{RI})), 1e-9)", "5xx ratio"),
             unit="percentunit", thresholds=ERR),
        prom("Mean latency by operation", (f"topk(10, sum by (op) (rate(cairn_store_request_seconds_total{RI})) / "
                                           f"clamp_min(sum by (op) (rate(cairn_store_requests_total{RI})), 1e-9))",
                                           "{{op}}"), unit="s"),
        prom("Bytes received / s", (f"sum(rate(cairn_store_received_bytes_total{RI}))", "received"), unit="Bps"),
        prom("Objects", ("sum(cairn_store_objects)", "objects"), view="stat", instant=True),
        prom("Disk free", ("min(cairn_store_disk_free_bytes / cairn_store_disk_total_bytes)", "free"), view="gauge",
             instant=True, unit="percentunit", min=0, max=1,
             thresholds=[{"value": None, "color": "red"}, {"value": 0.1, "color": "orange"},
                         {"value": 0.2, "color": "green"}]),
    ],
}

TRINO = {
    "id": "ops-trino", "name": "Trino and access control",
    "description": "Trino's queries, errors and latency, the access checks it asks Cairn for, and its slowest queries.",
    "controls": [TIME],
    "tiles": [
        prom("Trino calls / s", (f'sum by (span_name) (rate(traces_span_metrics_calls_total{{service_name="trino",'
                                 f"{SERVER}}}{RI}))", "{{span_name}}"), width=2, unit="reqps"),
        prom("Trino p95", (_p(0.95, f'service_name="trino",{SERVER}', by=""),
                           "p95"), unit="ms"),
        prom("Access checks / s (Trino → Cairn)",
             (f'sum(rate(traces_span_metrics_calls_total{{service_name="cairn-api",span_name=~".*/v1/trino/.*",'
              f"{SERVER}}}{RI}))", "checks/s"), unit="ops"),
        {"title": "Slowest Trino requests", "width": 2, "view": "table", "display": {},
         "traces": {"query": '{ resource.service.name = "trino" && duration > 1s }', "limit": 20}},
    ],
}

BUILTINS = [PLATFORM, SERVICE_BOARD, POSTGRES, SLOS, STORE, TRINO]


def seed(boards) -> list[str]:
    """Makes (or updates) every built-in dashboard; returns the ids written."""
    written = []
    for b in BUILTINS:
        if boards.put_builtin(b["id"], b["name"], b["description"], b["tiles"], b["controls"]):
            written.append(b["id"])
    if written:
        logger.info("built-in ops dashboards written: %s", ", ".join(written))
    return written
