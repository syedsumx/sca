"""Airflow's own OpenTelemetry settings for a managed deployment, from Cairn's observability configuration.

What Airflow's telemetry looks like and how Cairn reads it belongs to observability (``cairn.observability``); a
deployment only points Airflow at the Collector. When ``cairn.observability.airflow_otel_env`` exists it decides;
otherwise this minimal one: with ``CAIRN_AIRFLOW_OTEL_COLLECTOR`` (``host:port`` of the Collector's OTLP/HTTP
receiver as the deployment reaches it), Airflow's metrics and traces go there, each deployment its own service name.
"""

from __future__ import annotations

import os


def airflow_otel_env(deployment: str, environment: str = "", collector: str = "") -> dict[str, str]:
    try:
        from cairn import observability

        helper = getattr(observability, "airflow_otel_env", None)
    except ImportError:  # pragma: no cover - observability ships with Cairn
        helper = None
    if helper is not None:                       # observability's own (L145): what Airflow's telemetry looks like
        return dict(helper(service_name=f"airflow-{deployment}", deployment=deployment) or {})
    target = collector or os.environ.get("CAIRN_AIRFLOW_OTEL_COLLECTOR", "")
    if not target:
        return {}
    host, _, port = target.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError("CAIRN_AIRFLOW_OTEL_COLLECTOR is host:port (the Collector's OTLP/HTTP receiver)")
    service = f"airflow-{deployment}"
    ssl = "true" if os.environ.get("CAIRN_AIRFLOW_OTEL_SSL", "").lower() in ("1", "true", "yes") else "false"
    attrs = f"deployment.environment={environment}" if environment else ""
    out = {"AIRFLOW__METRICS__OTEL_ON": "true", "AIRFLOW__METRICS__OTEL_HOST": host,
           "AIRFLOW__METRICS__OTEL_PORT": port, "AIRFLOW__METRICS__OTEL_PREFIX": "airflow",
           "AIRFLOW__METRICS__OTEL_SSL_ACTIVE": ssl, "AIRFLOW__METRICS__OTEL_SERVICE": service,
           "AIRFLOW__TRACES__OTEL_ON": "true", "AIRFLOW__TRACES__OTEL_HOST": host, "AIRFLOW__TRACES__OTEL_PORT": port,
           "AIRFLOW__TRACES__OTEL_SERVICE": service, "AIRFLOW__TRACES__OTEL_SSL_ACTIVE": ssl,
           "OTEL_SERVICE_NAME": service}
    if attrs:
        out["OTEL_RESOURCE_ATTRIBUTES"] = f"service.namespace=airflow,airflow.deployment={deployment},{attrs}"
    return out
