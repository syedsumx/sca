"""Observability: OpenTelemetry for every Cairn process (telemetry, redact), and in Cairn the health map, request
journeys, logs, SLOs, alert rules, silences and incidents (service, api), read from Prometheus, Loki and Tempo
(backends); pipeline observability, data products and SLAs (pipelines); OTLP destinations the bundled Collector
exports to (exporters); Airflow's own OpenTelemetry (:func:`airflow_otel_env`)."""

from cairn.observability.airflow_otel import airflow_otel_env

__all__ = ["airflow_otel_env"]
