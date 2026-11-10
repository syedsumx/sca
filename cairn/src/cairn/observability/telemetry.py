"""OpenTelemetry for Cairn's processes: traces, metrics and logs over OTLP/HTTP, configured by the standard
``OTEL_*`` environment.

* **On** when ``OTEL_EXPORTER_OTLP_ENDPOINT`` (or a signal's own ``OTEL_EXPORTER_OTLP_<SIGNAL>_ENDPOINT``, or the
  older ``CAIRN_OTEL_ENDPOINT``) is set and the ``otel`` extra is installed; ``OTEL_SDK_DISABLED=true`` turns it
  off. ``OTEL_TRACES_EXPORTER``, ``OTEL_METRICS_EXPORTER`` and ``OTEL_LOGS_EXPORTER`` set to ``none`` turn one
  signal off. Only ``http/protobuf`` is shipped (``grpc`` falls back to it, with a warning).
* **Who.** ``OTEL_SERVICE_NAME`` (the chart sets ``cairn-<component>``), else ``cairn-<process>``;
  ``OTEL_RESOURCE_ATTRIBUTES`` adds to the resource.
* **Sampling.** ``OTEL_TRACES_SAMPLER`` / ``OTEL_TRACES_SAMPLER_ARG`` as the SDK reads them; without them,
  parent-based, at ``CAIRN_OTEL_SAMPLE_RATIO`` (0.1). A request that arrives with a sampled ``traceparent`` is
  always followed (a trace started by Trino, a browser, another service).
* **Credentials for the backend** (Grafana Cloud, Honeycomb, Datadog, New Relic): ``OTEL_EXPORTER_OTLP_HEADERS``
  from a Kubernetes Secret (an env ``secretKeyRef``, never a command line), or ``CAIRN_OTEL_HEADERS_FILE``, a file
  holding ``name=value,name2=value2`` (a mounted Secret or a Docker secret).
* **What is traced.** FastAPI requests (``/health`` and ``/metrics`` excepted), outgoing httpx calls (with W3C
  ``traceparent`` injected, so Trino, the Cairn Store, Catalog and Ontology and Cairn's own services join the
  trace), SQLAlchemy statements, background cycles (:func:`span`), flow steps (linked to the request that started
  the run) and Spark runs (linked through their OpenLineage run id). Trino queries carry the trace id as a client
  tag (``cairn_trace=<id>``), so a query in Trino's history leads back to its trace.
* **Logs** go through the same pipeline with the trace and span id of the request that wrote them, so a trace's
  logs are one query away (Loki: ``{service_name=~".+"} | trace_id="<id>"``).
* **Redaction** (:mod:`cairn.observability.redact`) runs on every span and log record before export; one that
  cannot be cleaned is dropped and counted (``cairn_telemetry_dropped_total``).
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from typing import Any, Iterator, Optional

from cairn.observability import redact

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_state: dict[str, Any] = {"on": False, "providers": None, "service": None}
_TRUE = ("1", "true", "yes", "on")

try:
    from prometheus_client import Counter as _Counter

    DROPPED = _Counter("cairn_telemetry_dropped_total", "Spans and log records dropped because they could not be "
                       "cleaned of credentials or values", ["signal"])
except ImportError:  # pragma: no cover
    DROPPED = None


def _dropped(signal: str) -> None:
    if DROPPED is not None:
        DROPPED.labels(signal).inc()


def _endpoint() -> Optional[str]:
    for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "CAIRN_OTEL_ENDPOINT"):
        if os.environ.get(k, "").strip():
            return os.environ[k].strip().rstrip("/")
    return None


def configured() -> bool:
    """Whether this process would export telemetry (an endpoint is set and the SDK is not disabled)."""
    return bool(_endpoint()) and os.environ.get("OTEL_SDK_DISABLED", "").lower() not in _TRUE


def enabled() -> bool:
    return bool(_state["on"])


def _headers() -> Optional[dict]:
    path = os.environ.get("CAIRN_OTEL_HEADERS_FILE", "")
    if not path:
        return None                       # the exporters read OTEL_EXPORTER_OTLP_HEADERS themselves
    from urllib.parse import unquote

    with open(path, encoding="utf-8") as fh:
        raw = fh.read().strip()
    out = {}
    for part in raw.replace("\n", ",").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[unquote(k.strip())] = unquote(v.strip())
    return out


def _signal_url(signal: str) -> Optional[str]:
    """The endpoint for one signal, when only the legacy CAIRN_OTEL_ENDPOINT is set (the SDK reads the standard
    variables itself)."""
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT"):
        return None
    legacy = os.environ.get("CAIRN_OTEL_ENDPOINT", "").rstrip("/")
    return f"{legacy}/v1/{signal}" if legacy else None


def _off(signal: str) -> bool:
    return os.environ.get(f"OTEL_{signal.upper()}_EXPORTER", "otlp").strip().lower() == "none"


def setup(service: str, app: Any = None, engine: Any = None) -> bool:
    """Turns telemetry on for this process (once) and instruments ``app`` and ``engine`` (each time). Returns
    whether telemetry is on."""
    if not configured():
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    except ImportError:
        logger.warning("an OpenTelemetry endpoint is set but the otel extra is not installed; telemetry is off")
        return False
    with _lock:
        if not _state["on"]:
            try:
                _start(service)
            except Exception:  # noqa: BLE001 - telemetry never stops Cairn from starting
                logger.exception("OpenTelemetry could not be set up; telemetry is off")
                return False
    providers = _state["providers"]
    if engine is not None and not getattr(engine, "_cairn_otel", False):
        try:                     # its version range lags SQLAlchemy's releases; the API it hooks is unchanged
            SQLAlchemyInstrumentor().instrument(engine=engine, tracer_provider=providers["traces"],
                                                enable_commenter=False, skip_dep_check=True)
        except Exception:  # noqa: BLE001 - statements untraced, the rest still is
            logger.warning("SQLAlchemy statements are not traced (the instrumentation refused this version)")
        try:
            engine._cairn_otel = True
        except AttributeError:
            pass
    if app is not None:
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics,/-/", tracer_provider=providers["traces"],
                                           exclude_spans=["receive", "send"],
                                           meter_provider=providers["metrics"])
    return trace.get_tracer_provider() is providers["traces"]


def instrument_asgi(app: Any) -> Any:
    """A plain ASGI app (the core proxy) with server spans; the app itself when telemetry is off."""
    if not _state["on"]:
        return app
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

    providers = _state["providers"]
    return OpenTelemetryMiddleware(app, excluded_urls="/__cairn/health", tracer_provider=providers["traces"],
                                   meter_provider=providers["metrics"])


def _start(service: str) -> None:
    from opentelemetry import metrics as metrics_api
    from opentelemetry import trace
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio

    from cairn import __version__

    if os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf") not in ("http/protobuf", ""):
        logger.warning("OTEL_EXPORTER_OTLP_PROTOCOL=%s: Cairn exports http/protobuf (point it at port 4318)",
                       os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL"))
    name = os.environ.get("OTEL_SERVICE_NAME") or f"cairn-{service}"
    resource = Resource.create({"service.name": name, "service.version": __version__,
                                "service.namespace": "cairn"})
    headers = _headers()
    sampler = None
    if not os.environ.get("OTEL_TRACES_SAMPLER"):
        sampler = ParentBasedTraceIdRatio(float(os.environ.get("CAIRN_OTEL_SAMPLE_RATIO", "0.1")))
    tp = TracerProvider(resource=resource, **({"sampler": sampler} if sampler is not None else {}))
    if not _off("traces"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        tp.add_span_processor(RedactingSpanProcessor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=_signal_url("traces"), headers=headers))))
    trace.set_tracer_provider(tp)
    mp = None
    if not _off("metrics"):
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        mp = MeterProvider(resource=resource, metric_readers=[PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=_signal_url("metrics"), headers=headers))])
        metrics_api.set_meter_provider(mp)
    if not _off("logs"):
        _start_logs(resource, headers)
    HTTPXClientInstrumentor().instrument(tracer_provider=tp, meter_provider=mp)
    _state.update(on=True, providers={"traces": tp, "metrics": mp}, service=name)
    logger.info("OpenTelemetry on: %s to %s", name, redact.scrub_url(_endpoint() or ""))


def _start_logs(resource, headers) -> None:
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    lp = LoggerProvider(resource=resource)
    lp.add_log_record_processor(RedactingLogProcessor(BatchLogRecordProcessor(
        OTLPLogExporter(endpoint=_signal_url("logs"), headers=headers))))
    set_logger_provider(lp)
    import warnings

    with warnings.catch_warnings():          # the SDK's handler, deprecated for a separate package's, works the same
        warnings.simplefilter("ignore", DeprecationWarning)
        handler = LoggingHandler(
            level=getattr(logging, os.environ.get("CAIRN_OTEL_LOG_LEVEL", "INFO").upper(), logging.INFO),
            logger_provider=lp)
    handler.addFilter(lambda r: not r.name.startswith(("opentelemetry", "urllib3")))
    logging.getLogger().addHandler(handler)


try:
    from opentelemetry.sdk._logs import LogRecordProcessor as _LogProcessorBase
    from opentelemetry.sdk.trace import SpanProcessor as _SpanProcessorBase
except ImportError:  # pragma: no cover - without the otel extra nothing below is used
    _SpanProcessorBase = _LogProcessorBase = object  # type: ignore[misc,assignment]


def clean_span(span):
    """A copy of ``span`` that is safe to export (:mod:`cairn.observability.redact`); raises
    :class:`~cairn.observability.redact.Unredactable` when it cannot be made so."""
    from opentelemetry.sdk.trace import Event, ReadableSpan
    from opentelemetry.trace import Link, SpanKind, Status

    server = span.kind == SpanKind.SERVER
    attrs = redact.scrub_attributes(span.attributes, server)
    events = []
    for e in span.events:
        ea = redact.scrub_attributes(e.attributes)
        for k in ("exception.message", "exception.stacktrace"):
            if isinstance(ea.get(k), str):
                ea[k] = redact.scrub_error(ea[k])
        events.append(Event(redact.scrub_text(e.name), ea, e.timestamp))
    status = span.status
    if status.description:
        status = Status(status.status_code, redact.scrub_error(status.description))
    links = [Link(lk.context, redact.scrub_attributes(lk.attributes)) for lk in span.links]
    name = redact.scrub_text(span.name)
    redact.check_clean([name, status.description, *attrs.values(),
                        *(v for e in events for v in (e.attributes or {}).values())])
    return ReadableSpan(name=name, context=span.context, parent=span.parent, resource=span.resource, attributes=attrs,
                        events=events, links=links, kind=span.kind, status=status, start_time=span.start_time,
                        end_time=span.end_time, instrumentation_scope=span.instrumentation_scope)


class RedactingSpanProcessor(_SpanProcessorBase):  # type: ignore[misc]
    """Cleans each finished span before the exporter's processor sees it; drops the ones it cannot clean."""

    def __init__(self, inner):
        self.inner = inner

    def on_start(self, span, parent_context=None) -> None:
        self.inner.on_start(span, parent_context=parent_context)

    def on_end(self, span) -> None:
        try:
            clean = clean_span(span)
        except Exception:  # noqa: BLE001 - fail closed: a span that cannot be cleaned is not exported
            _dropped("traces")
            return
        self.inner.on_end(clean)

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


class RedactingLogProcessor(_LogProcessorBase):  # type: ignore[misc]
    """Cleans each log record (its message, its exception, its attributes) before export; drops the ones it
    cannot clean."""

    def __init__(self, inner):
        self.inner = inner

    def on_emit(self, log_record) -> None:
        from opentelemetry.attributes import BoundedAttributes

        rec = log_record.log_record
        try:
            body = rec.body
            if body is not None and not isinstance(body, (bool, int, float)):
                body = redact.scrub_error(body if isinstance(body, str) else str(body))
            attrs = redact.scrub_attributes(dict(rec.attributes or {}))
            for k in ("exception.message", "exception.stacktrace"):
                if isinstance(attrs.get(k), str):
                    attrs[k] = redact.scrub_error(attrs[k])
            redact.check_clean([body, *attrs.values()])
        except Exception:  # noqa: BLE001 - fail closed
            _dropped("logs")
            return
        rec.body = body
        rec.attributes = BoundedAttributes(attributes=attrs, immutable=False)
        self.inner.on_emit(log_record)

    emit = on_emit                      # the SDK's older name for it

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


def flush(timeout_ms: int = 10_000) -> None:
    """Exports what is buffered now (tests, and before a short-lived process exits)."""
    if not _state["on"]:
        return
    from opentelemetry import _logs

    providers = _state["providers"]
    providers["traces"].force_flush(timeout_ms)
    if providers["metrics"] is not None:
        providers["metrics"].force_flush(timeout_ms)
    lp = _logs.get_logger_provider()
    if hasattr(lp, "force_flush"):
        lp.force_flush(timeout_ms)


# ---- context in and out -----------------------------------------------------------------------------------------
def trace_id() -> Optional[str]:
    """The current trace id (32 hex digits), for log lines, error pages and answers; None without a trace."""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        return format(ctx.trace_id, "032x") if ctx.is_valid else None
    except ImportError:
        return None


def current_traceparent() -> Optional[str]:
    """The W3C ``traceparent`` of the current span (to store with work that runs later: a flow run, a job)."""
    return outgoing_headers().get("traceparent")


def outgoing_headers() -> dict[str, str]:
    """W3C trace context headers for a call this process makes outside httpx (Trino's Python client, a
    subprocess's environment)."""
    try:
        from opentelemetry.propagate import inject
    except ImportError:
        return {}
    carrier: dict[str, str] = {}
    inject(carrier)
    return {k: v for k, v in carrier.items() if k in ("traceparent", "tracestate")}


def trino_client_tags() -> list[str]:
    """Client tags for a Trino query: ``cairn_trace=<trace id>`` (Trino's query history then leads to the trace)."""
    tid = trace_id()
    return [f"cairn_trace={tid}"] if tid else []


def link_to(traceparent: Optional[str]):
    """A span link to the span ``traceparent`` names (empty when it is missing or malformed)."""
    if not traceparent:
        return []
    try:
        from opentelemetry.trace import Link, get_current_span
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
    except ImportError:
        return []
    ctx = TraceContextTextMapPropagator().extract({"traceparent": traceparent})
    sc = get_current_span(ctx).get_span_context()
    return [Link(sc)] if sc.is_valid else []


@contextlib.contextmanager
def span(name: str, attributes: Optional[dict] = None, links_to: Optional[list] = None,
         root: bool = False) -> Iterator[Any]:
    """A span around a piece of work (a worker cycle, a flow step). ``links_to``: traceparents of the work that
    caused this (the request that started a flow run). ``root``: start a new trace (with those links) rather than
    continue the current one. A no-op when telemetry is off."""
    if not _state["on"]:
        yield None
        return
    from opentelemetry import context, trace

    links = [lk for tp in (links_to or []) for lk in link_to(tp)]
    tracer = trace.get_tracer("cairn")
    kwargs: dict = {"attributes": attributes or {}, "links": links}
    if root:
        kwargs["context"] = context.Context()
    with tracer.start_as_current_span(name, **kwargs) as s:
        yield s


def record_linked_run(kind: str, run_id: str, traceparent: Optional[str], attributes: Optional[dict] = None) -> None:
    """A span for work another system ran (a Spark application, an Airflow task), linked to the trace that
    started it, so the request's journey shows it (``kind``: spark, airflow; ``run_id``: its OpenLineage run id)."""
    if not _state["on"] or not traceparent:
        return
    with span(f"{kind}.run", {"cairn.run_id": run_id, "cairn.run_kind": kind, **(attributes or {})},
              links_to=[traceparent], root=True):
        pass
