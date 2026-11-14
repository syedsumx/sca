"""OpenTelemetry for Cairn's processes (kept for the callers that import it from here): see
:mod:`cairn.observability.telemetry` for what is traced and how it is configured (``OTEL_*``, or the older
``CAIRN_OTEL_ENDPOINT`` and ``CAIRN_OTEL_SAMPLE_RATIO``)."""

from __future__ import annotations

from typing import Optional

from cairn.observability import telemetry


def setup_tracing(app=None, service: str = "cairn", engine=None) -> bool:
    return telemetry.setup(service, app=app, engine=engine)


def trace_id() -> Optional[str]:
    """The current trace id, for log lines and error pages (None when tracing is off)."""
    return telemetry.trace_id()
