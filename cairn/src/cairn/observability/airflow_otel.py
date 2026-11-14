"""Airflow's own OpenTelemetry, from Cairn's observability configuration.

:func:`airflow_otel_env` turns Cairn's telemetry settings into the environment an Airflow 3.3 process needs to send
its traces (a DAG run is one trace, ``dag_run.<dag>``, with a ``worker.<task>`` span per task try) and its metrics
(scheduler heartbeat, DAG processing, task durations, pool slots, executor slots...) to Cairn's bundled
OpenTelemetry Collector. The Compose overlay, the Helm values (``deploy/airflow/values-cairn.yaml``) and Cairn's
managed Airflow deployments all use it, so every Airflow is set up the same way.

**Through the Collector, never directly.** Airflow cleans nothing it sends (a task's exception can carry a
connection URI with its password), so its telemetry always goes to a Collector that runs Cairn's redaction (the
bundled one, whose exporter pipelines all redact first; see :mod:`cairn.observability.exporters`). When Cairn's own
telemetry goes straight to a vendor with credentials (``OTEL_EXPORTER_OTLP_HEADERS``, ``CAIRN_OTEL_HEADERS_FILE``)
and no Collector endpoint is given for Airflow, the helper refuses (fail closed) rather than point Airflow at it.

Airflow 3.2 deprecated its own ``otel_host``/``otel_port`` settings for the standard ``OTEL_*`` variables; this
helper sets only the standard ones (and the two switches Airflow still reads).
"""

from __future__ import annotations

import os
import re
from typing import Mapping, Optional

__all__ = ["airflow_otel_env"]

_SERVICE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_ATTR = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _ratio(cfg: Mapping[str, str]) -> str:
    """Head sampling for Airflow's traces: Cairn's own (``OTEL_TRACES_SAMPLER_ARG`` with a ratio sampler, else
    ``CAIRN_OTEL_SAMPLE_RATIO``), 1.0 when neither is set (DAG runs are few and each one matters)."""
    raw = ""
    if "traceidratio" in str(cfg.get("OTEL_TRACES_SAMPLER", "")):
        raw = str(cfg.get("OTEL_TRACES_SAMPLER_ARG", ""))
    raw = raw or str(cfg.get("CAIRN_OTEL_SAMPLE_RATIO", "") or "1.0")
    try:
        v = float(raw)
    except ValueError:
        raise ValueError(f"the trace sampling ratio {raw!r} is not a number") from None
    if not 0.0 <= v <= 1.0:
        raise ValueError(f"the trace sampling ratio {raw!r} is not between 0 and 1")
    return repr(v)


def airflow_otel_env(cfg: Optional[Mapping[str, str]] = None, service_name: str = "airflow",
                     deployment: str = "") -> dict[str, str]:
    """The environment variables that send one Airflow's traces and metrics to Cairn's Collector.

    ``cfg``: Cairn's observability configuration as environment names (default: this process's environment):

    * ``CAIRN_OBS_AIRFLOW_OTLP_ENDPOINT``: the Collector's OTLP/HTTP endpoint as Airflow reaches it (e.g.
      ``http://cairn-otel-collector.cairn.svc:4318`` from another namespace); else ``OTEL_EXPORTER_OTLP_ENDPOINT``,
      Cairn's own, when that is a Collector (no credentials configured for it);
    * ``OTEL_TRACES_SAMPLER``/``OTEL_TRACES_SAMPLER_ARG`` or ``CAIRN_OTEL_SAMPLE_RATIO``: the trace sampling ratio;
    * ``CAIRN_OBS_AIRFLOW_METRIC_INTERVAL_MS``: how often metrics are sent (default 15000).

    ``service_name`` is the OpenTelemetry service (``airflow``, or one per deployment); ``deployment`` (optional)
    becomes the resource attribute ``cairn.airflow.deployment``. Returns ``{}`` when Cairn's telemetry is off (no
    endpoint); raises ``ValueError`` when the settings are wrong or would bypass the Collector's redaction.
    """
    cfg = os.environ if cfg is None else cfg
    if not _SERVICE.match(service_name or ""):
        raise ValueError("service_name: letters, digits, '.', '_' and '-' (at most 63)")
    if deployment and not _ATTR.match(deployment):
        raise ValueError("deployment: letters, digits, '.', '_' and '-' (at most 128)")
    endpoint = str(cfg.get("CAIRN_OBS_AIRFLOW_OTLP_ENDPOINT", "") or "").strip()
    if not endpoint:
        endpoint = str(cfg.get("OTEL_EXPORTER_OTLP_ENDPOINT", "") or cfg.get("CAIRN_OTEL_ENDPOINT", "") or "").strip()
        if endpoint and (cfg.get("OTEL_EXPORTER_OTLP_HEADERS") or cfg.get("CAIRN_OTEL_HEADERS_FILE")):
            raise ValueError("Cairn's telemetry goes to a backend with credentials, not to a Collector: Airflow's "
                             "telemetry is not cleaned by Airflow, so it must go through a Collector that redacts "
                             "(set CAIRN_OBS_AIRFLOW_OTLP_ENDPOINT to the bundled Collector)")
    if not endpoint:
        return {}
    if not re.match(r"^https?://[^\s/?#]+(:\d+)?/?$", endpoint):
        raise ValueError(f"the Collector endpoint {endpoint!r} is not an http(s)://host:port URL (OTLP/HTTP)")
    try:
        interval = int(str(cfg.get("CAIRN_OBS_AIRFLOW_METRIC_INTERVAL_MS", "") or "15000"))
    except ValueError:
        raise ValueError("CAIRN_OBS_AIRFLOW_METRIC_INTERVAL_MS: milliseconds") from None
    if not 1000 <= interval <= 300_000:
        raise ValueError("CAIRN_OBS_AIRFLOW_METRIC_INTERVAL_MS: 1000 to 300000")
    attrs = "service.namespace=cairn,cairn.component=airflow"
    if deployment:
        attrs += f",cairn.airflow.deployment={deployment}"
    return {
        "AIRFLOW__TRACES__OTEL_ON": "True",
        "AIRFLOW__METRICS__OTEL_ON": "True",
        "AIRFLOW__METRICS__OTEL_PREFIX": "airflow",
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint.rstrip("/"),
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_TRACES_EXPORTER": "otlp_proto_http",     # Airflow's default, "otlp", is gRPC whatever the protocol
        "OTEL_SERVICE_NAME": service_name,
        "OTEL_RESOURCE_ATTRIBUTES": attrs,
        "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
        "OTEL_TRACES_SAMPLER_ARG": _ratio(cfg),
        "OTEL_METRIC_EXPORT_INTERVAL": str(interval),
    }
