"""Optional OpenTelemetry tracing. Off unless ``CAIRN_OTEL_ENDPOINT`` (an OTLP/HTTP collector, e.g.
``http://otel-collector:4318``) is set and the ``otel`` extra is installed (``pip install
cairn-platform[otel]``). Traces FastAPI requests, outgoing httpx calls and SQLAlchemy queries."""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)
_done = False


def setup_tracing(app=None, service: str = "cairn", engine=None) -> bool:
    global _done
    endpoint = os.environ.get("CAIRN_OTEL_ENDPOINT", "").rstrip("/")
    if not endpoint:
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio
    except ImportError:
        logger.warning("CAIRN_OTEL_ENDPOINT is set but the otel extra is not installed; tracing is off")
        return False
    if not _done:
        ratio = float(os.environ.get("CAIRN_OTEL_SAMPLE_RATIO", "0.1"))
        provider = TracerProvider(resource=Resource.create({"service.name": f"cairn-{service}"}),
                                  sampler=ParentBasedTraceIdRatio(ratio))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
        trace.set_tracer_provider(provider)
        HTTPXClientInstrumentor().instrument()
        _done = True
    if engine is not None:
        SQLAlchemyInstrumentor().instrument(engine=engine)
    if app is not None:
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
    logger.info("tracing to %s (sampling %s)", endpoint, os.environ.get("CAIRN_OTEL_SAMPLE_RATIO", "0.1"))
    return True


def trace_id() -> Optional[str]:
    """The current trace id, for log lines and error pages (None when tracing is off)."""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        return format(ctx.trace_id, "032x") if ctx.is_valid else None
    except ImportError:
        return None
