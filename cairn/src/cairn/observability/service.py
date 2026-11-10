"""Observability in Cairn: the platform's health map, a request's journey across components, its logs, SLOs with
multi-window burn-rate alerts, alert rules, silences, and incidents.

* **Health map.** From Prometheus: the span metrics and service graph the bundled OpenTelemetry Collector derives
  from every component's traces (``traces_span_metrics_*``, ``traces_service_graph_*``) and the scrape targets'
  ``up``. Each component is ``up``, ``degraded`` (error ratio above 5 %, or an alert firing about it), ``down``
  (its scrape targets are all down) or ``unknown`` (nothing heard from it).
* **Journeys.** One trace across processes, read from Tempo (or a Tempo-compatible API); with a vendor backend
  that has none, a link to the trace there. **Logs** of a trace, request or component come from Loki.
* **SLOs.** ``indicator``: ``ratio`` (PromQL for bad and total events over ``$window``) or ``latency`` (a
  histogram, a selector and a threshold that is one of its buckets); ``objective`` (a percentage) over ``window``.
  Alerts burn the error budget the SRE workbook's way: for each pair of windows (by default 1h and 5m at 14.4x,
  6h and 30m at 6x as ``critical``; 1d and 2h at 3x, 3d and 6h at 1x as ``warning``) the alert fires while both
  windows burn faster than the factor.
* **Alert rules.** A PromQL or LogQL condition (each series it returns is an alert), ``pending_for`` before it
  fires, a severity, labels, a summary (``{{ $value }}``, ``{{ $labels.name }}``), notification routes (the
  existing ones; they receive the alert whatever their own filters) and people. Firing alerts repeat every
  ``repeat_interval`` until acknowledged. A rule whose backend cannot be read keeps its alerts as they were (it
  never resolves an alert it cannot see) and says why.
* **Silences** match alerts by labels (``alertname``, ``severity``, ``source`` and the series' own) between two
  times; a silenced alert notifies no one and opens no incident.
* **Incidents.** A firing alert (``critical``, or any severity with ``open_incident``) opens an incident, or joins
  the open one of its rule or SLO. Its timeline holds its alerts, notes, status changes, and, read when asked,
  what changed around it: the audit log's changes, infrastructure-as-code change requests applied, and deploys.
  :meth:`ObservabilityService.incident_context` is everything an agent working on one needs, in one answer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import BigInteger, Boolean, Column, Float, Index, Integer, String, Table, Text, and_, delete, insert, \
    select, update

from cairn.common.db import JSONText, metadata
from cairn.observability import redact
from cairn.observability.backends import TRACE_ID, BackendError, Backends

logger = logging.getLogger(__name__)
ACTOR = "urn:li:corpuser:cairn-observability"
ID = r"^[a-z0-9][a-z0-9\-]{1,62}$"
SEVERITIES = ("critical", "warning", "info")
DEFAULT_BURNS = [
    {"long": "1h", "short": "5m", "factor": 14.4, "severity": "critical"},
    {"long": "6h", "short": "30m", "factor": 6.0, "severity": "critical"},
    {"long": "1d", "short": "2h", "factor": 3.0, "severity": "warning"},
    {"long": "3d", "short": "6h", "factor": 1.0, "severity": "warning"},
]
CHANGE_ACTION = re.compile(r"(?:^|[._])(created|updated|update|deleted|delete|removed|changed|settings_changed|set|put|"
                           r"saved|published|applied|enabled|disabled|granted|revoked|installed|deployed|rotated|"
                           r"rotate|restored|imported|replaced|rolled_back|reverted|retired|undone|reset|scaled|"
                           r"activated|deactivated|approved|upgraded|registered|bundle_set|bundle_changed)$")

obs_config = Table(
    "cairn_obs_config", metadata,
    Column("kind", String(16), primary_key=True),          # slo | alert_rule | silence
    Column("id", String(64), primary_key=True),
    Column("doc", JSONText, nullable=False),
    Column("status", JSONText, nullable=True),              # the last evaluation: when, and an error if any
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_by", String(512), nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)
obs_alerts = Table(
    "cairn_obs_alerts", metadata,
    Column("id", String(40), primary_key=True),
    Column("source", String(160), nullable=False, index=True),     # rule:<id> | slo:<id>:<long window>
    Column("fingerprint", String(64), nullable=False),
    Column("name", String(200), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("state", String(16), nullable=False, index=True),       # pending | firing | resolved
    Column("labels", JSONText, nullable=False),
    Column("value", Float, nullable=True),
    Column("summary", Text, nullable=False),
    Column("started_ms", BigInteger, nullable=False),
    Column("firing_ms", BigInteger, nullable=True),
    Column("resolved_ms", BigInteger, nullable=True),
    Column("last_eval_ms", BigInteger, nullable=False),
    Column("notified_ms", BigInteger, nullable=True),
    Column("silenced_by", String(64), nullable=True),
    Column("acknowledged_by", String(512), nullable=True),
    Column("acknowledged_ms", BigInteger, nullable=True),
    Column("incident_id", String(40), nullable=True, index=True),
    Index("ix_cairn_obs_alerts_source_fp", "source", "fingerprint"),
)
obs_incidents = Table(
    "cairn_obs_incidents", metadata,
    Column("id", String(40), primary_key=True),
    Column("group_key", String(160), nullable=True, index=True),
    Column("title", String(300), nullable=False),
    Column("severity", String(16), nullable=False),
    Column("status", String(16), nullable=False, index=True),      # open | acknowledged | resolved
    Column("services", JSONText, nullable=False),
    Column("summary", Text, nullable=True),
    Column("assignee", String(512), nullable=True),
    Column("opened_by", String(512), nullable=False),
    Column("opened_ms", BigInteger, nullable=False),
    Column("acknowledged_by", String(512), nullable=True),
    Column("acknowledged_ms", BigInteger, nullable=True),
    Column("resolved_by", String(512), nullable=True),
    Column("resolved_ms", BigInteger, nullable=True),
    Column("updated_ms", BigInteger, nullable=False),
)
obs_incident_events = Table(
    "cairn_obs_incident_events", metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("incident_id", String(40), nullable=False, index=True),
    Column("ts_ms", BigInteger, nullable=False),
    Column("kind", String(32), nullable=False),     # alert_firing alert_resolved note finding hypothesis action status
    Column("actor", String(512), nullable=False),
    Column("by_agent", Boolean, nullable=False),
    Column("text", Text, nullable=False),
    Column("doc", JSONText, nullable=True),
)
obs_deploys = Table(
    "cairn_obs_deploys", metadata,
    Column("id", String(40), primary_key=True),
    Column("component", String(128), nullable=False, index=True),
    Column("version", String(128), nullable=False),
    Column("description", Text, nullable=True),
    Column("url", String(1024), nullable=True),
    Column("actor", String(512), nullable=False),
    Column("ts_ms", BigInteger, nullable=False, index=True),
)


class ObservabilityError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def seconds(d: str) -> int:
    """``30s``, ``5m``, ``1h``, ``7d``, ``2w`` (also ``1h30m``) in seconds."""
    parts = re.findall(r"(\d+)([smhdw])", d or "")
    if not parts or "".join(n + u for n, u in parts) != (d or "").strip():
        raise ValueError(f"{d!r} is not a duration like 30s, 5m, 1h or 7d")
    return sum(int(n) * _UNITS[u] for n, u in parts)


def _duration(v: str) -> str:
    seconds(v)
    return v


def _iso(ms: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z") if ms else None


def _ms(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        raise ValueError(f"{iso!r} is not a time (ISO 8601, e.g. 2026-11-10T12:00:00Z)")


_SELECTOR = re.compile(r'^\s*([a-zA-Z_][a-zA-Z0-9_]*\s*(=|!=|=~|!~)\s*"(?:[^"\\]|\\.)*"\s*)'
                       r'(,\s*[a-zA-Z_][a-zA-Z0-9_]*\s*(=|!=|=~|!~)\s*"(?:[^"\\]|\\.)*"\s*)*,?\s*$')


# ---- definitions -------------------------------------------------------------------------------------------------
class BurnAlert(BaseModel):
    long: str = Field(description="the long window, e.g. 1h")
    short: str = Field(description="the short window, e.g. 5m (both must burn faster than factor)")
    factor: float = Field(gt=0, le=1000)
    severity: Literal["critical", "warning", "info"] = "critical"

    _d = field_validator("long", "short")(classmethod(lambda cls, v: _duration(v)))


class Indicator(BaseModel):
    type: Literal["ratio", "latency"]
    errors: Optional[str] = Field(None, max_length=4000, description="ratio: PromQL for bad events over [$window]")
    total: Optional[str] = Field(None, max_length=4000, description="ratio: PromQL for all events over [$window]")
    metric: Optional[str] = Field(None, pattern=r"^[a-zA-Z_:][a-zA-Z0-9_:]*$",
                                  description="latency: the histogram (without _bucket)")
    selector: str = Field("", max_length=1000, description='latency: label matchers, e.g. service_name="cairn-api"')
    threshold: Optional[float] = Field(None, gt=0, description="latency: the bucket bound (le) a good event is under")

    @model_validator(mode="after")
    def _shape(self):
        if self.type == "ratio":
            for k in ("errors", "total"):
                v = getattr(self, k)
                if not v or "$window" not in v:
                    raise ValueError(f"a ratio indicator needs {k}: PromQL with [$window] in it")
        else:
            if not self.metric or self.threshold is None:
                raise ValueError("a latency indicator needs metric and threshold")
            if self.selector and not _SELECTOR.match(self.selector):
                raise ValueError('selector: label matchers like service_name="cairn-api", span_kind="SPAN_KIND_SERVER"')
        return self

    def bad_ratio(self, window: str) -> str:
        """PromQL for the share of bad events over ``window``."""
        if self.type == "ratio":
            return f"({self.errors.replace('$window', window)}) / ({self.total.replace('$window', window)})"
        sel = self.selector.strip().rstrip(",")
        le = f'le="{_le(self.threshold)}"'
        bucket = "{" + (sel + "," if sel else "") + le + "}"
        count = "{" + sel + "}"
        return (f"1 - (sum(rate({self.metric}_bucket{bucket}[{window}])) / "
                f"sum(rate({self.metric}_count{count}[{window}])))")


def _le(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


class Slo(BaseModel):
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    service: str = Field(min_length=1, max_length=128, description="the component it is about, e.g. cairn-api")
    indicator: Indicator
    objective: float = Field(gt=0, lt=100, description="the share of good events to keep, in percent, e.g. 99.9")
    window: str = Field("30d", description="the compliance window, e.g. 30d")
    burn_alerts: list[BurnAlert] = Field(default_factory=lambda: [BurnAlert(**b) for b in DEFAULT_BURNS], max_length=8)
    routes: list[str] = Field(default_factory=list, description="notification routes that receive its alerts")
    recipients: list[str] = Field(default_factory=list, description="people told in Cairn")
    labels: dict[str, str] = Field(default_factory=dict)
    open_incident: bool = True
    enabled: bool = True

    _w = field_validator("window")(classmethod(lambda cls, v: _duration(v)))


class AlertRule(BaseModel):
    id: str = Field(pattern=ID)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field("", max_length=2000)
    language: Literal["promql", "logql"] = "promql"
    expr: str = Field(min_length=1, max_length=4000, description="a condition: every series it returns is an alert")
    pending_for: str = Field("0s", description="how long the condition holds before the alert fires (Prometheus's for)")
    severity: Literal["critical", "warning", "info"] = "warning"
    labels: dict[str, str] = Field(default_factory=dict)
    summary: str = Field("", max_length=1000, description="{{ $value }} and {{ $labels.<name> }} are filled in")
    runbook_url: str = Field("", max_length=1000)
    routes: list[str] = Field(default_factory=list, description="notification routes that receive its alerts")
    recipients: list[str] = Field(default_factory=list, description="people told in Cairn")
    open_incident: Optional[bool] = Field(None, description="default: only critical alerts open incidents")
    repeat_interval: str = Field("4h", description="a firing alert nobody acknowledged is sent again this often")
    enabled: bool = True

    _d = field_validator("pending_for", "repeat_interval")(classmethod(lambda cls, v: _duration(v)))


class Matcher(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_.]*$")
    value: str = Field(max_length=500)
    regex: bool = False

    @model_validator(mode="after")
    def _re(self):
        if self.regex:
            try:
                re.compile(self.value)
            except re.error as e:
                raise ValueError(f"matcher {self.name}: not a regular expression ({e})")
        return self


class Silence(BaseModel):
    id: str = Field(pattern=ID)
    matchers: list[Matcher] = Field(min_length=1, max_length=20)
    starts_at: Optional[str] = Field(None, description="ISO 8601; empty: when it is made")
    ends_at: str = Field(description="ISO 8601")
    comment: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _times(self):
        end = _ms(self.ends_at)
        start = _ms(self.starts_at) if self.starts_at else None
        if start is not None and end <= start:
            raise ValueError("ends_at must be after starts_at")
        if end - (start or _now()) > 90 * 86_400_000:
            raise ValueError("a silence lasts at most 90 days (change what alerts instead)")
        return self


MODELS = {"slo": Slo, "alert_rule": AlertRule, "silence": Silence}


def _fingerprint(source: str, labels: dict) -> str:
    return hashlib.sha256(json.dumps([source, sorted(labels.items())]).encode()).hexdigest()[:40]


def _fill(template: str, value: Optional[float], labels: dict) -> str:
    out = re.sub(r"\{\{\s*\$value\s*\}\}", "" if value is None else f"{value:.4g}", template)
    return re.sub(r"\{\{\s*\$labels\.([a-zA-Z0-9_]+)\s*\}\}", lambda m: str(labels.get(m.group(1), "")), out)


class ObservabilityService:
    def __init__(self, engine, backends: Optional[Backends] = None, hub=None, audit=None,
                 public_url: str = "", version: str = ""):
        self.engine = engine
        self.backends = backends or Backends()
        self.hub = hub
        self.audit = audit
        self.public_url = public_url.rstrip("/")
        self.version = version

    # ---- configuration (SLOs, alert rules, silences) -------------------------------------------------------------
    def _audit(self, actor: str, action: str, subject: Optional[str], details: dict) -> None:
        if self.audit is not None:
            self.audit.record("observability", action, actor, subject, details)

    def list(self, kind: str) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(obs_config).where(obs_config.c.kind == kind).order_by(obs_config.c.id)).fetchall()
        return [self._public(kind, r) for r in rows]

    def _public(self, kind: str, r) -> dict:
        doc = dict(r.doc)
        if kind == "silence":
            doc["active"] = self._silence_active(doc, _now())
        return {**doc, "created_by": r.created_by, "updated_by": r.updated_by,
                "updated_at": _iso(r.updated_ms), "last_evaluation": r.status}

    def get(self, kind: str, oid: str) -> dict:
        with self.engine.connect() as c:
            r = c.execute(select(obs_config).where(and_(obs_config.c.kind == kind, obs_config.c.id == oid))).first()
        if r is None:
            raise ObservabilityError(f"no {kind.replace('_', ' ')} {oid}", 404)
        return self._public(kind, r)

    def _docs(self, kind: str) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(r.doc) for r in c.execute(select(obs_config.c.doc).where(obs_config.c.kind == kind))]

    def validate(self, kind: str, body: dict) -> dict:
        try:
            m = MODELS[kind].model_validate(body)
        except Exception as e:  # pydantic's ValidationError: say what is wrong
            raise ObservabilityError(_why(e), 422)
        doc = m.model_dump(mode="json")
        if kind == "slo":            # the indicator as declared (what a bundle or Terraform compares with)
            doc["indicator"] = {k: v for k, v in m.indicator.model_dump(mode="json", exclude_none=True).items()
                                if not (k == "selector" and v == "")}
        if kind in ("slo", "alert_rule"):
            self._check_routes(doc.get("routes") or [])
        return doc

    def _check_routes(self, routes: list[str]) -> None:
        if not routes or self.hub is None:
            return
        known = {r.id for r in self.hub.list_routes()}
        missing = [r for r in routes if r not in known]
        if missing:
            raise ObservabilityError(f"no notification route {', '.join(missing)} (Administration → Notifications)", 422)

    def create(self, kind: str, actor: str, body: dict) -> dict:
        doc = self.validate(kind, body)
        now = _now()
        with self.engine.begin() as c:
            if c.execute(select(obs_config.c.id).where(and_(obs_config.c.kind == kind,
                                                            obs_config.c.id == doc["id"]))).first():
                raise ObservabilityError(f"{kind.replace('_', ' ')} {doc['id']} exists", 409)
            c.execute(insert(obs_config).values(kind=kind, id=doc["id"], doc=doc, status=None, created_by=actor,
                                                created_ms=now, updated_by=actor, updated_ms=now))
        self._audit(actor, f"observability.{kind}_created", doc["id"], {"doc": doc})
        return self.get(kind, doc["id"])

    def replace(self, kind: str, actor: str, oid: str, body: dict) -> dict:
        doc = self.validate(kind, {**body, "id": oid})
        with self.engine.begin() as c:
            n = c.execute(update(obs_config).where(and_(obs_config.c.kind == kind, obs_config.c.id == oid))
                          .values(doc=doc, updated_by=actor, updated_ms=_now())).rowcount
        if not n:
            raise ObservabilityError(f"no {kind.replace('_', ' ')} {oid}", 404)
        self._audit(actor, f"observability.{kind}_updated", oid, {"doc": doc})
        return self.get(kind, oid)

    def remove(self, kind: str, actor: str, oid: str) -> None:
        with self.engine.begin() as c:
            n = c.execute(delete(obs_config).where(and_(obs_config.c.kind == kind, obs_config.c.id == oid))).rowcount
        if not n:
            raise ObservabilityError(f"no {kind.replace('_', ' ')} {oid}", 404)
        if kind in ("slo", "alert_rule"):          # its alerts end with it
            prefix = ("slo:" if kind == "slo" else "rule:") + oid
            for a in self._open_alerts_like(prefix):
                self._resolve(a, _now(), "its definition was deleted")
        self._audit(actor, f"observability.{kind}_deleted", oid, {})

    def _set_status(self, kind: str, oid: str, status: dict) -> None:
        with self.engine.begin() as c:
            c.execute(update(obs_config).where(and_(obs_config.c.kind == kind, obs_config.c.id == oid))
                      .values(status=status))

    # ---- backends ----------------------------------------------------------------------------------------------
    def _prom(self):
        if self.backends.prometheus is None:
            raise ObservabilityError("the metrics backend is not configured: set CAIRN_OBS_PROMETHEUS_URL (Prometheus or compatible)", 503)
        return self.backends.prometheus

    def _query(self, language: str, expr: str) -> list[dict]:
        if language == "logql":
            if self.backends.loki is None:
                raise ObservabilityError("the logs backend is not configured: set CAIRN_OBS_LOKI_URL", 503)
            return self.backends.loki.query(expr)
        return self._prom().query(expr)

    def test_rule(self, body: dict) -> dict:
        """What a rule would match now (its condition, run once): nothing is kept."""
        doc = self.validate("alert_rule", {"id": "test-rule", "name": "test", **body})
        try:
            rows = self._query(doc["language"], doc["expr"])
        except BackendError as e:
            raise ObservabilityError(str(e), e.status)
        return {"matches": [{"labels": r["labels"], "value": r["value"],
                             "summary": _fill(doc["summary"], r["value"], r["labels"])} for r in rows[:100]],
                "count": len(rows)}

    # ---- health map ---------------------------------------------------------------------------------------------
    def health(self, window: str = "5m") -> dict:
        seconds(window)
        prom = self._prom()
        server = 'span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"'
        q = {
            "rate": f"sum by (service_name) (rate(traces_span_metrics_calls_total{{{server}}}[{window}]))",
            "errors": ("sum by (service_name) (rate(traces_span_metrics_calls_total"
                       f'{{{server},status_code="STATUS_CODE_ERROR"}}[{window}]))'),
            "p95": ("histogram_quantile(0.95, sum by (service_name, le) "
                    f"(rate(traces_span_metrics_duration_milliseconds_bucket{{{server}}}[{window}])))"),
            "up": "max by (job) (up)",
            "edges": f"sum by (client, server) (rate(traces_service_graph_request_total[{window}]))",
            "edge_errors": f"sum by (client, server) (rate(traces_service_graph_request_failed_total[{window}]))",
        }
        got: dict[str, Any] = {}
        errors = {}
        for k, expr in q.items():
            try:
                got[k] = prom.query(expr)
            except BackendError as e:
                if k == "rate":
                    raise ObservabilityError(str(e), e.status)
                got[k], errors[k] = [], str(e)
        comps: dict[str, dict] = {}

        def comp(name: str) -> dict:
            return comps.setdefault(name, {"name": name, "rate": None, "error_ratio": None, "p95_ms": None,
                                           "up": None, "targets": 0, "firing_alerts": 0})

        for r in got["rate"]:
            if r["labels"].get("service_name"):
                comp(r["labels"]["service_name"])["rate"] = r["value"]
        for r in got["errors"]:
            c = comp(r["labels"].get("service_name", ""))
            if c["rate"]:
                c["error_ratio"] = (r["value"] or 0) / c["rate"]
        for r in got["p95"]:
            if r["labels"].get("service_name") and r["value"] is not None:
                comp(r["labels"]["service_name"])["p95_ms"] = r["value"]
        for r in got["up"]:
            job = r["labels"].get("job")
            if job:
                c = comp(job)
                c["targets"] += 1
                c["up"] = bool(r["value"])
        firing = [a for a in self.alerts(state="firing") if not a["silenced_by"]]
        for a in firing:
            svc = a["labels"].get("service") or a["labels"].get("service_name") or a["labels"].get("job")
            if svc and svc in comps:
                comps[svc]["firing_alerts"] += 1
        for c in comps.values():
            if c["up"] is False:
                c["state"] = "down"
            elif c["rate"] is None and c["up"] is None:
                c["state"] = "unknown"
            elif (c["error_ratio"] or 0) > 0.05 or c["firing_alerts"]:
                c["state"] = "degraded"
            else:
                c["state"] = "up"
        edges = {}
        for r in got["edges"]:
            key = (r["labels"].get("client", ""), r["labels"].get("server", ""))
            edges[key] = {"from": key[0], "to": key[1], "rate": r["value"], "error_ratio": 0.0}
            comp(key[0])
            comp(key[1])
        for r in got["edge_errors"]:
            key = (r["labels"].get("client", ""), r["labels"].get("server", ""))
            if key in edges and edges[key]["rate"]:
                edges[key]["error_ratio"] = (r["value"] or 0) / edges[key]["rate"]
        for c in comps.values():
            c.setdefault("state", "unknown")
        return {"window": window, "components": sorted(comps.values(), key=lambda c: c["name"]),
                "edges": sorted(edges.values(), key=lambda e: (e["from"], e["to"])), "partial": errors,
                "at": _iso(_now())}

    # ---- journeys and logs --------------------------------------------------------------------------------------
    def traces(self, service: str = "", errors_only: bool = False, min_duration_ms: Optional[int] = None,
               since_minutes: int = 60, limit: int = 20) -> dict:
        if self.backends.tempo is None:
            raise ObservabilityError("the trace backend is not configured: set CAIRN_OBS_TEMPO_URL (or CAIRN_OBS_TRACE_URL for links)", 503)
        tags: dict[str, str] = {}
        if service:
            tags["service.name"] = service
        if errors_only:
            tags["status"] = "error"
        try:
            items = self.backends.tempo.search(tags, min(limit, 100), min_duration_ms, since_minutes * 60)
        except BackendError as e:
            raise ObservabilityError(str(e), e.status)
        return {"items": items}

    def journey(self, trace_id: str) -> dict:
        trace_id = trace_id.strip().lower()
        if not TRACE_ID.match(trace_id):
            raise ObservabilityError("a trace id is 32 hexadecimal digits", 422)
        links = self.backends.links(trace_id)
        if self.backends.tempo is None:
            if links:
                return {"trace_id": trace_id, "source": "link", "links": links, "spans": [], "services": []}
            raise ObservabilityError("the trace backend is not configured: set CAIRN_OBS_TEMPO_URL or CAIRN_OBS_TRACE_URL", 503)
        try:
            doc = self.backends.tempo.trace(trace_id)
        except BackendError as e:
            raise ObservabilityError(str(e), e.status)
        if not doc:
            raise ObservabilityError(f"no trace {trace_id} (not sampled, not yet written, or past retention)", 404)
        spans = _spans(doc)
        if not spans:
            raise ObservabilityError(f"no trace {trace_id}", 404)
        start = min(s["start_ms"] for s in spans)
        end = max(s["start_ms"] + s["duration_ms"] for s in spans)
        for s in spans:
            s["offset_ms"] = round(s["start_ms"] - start, 3)
        services = sorted({s["service"] for s in spans})
        ids = {s["span_id"] for s in spans}
        roots = [s for s in spans if not s["parent_id"] or s["parent_id"] not in ids]
        root = min(roots, key=lambda s: s["start_ms"]) if roots else spans[0]
        return {"trace_id": trace_id, "source": "tempo", "links": links, "root": {"service": root["service"],
                                                                                   "name": root["name"]},
                "start": _iso(int(start)), "duration_ms": round(end - start, 3), "services": services,
                "processes": len({(s["service"], s.get("instance")) for s in spans}),
                "errors": sum(1 for s in spans if s["status"] == "error"),
                "spans": sorted(spans, key=lambda s: s["start_ms"])}

    def logs(self, trace_id: str = "", service: str = "", text: str = "", level: str = "",
             since_minutes: int = 60, limit: int = 200) -> dict:
        """Log lines of a trace (or request id) or a component, newest first; at least one of them is required."""
        if not trace_id and not service:
            raise ObservabilityError("search the logs of a trace (trace_id) or a component (service)", 422)
        if trace_id and not TRACE_ID.match(trace_id.strip().lower()):
            raise ObservabilityError("a trace id is 32 hexadecimal digits", 422)
        if service and not re.match(r"^[A-Za-z0-9_.:-]{1,128}$", service):
            raise ObservabilityError("service: letters, digits and . _ : -", 422)
        if self.backends.loki is None:
            links = self.backends.links(trace_id, service) if trace_id else {}
            if links.get("logs"):
                return {"source": "link", "links": links, "items": []}
            raise ObservabilityError("the logs backend is not configured: set CAIRN_OBS_LOKI_URL or CAIRN_OBS_LOGS_URL", 503)
        sel = f'{{service_name="{service}"}}' if service else '{service_name=~".+"}'
        q = sel
        if text:
            q += " |= " + json.dumps(text[:200])
        if trace_id:
            q += f' | trace_id="{trace_id.strip().lower()}"'
        if level:
            if not re.match(r"^[A-Za-z]{1,16}$", level):
                raise ObservabilityError("level: e.g. ERROR, WARNING, INFO", 422)
            q += f' | severity_text=~"(?i){level}"'
        end = time.time()
        try:
            rows = self.backends.loki.lines(q, end - since_minutes * 60, end + 5, min(limit, 1000))
        except BackendError as e:
            raise ObservabilityError(str(e), e.status)
        items = [{"at": _iso(r["ts_ms"]), "ts_ms": r["ts_ms"],
                  "service": r["labels"].get("service_name"), "level": r["labels"].get("severity_text")
                  or r["labels"].get("detected_level") or r["labels"].get("level"),
                  "trace_id": r["labels"].get("trace_id"), "span_id": r["labels"].get("span_id"),
                  "line": redact.scrub_error(r["line"])} for r in rows]
        return {"source": "loki", "query": q, "items": items}

    # ---- SLOs ---------------------------------------------------------------------------------------------------
    def _bad_ratio(self, slo: dict, window: str) -> Optional[float]:
        ind = Indicator.model_validate(slo["indicator"])
        rows = self._prom().query(ind.bad_ratio(window))
        vals = [r["value"] for r in rows if r["value"] is not None]
        return max(vals) if vals else None

    def slo_status(self, oid: str) -> dict:
        slo = {k: v for k, v in self.get("slo", oid).items()}
        budget = 1 - slo["objective"] / 100
        try:
            bad = self._bad_ratio(slo, slo["window"])
            burns = []
            for b in slo["burn_alerts"]:
                lo, sh = self._bad_ratio(slo, b["long"]), self._bad_ratio(slo, b["short"])
                burns.append({**b, "long_burn": None if lo is None else lo / budget,
                              "short_burn": None if sh is None else sh / budget,
                              "burning": lo is not None and sh is not None and lo / budget > b["factor"]
                              and sh / budget > b["factor"]})
        except BackendError as e:
            raise ObservabilityError(str(e), e.status)
        consumed = None if bad is None else bad / budget
        return {"id": oid, "objective": slo["objective"], "window": slo["window"],
                "sli": None if bad is None else round(100 * (1 - bad), 5),
                "budget_consumed": consumed, "budget_remaining": None if consumed is None else 1 - consumed,
                "burn": burns, "firing": [a for a in self.alerts(state="firing") if a["source"].startswith(f"slo:{oid}:")]}

    def slo_history(self, oid: str, hours: int = 24, points: int = 48) -> dict:
        """The 1-hour burn rate over the last ``hours`` (the budget-burn chart)."""
        slo = self.get("slo", oid)
        budget = 1 - slo["objective"] / 100
        ind = Indicator.model_validate(slo["indicator"])
        end = time.time()
        start = end - hours * 3600
        try:
            rows = self._prom().query_range(f"({ind.bad_ratio('1h')}) / {budget}", start, end,
                                            max((end - start) / max(points, 2), 15))
        except BackendError as e:
            raise ObservabilityError(str(e), e.status)
        series = rows[0]["values"] if rows else []
        return {"id": oid, "burn_1h": [{"at": _iso(int(t * 1000)), "value": v} for t, v in series]}

    # ---- alerts ---------------------------------------------------------------------------------------------------
    def alerts(self, state: Optional[str] = None, limit: int = 200) -> list[dict]:
        stmt = select(obs_alerts).order_by(obs_alerts.c.started_ms.desc()).limit(limit)
        if state == "open":
            stmt = stmt.where(obs_alerts.c.state.in_(("pending", "firing")))
        elif state:
            stmt = stmt.where(obs_alerts.c.state == state)
        with self.engine.connect() as c:
            return [self._alert_public(r._mapping) for r in c.execute(stmt)]

    @staticmethod
    def _alert_public(r) -> dict:
        return {"id": r["id"], "source": r["source"], "name": r["name"], "severity": r["severity"], "state": r["state"],
                "labels": r["labels"], "value": r["value"], "summary": r["summary"],
                "started_at": _iso(r["started_ms"]), "firing_since": _iso(r["firing_ms"]),
                "resolved_at": _iso(r["resolved_ms"]), "silenced_by": r["silenced_by"],
                "acknowledged_by": r["acknowledged_by"], "acknowledged_at": _iso(r["acknowledged_ms"]),
                "incident_id": r["incident_id"]}

    def acknowledge_alert(self, actor: str, aid: str) -> dict:
        with self.engine.begin() as c:
            r = c.execute(select(obs_alerts).where(obs_alerts.c.id == aid)).first()
            if r is None:
                raise ObservabilityError(f"no alert {aid}", 404)
            if r.state != "firing":
                raise ObservabilityError(f"alert {aid} is {r.state}: only a firing alert is acknowledged", 409)
            c.execute(update(obs_alerts).where(obs_alerts.c.id == aid).values(acknowledged_by=actor,
                                                                              acknowledged_ms=_now()))
        if r.incident_id:
            self._event(r.incident_id, "status", actor, f"{actor} acknowledged alert {r.name}", {"alert": aid})
        self._audit(actor, "observability.alert_acknowledged", aid, {"name": r.name})
        with self.engine.connect() as c:
            return self._alert_public(c.execute(select(obs_alerts).where(obs_alerts.c.id == aid)).first()._mapping)

    def _open_alerts_like(self, prefix: str) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(obs_alerts).where(and_(obs_alerts.c.source.like(prefix + "%"),
                                                           obs_alerts.c.state.in_(("pending", "firing"))))).fetchall()
        return [dict(r._mapping) for r in rows if r.source == prefix or r.source.startswith(prefix + ":")]

    # ---- silences ---------------------------------------------------------------------------------------------
    @staticmethod
    def _silence_active(doc: dict, now: int) -> bool:
        start = _ms(doc["starts_at"]) if doc.get("starts_at") else 0
        return start <= now < _ms(doc["ends_at"])

    def _silenced_by(self, labels: dict, now: int) -> Optional[str]:
        for s in self._docs("silence"):
            if not self._silence_active(s, now):
                continue
            if all((re.fullmatch(m["value"], str(labels.get(m["name"], ""))) if m.get("regex")
                    else str(labels.get(m["name"], "")) == m["value"]) for m in s["matchers"]):
                return s["id"]
        return None

    # ---- evaluation ---------------------------------------------------------------------------------------------
    def evaluate(self, now_ms: Optional[int] = None) -> dict:
        """One pass over every enabled rule and SLO (the notifier runs it every CAIRN_OBS_EVALUATE_SECONDS)."""
        now = now_ms or _now()
        out = {"rules": 0, "slos": 0, "errors": 0, "fired": 0, "resolved": 0}
        self._record_own_deploy(now)
        for rule in self._docs("alert_rule"):
            src = f"rule:{rule['id']}"
            if not rule.get("enabled", True):
                out["resolved"] += self._advance(src, rule, {}, now, "the rule is turned off")["resolved"]
                continue
            out["rules"] += 1
            try:
                rows = self._query(rule.get("language", "promql"), rule["expr"])
            except (BackendError, ObservabilityError) as e:   # never resolve what cannot be seen
                out["errors"] += 1
                self._set_status("alert_rule", rule["id"], {"at": _iso(now), "error": str(e)[:300]})
                continue
            active = {}
            for r in rows[:500]:
                labels = {k: str(v) for k, v in r["labels"].items() if k != "__name__"}
                active[_fingerprint(src, labels)] = {"labels": labels, "value": r["value"]}
            self._set_status("alert_rule", rule["id"], {"at": _iso(now), "matches": len(active)})
            n = self._advance(src, {**rule, "severity": rule.get("severity", "warning")}, active, now)
            out["fired"] += n["fired"]
            out["resolved"] += n["resolved"]
        for slo in self._docs("slo"):
            if not slo.get("enabled", True):
                for b in slo.get("burn_alerts") or []:
                    self._advance(f"slo:{slo['id']}:{b['long']}", slo, {}, now, "the SLO is turned off")
                continue
            out["slos"] += 1
            budget = 1 - slo["objective"] / 100
            try:
                ratios: dict[str, Optional[float]] = {}
                for b in slo["burn_alerts"]:
                    for w in (b["long"], b["short"]):
                        if w not in ratios:
                            ratios[w] = self._bad_ratio(slo, w)
            except (BackendError, ObservabilityError) as e:
                out["errors"] += 1
                self._set_status("slo", slo["id"], {"at": _iso(now), "error": str(e)[:300]})
                continue
            burning = []
            for b in slo["burn_alerts"]:
                src = f"slo:{slo['id']}:{b['long']}"
                lo, sh = ratios[b["long"]], ratios[b["short"]]
                active = {}
                if lo is not None and sh is not None and lo / budget > b["factor"] and sh / budget > b["factor"]:
                    labels = {**slo.get("labels", {}), "service": slo["service"], "slo": slo["id"],
                              "window": f"{b['long']}/{b['short']}"}
                    active[_fingerprint(src, labels)] = {"labels": labels, "value": lo / budget}
                    burning.append(b["long"])
                spec = {"name": f"SLO {slo['name']}: error budget burning", "severity": b["severity"],
                        "summary": (f"{slo['service']} burns its {slo['objective']}% / {slo['window']} error budget "
                                    f"{{{{ $value }}}}x too fast over {b['long']} and {b['short']} (alert at {b['factor']}x)"),
                        "routes": slo.get("routes", []), "recipients": slo.get("recipients", []),
                        "open_incident": slo.get("open_incident", True), "pending_for": "0s",
                        "repeat_interval": "4h", "group": f"slo:{slo['id']}"}
                n = self._advance(src, spec, active, now)
                out["fired"] += n["fired"]
                out["resolved"] += n["resolved"]
            self._set_status("slo", slo["id"], {"at": _iso(now), "burning": burning,
                                                "sli": None if ratios.get(slo["burn_alerts"][0]["long"]) is None
                                                else 1 - ratios[slo["burn_alerts"][0]["long"]]})
        return out

    def _advance(self, source: str, spec: dict, active: dict, now: int, why_resolved: str = "") -> dict:
        """Moves the alerts of one source: new matches pend, pending ones fire after pending_for, firing ones
        repeat, and the ones that no longer match resolve."""
        fired = resolved = 0
        with self.engine.connect() as c:
            rows = {r.fingerprint: dict(r._mapping) for r in c.execute(select(obs_alerts).where(and_(
                obs_alerts.c.source == source, obs_alerts.c.state.in_(("pending", "firing")))))}
        for_ms = seconds(spec.get("pending_for") or "0s") * 1000
        for fp, m in active.items():
            labels = {**spec.get("labels", {}), **m["labels"], "alertname": spec["name"], "severity": spec["severity"],
                      "source": source}
            summary = _fill(spec.get("summary") or spec["name"], m["value"], labels)
            a = rows.get(fp)
            if a is None:
                aid = "alt_" + uuid.uuid4().hex[:20]
                with self.engine.begin() as c:
                    c.execute(insert(obs_alerts).values(
                        id=aid, source=source, fingerprint=fp, name=spec["name"][:200], severity=spec["severity"],
                        state="pending", labels=labels, value=m["value"], summary=summary[:4000], started_ms=now,
                        firing_ms=None, resolved_ms=None, last_eval_ms=now, notified_ms=None, silenced_by=None,
                        acknowledged_by=None, acknowledged_ms=None, incident_id=None))
                    a = dict(c.execute(select(obs_alerts).where(obs_alerts.c.id == aid)).first()._mapping)
            else:
                with self.engine.begin() as c:
                    c.execute(update(obs_alerts).where(obs_alerts.c.id == a["id"]).values(
                        value=m["value"], summary=summary[:4000], last_eval_ms=now))
                a.update(value=m["value"], summary=summary)
            if a["state"] == "pending" and now - a["started_ms"] >= for_ms:
                self._fire(a, spec, now)
                fired += 1
            elif a["state"] == "firing":
                self._still_firing(a, spec, now)
        for fp, a in rows.items():
            if fp not in active:
                if a["state"] == "pending":
                    with self.engine.begin() as c:
                        c.execute(delete(obs_alerts).where(obs_alerts.c.id == a["id"]))
                else:
                    self._resolve(a, now, why_resolved)
                    resolved += 1
        return {"fired": fired, "resolved": resolved}

    def _fire(self, a: dict, spec: dict, now: int) -> None:
        silence = self._silenced_by(a["labels"], now)
        values: dict = {"state": "firing", "firing_ms": now, "silenced_by": silence}
        incident = None
        if not silence:
            opens = spec.get("open_incident")
            if opens is None:
                opens = spec["severity"] == "critical"
            if opens:
                incident = self._open_or_join(a, spec, now)
                values["incident_id"] = incident
            values["notified_ms"] = now
        with self.engine.begin() as c:
            c.execute(update(obs_alerts).where(obs_alerts.c.id == a["id"]).values(**values))
        a.update(values)
        self._audit(ACTOR, "observability.alert_fired", a["id"], {"name": a["name"], "severity": a["severity"],
                                                                  "labels": a["labels"], "silenced_by": silence})
        if not silence:
            self._notify("observability.alert_firing", a, spec)

    def _still_firing(self, a: dict, spec: dict, now: int) -> None:
        silence = self._silenced_by(a["labels"], now)
        if silence != a["silenced_by"]:
            with self.engine.begin() as c:
                c.execute(update(obs_alerts).where(obs_alerts.c.id == a["id"]).values(silenced_by=silence))
            a["silenced_by"] = silence
        if silence or a["acknowledged_by"]:
            return
        repeat = seconds(spec.get("repeat_interval") or "4h") * 1000
        if a["notified_ms"] is None or now - a["notified_ms"] >= repeat:
            values: dict = {"notified_ms": now}
            if a["notified_ms"] is None and a["incident_id"] is None:      # a silence ended while it fired
                opens = spec.get("open_incident")
                if opens if opens is not None else spec["severity"] == "critical":
                    values["incident_id"] = self._open_or_join(a, spec, now)
            with self.engine.begin() as c:
                c.execute(update(obs_alerts).where(obs_alerts.c.id == a["id"]).values(**values))
            a.update(values)
            self._notify("observability.alert_firing", a, spec)

    def _resolve(self, a: dict, now: int, why: str = "") -> None:
        with self.engine.begin() as c:
            c.execute(update(obs_alerts).where(obs_alerts.c.id == a["id"]).values(state="resolved", resolved_ms=now))
        a.update(state="resolved", resolved_ms=now)
        self._audit(ACTOR, "observability.alert_resolved", a["id"], {"name": a["name"], **({"why": why} if why else {})})
        if a.get("incident_id"):
            self._event(a["incident_id"], "alert_resolved", ACTOR,
                        f"{a['name']} resolved" + (f" ({why})" if why else ""), {"alert": a["id"]})
        if a.get("notified_ms") and not a.get("silenced_by"):
            spec = self._spec_of(a["source"])
            self._notify("observability.alert_resolved", a, spec or {})

    def _spec_of(self, source: str) -> Optional[dict]:
        kind, _, rest = source.partition(":")
        oid = rest.split(":")[0]
        for d in self._docs("alert_rule" if kind == "rule" else "slo"):
            if d["id"] == oid:
                return d
        return None

    def _notify(self, type_: str, a: dict, spec: dict) -> None:
        if self.hub is None:
            return
        from cairn.notify.hub import Event

        firing = type_.endswith("firing")
        title = (f"[{a['severity'].upper()}] {a['name']}" if firing else f"Resolved: {a['name']}")
        body = a["summary"] + (f"\nRunbook: {spec['runbook_url']}" if spec.get("runbook_url") else "")
        try:
            self.hub.publish(Event(
                type=type_, title=title[:300], body=body[:3000], actor=ACTOR, recipients=list(spec.get("recipients") or []),
                url=f"{self.public_url}/cairn/observability?alert={a['id']}" if self.public_url else None,
                payload={"alert": a["id"], "severity": a["severity"], "labels": a["labels"], "value": a["value"],
                         "incident": a.get("incident_id")},
                routes=list(spec.get("routes") or [])))
        except Exception:  # noqa: BLE001 - a notification problem never stops evaluation
            logger.exception("could not publish %s for %s", type_, a["id"])

    # ---- incidents ----------------------------------------------------------------------------------------------
    def _open_or_join(self, a: dict, spec: dict, now: int) -> str:
        group = spec.get("group") or a["source"]
        services = sorted({v for k, v in a["labels"].items() if k in ("service", "service_name", "job") and v})
        with self.engine.begin() as c:
            r = c.execute(select(obs_incidents).where(and_(obs_incidents.c.group_key == group,
                                                           obs_incidents.c.status != "resolved"))).first()
            if r is not None:
                iid = r.id
                merged = sorted(set(r.services or []) | set(services))
                c.execute(update(obs_incidents).where(obs_incidents.c.id == iid).values(services=merged, updated_ms=now))
                created = False
            else:
                iid = "inc_" + uuid.uuid4().hex[:16]
                c.execute(insert(obs_incidents).values(
                    id=iid, group_key=group, title=a["name"][:300], severity=a["severity"], status="open",
                    services=services, summary=a["summary"][:4000], assignee=None, opened_by=ACTOR, opened_ms=now,
                    acknowledged_by=None, acknowledged_ms=None, resolved_by=None, resolved_ms=None, updated_ms=now))
                created = True
        self._event(iid, "alert_firing", ACTOR, f"{a['name']} fired: {a['summary']}",
                    {"alert": a["id"], "severity": a["severity"], "labels": a["labels"]}, now)
        if created:
            self._audit(ACTOR, "observability.incident_opened", iid, {"title": a["name"], "alert": a["id"]})
            if self.hub is not None:
                from cairn.notify.hub import Event

                try:
                    self.hub.publish(Event(
                        type="observability.incident_opened", title=f"Incident: {a['name']}"[:300],
                        body=a["summary"][:3000], actor=ACTOR, recipients=list(spec.get("recipients") or []),
                        url=f"{self.public_url}/cairn/observability/incidents/{iid}" if self.public_url else None,
                        payload={"incident": iid, "severity": a["severity"]}, routes=list(spec.get("routes") or [])))
                except Exception:  # noqa: BLE001
                    logger.exception("could not publish the incident %s", iid)
        return iid

    def _event(self, iid: str, kind: str, actor: str, text: str, doc: Optional[dict] = None,
               now: Optional[int] = None, by_agent: bool = False) -> None:
        with self.engine.begin() as c:
            c.execute(insert(obs_incident_events).values(incident_id=iid, ts_ms=now or _now(), kind=kind, actor=actor,
                                                         by_agent=by_agent, text=redact.scrub_text(text)[:8000],
                                                         doc=doc or {}))
            c.execute(update(obs_incidents).where(obs_incidents.c.id == iid).values(updated_ms=now or _now()))

    @staticmethod
    def _incident_public(r) -> dict:
        return {"id": r["id"], "title": r["title"], "severity": r["severity"], "status": r["status"],
                "services": r["services"] or [], "summary": r["summary"], "assignee": r["assignee"],
                "group": r["group_key"], "opened_by": r["opened_by"], "opened_at": _iso(r["opened_ms"]),
                "acknowledged_by": r["acknowledged_by"], "acknowledged_at": _iso(r["acknowledged_ms"]),
                "resolved_by": r["resolved_by"], "resolved_at": _iso(r["resolved_ms"]), "updated_at": _iso(r["updated_ms"])}

    def incidents(self, status: Optional[str] = None, limit: int = 100) -> list[dict]:
        stmt = select(obs_incidents).order_by(obs_incidents.c.opened_ms.desc()).limit(limit)
        if status == "active":
            stmt = stmt.where(obs_incidents.c.status != "resolved")
        elif status:
            stmt = stmt.where(obs_incidents.c.status == status)
        with self.engine.connect() as c:
            return [self._incident_public(r._mapping) for r in c.execute(stmt)]

    def _incident_row(self, iid: str) -> dict:
        with self.engine.connect() as c:
            r = c.execute(select(obs_incidents).where(obs_incidents.c.id == iid)).first()
        if r is None:
            raise ObservabilityError(f"no incident {iid}", 404)
        return dict(r._mapping)

    def open_incident(self, actor: str, body: dict, by_agent: bool = False) -> dict:
        title = str(body.get("title") or "").strip()
        severity = body.get("severity", "warning")
        if not title or len(title) > 300:
            raise ObservabilityError("an incident needs a title (up to 300 characters)", 422)
        if severity not in SEVERITIES:
            raise ObservabilityError(f"severity: one of {', '.join(SEVERITIES)}", 422)
        services = [str(s)[:128] for s in body.get("services") or []][:20]
        now = _now()
        iid = "inc_" + uuid.uuid4().hex[:16]
        with self.engine.begin() as c:
            c.execute(insert(obs_incidents).values(
                id=iid, group_key=None, title=title, severity=severity, status="open", services=services,
                summary=str(body.get("summary") or "")[:4000] or None, assignee=body.get("assignee"),
                opened_by=actor, opened_ms=now, acknowledged_by=None, acknowledged_ms=None, resolved_by=None,
                resolved_ms=None, updated_ms=now))
        self._event(iid, "status", actor, f"opened by {actor}", {"status": "open"}, now, by_agent)
        self._audit(actor, "observability.incident_opened", iid, {"title": title})
        return self.incident(iid)

    def incident(self, iid: str) -> dict:
        inc = self._incident_public(self._incident_row(iid))
        with self.engine.connect() as c:
            alerts = [self._alert_public(r._mapping) for r in c.execute(
                select(obs_alerts).where(obs_alerts.c.incident_id == iid).order_by(obs_alerts.c.started_ms))]
        inc["alerts"] = alerts
        inc["timeline"] = self.timeline(iid)
        return inc

    def timeline(self, iid: str) -> list[dict]:
        """What happened (alerts, notes, status) and what changed around it (the audit log's changes,
        infrastructure-as-code change requests applied, deploys), oldest first."""
        row = self._incident_row(iid)
        start = row["opened_ms"] - 2 * 3_600_000
        end = row["resolved_ms"] or _now()
        with self.engine.connect() as c:
            events = [{"at": _iso(r.ts_ms), "ts_ms": r.ts_ms, "kind": r.kind, "actor": r.actor, "by_agent": r.by_agent,
                       "text": r.text, "detail": r.doc or {}}
                      for r in c.execute(select(obs_incident_events).where(obs_incident_events.c.incident_id == iid)
                                         .order_by(obs_incident_events.c.seq))]
        for ch in self.changes(start, end):
            events.append(ch)
        return sorted(events, key=lambda e: e["ts_ms"])

    def changes(self, start_ms: int, end_ms: int, limit: int = 100) -> list[dict]:
        """Changes between two times: audit entries that changed something, IaC change requests applied, deploys."""
        out: list[dict] = []
        from cairn.common.audit import audit_log

        with self.engine.connect() as c:
            for r in c.execute(select(audit_log.c.ts, audit_log.c.category, audit_log.c.action, audit_log.c.actor,
                                      audit_log.c.subject).where(and_(audit_log.c.ts >= _iso(start_ms)[:19],
                                                                      audit_log.c.ts <= _iso(end_ms + 1000)))
                               .order_by(audit_log.c.seq.desc()).limit(2000)):
                if r.category == "observability" or not CHANGE_ACTION.search(r.action):
                    continue
                ts = _ms(r.ts) if r.ts else start_ms
                out.append({"at": _iso(ts), "ts_ms": ts, "kind": "change", "actor": r.actor, "by_agent": False,
                            "text": f"{r.action} {r.subject or ''}".strip(), "detail": {"category": r.category}})
                if len(out) >= limit:
                    break
            try:
                from cairn.iac.changes import changes as iac_changes

                for r in c.execute(select(iac_changes.c.id, iac_changes.c.title, iac_changes.c.applied_by,
                                          iac_changes.c.applied_ms, iac_changes.c.status).where(and_(
                                              iac_changes.c.applied_ms >= start_ms, iac_changes.c.applied_ms <= end_ms))):
                    out.append({"at": _iso(r.applied_ms), "ts_ms": r.applied_ms, "kind": "iac_change",
                                "actor": r.applied_by or "", "by_agent": False,
                                "text": f"change request {r.id} applied: {r.title}", "detail": {"change": r.id,
                                                                                               "status": r.status}})
            except Exception:  # noqa: BLE001 - the IaC tables may not exist in a slimmed-down install
                pass
            for r in c.execute(select(obs_deploys).where(and_(obs_deploys.c.ts_ms >= start_ms,
                                                              obs_deploys.c.ts_ms <= end_ms))):
                out.append({"at": _iso(r.ts_ms), "ts_ms": r.ts_ms, "kind": "deploy", "actor": r.actor,
                            "by_agent": False, "text": f"{r.component} {r.version} deployed"
                            + (f": {r.description}" if r.description else ""), "detail": {"url": r.url}})
        return out

    def note(self, actor: str, iid: str, body: dict, by_agent: bool = False) -> dict:
        kind = body.get("kind", "note")
        if kind not in ("note", "finding", "hypothesis", "action"):
            raise ObservabilityError("kind: note, finding, hypothesis or action", 422)
        text = str(body.get("text") or "").strip()
        if not text or len(text) > 8000:
            raise ObservabilityError("a note needs text (up to 8,000 characters)", 422)
        self._incident_row(iid)
        detail = body.get("detail") if isinstance(body.get("detail"), dict) else {}
        self._event(iid, kind, actor, text, json.loads(redact.scrub_text(json.dumps(detail)))
                    if detail else {}, by_agent=by_agent)
        return self.incident(iid)

    def set_incident(self, actor: str, iid: str, action: str, body: dict, by_agent: bool = False) -> dict:
        row = self._incident_row(iid)
        now = _now()
        values: dict[str, Any] = {"updated_ms": now}
        if action == "acknowledge":
            if row["status"] != "open":
                raise ObservabilityError(f"incident {iid} is {row['status']}", 409)
            values.update(status="acknowledged", acknowledged_by=actor, acknowledged_ms=now)
            text = f"acknowledged by {actor}"
        elif action == "resolve":
            if row["status"] == "resolved":
                raise ObservabilityError(f"incident {iid} is resolved", 409)
            summary = str(body.get("summary") or "").strip()
            values.update(status="resolved", resolved_by=actor, resolved_ms=now,
                          **({"summary": summary[:4000]} if summary else {}))
            text = f"resolved by {actor}" + (f": {summary}" if summary else "")
        elif action == "reopen":
            if row["status"] != "resolved":
                raise ObservabilityError(f"incident {iid} is not resolved", 409)
            values.update(status="open", resolved_by=None, resolved_ms=None)
            text = f"reopened by {actor}"
        elif action == "assign":
            who = body.get("assignee")
            if who is not None and not str(who).startswith("urn:li:"):
                raise ObservabilityError("assignee: a person or agent (urn:li:corpuser:...)", 422)
            values["assignee"] = who
            text = f"assigned to {who}" if who else "unassigned"
        else:
            raise ObservabilityError(f"unknown action {action}", 404)
        with self.engine.begin() as c:
            c.execute(update(obs_incidents).where(obs_incidents.c.id == iid).values(**values))
        self._event(iid, "status", actor, text, {"action": action}, now, by_agent)
        self._audit(actor, f"observability.incident_{action}", iid, {})
        if action == "resolve" and self.hub is not None:
            from cairn.notify.hub import Event

            try:
                self.hub.publish(Event(type="observability.incident_resolved", title=f"Resolved: {row['title']}"[:300],
                                       body=text[:3000], actor=actor, payload={"incident": iid}))
            except Exception:  # noqa: BLE001
                logger.exception("could not publish the resolution of %s", iid)
        return self.incident(iid)

    def incident_context(self, iid: str) -> dict:
        """Everything one needs to work on an incident, in one answer: the incident, its alerts and timeline (with
        the changes around it), the health of the components involved, recent error traces and error logs of
        those components, and the SLOs about them. Each part that cannot be read says why instead."""
        inc = self.incident(iid)
        services = list(inc["services"])
        out: dict = {"incident": inc, "services": services}
        try:
            h = self.health()
            out["health"] = [c for c in h["components"] if not services or c["name"] in services] or h["components"]
            out["edges"] = [e for e in h["edges"] if not services or e["from"] in services or e["to"] in services]
        except ObservabilityError as e:
            out["health"] = {"error": str(e)}
        traces: list = []
        try:
            for s in services[:5] or [""]:
                traces += self.traces(service=s, errors_only=True, since_minutes=180, limit=5)["items"]
            out["error_traces"] = traces
        except ObservabilityError as e:
            out["error_traces"] = {"error": str(e)}
        logs: list = []
        try:
            for s in services[:5]:
                logs += self.logs(service=s, level="ERROR", since_minutes=180, limit=20)["items"]
            out["error_logs"] = logs
        except ObservabilityError as e:
            out["error_logs"] = {"error": str(e)}
        out["slos"] = [s for s in self.list("slo") if not services or s["service"] in services]
        return out

    # ---- deploys ------------------------------------------------------------------------------------------------
    def record_deploy(self, actor: str, body: dict) -> dict:
        comp, version = str(body.get("component") or "").strip(), str(body.get("version") or "").strip()
        if not comp or not version or len(comp) > 128 or len(version) > 128:
            raise ObservabilityError("a deploy names its component and version", 422)
        url = body.get("url")
        if url and not re.match(r"^https?://", str(url)):
            raise ObservabilityError("url: an http(s) link", 422)
        did = "dep_" + uuid.uuid4().hex[:16]
        with self.engine.begin() as c:
            c.execute(insert(obs_deploys).values(id=did, component=comp, version=version,
                                                 description=str(body.get("description") or "")[:2000] or None,
                                                 url=url, actor=actor, ts_ms=_now()))
        self._audit(actor, "observability.deploy_recorded", did, {"component": comp, "version": version})
        return {"id": did, "component": comp, "version": version}

    def deploys(self, limit: int = 100) -> list[dict]:
        with self.engine.connect() as c:
            return [{"id": r.id, "component": r.component, "version": r.version, "description": r.description,
                     "url": r.url, "actor": r.actor, "at": _iso(r.ts_ms)}
                    for r in c.execute(select(obs_deploys).order_by(obs_deploys.c.ts_ms.desc()).limit(limit))]

    def _record_own_deploy(self, now: int) -> None:
        """Cairn's own version, once when it changes (the first evaluation after an upgrade)."""
        if not self.version:
            return
        with self.engine.begin() as c:
            last = c.execute(select(obs_deploys.c.version).where(obs_deploys.c.component == "cairn")
                             .order_by(obs_deploys.c.ts_ms.desc()).limit(1)).scalar()
            if last != self.version:
                c.execute(insert(obs_deploys).values(id="dep_" + uuid.uuid4().hex[:16], component="cairn",
                                                     version=self.version, description="Cairn started at this version",
                                                     url=None, actor=ACTOR, ts_ms=now))

    # ---- overview -----------------------------------------------------------------------------------------------
    def overview(self) -> dict:
        firing = [a for a in self.alerts(state="firing") if not a["silenced_by"]]
        return {"backends": self.backends.summary(),
                "firing": {s: sum(1 for a in firing if a["severity"] == s) for s in SEVERITIES},
                "open_incidents": len(self.incidents(status="active")),
                "slos": len(self._docs("slo")), "alert_rules": len(self._docs("alert_rule")),
                "silences_active": sum(1 for s in self._docs("silence") if self._silence_active(s, _now()))}


def _why(e: Exception) -> str:
    errs = getattr(e, "errors", None)
    if callable(errs):
        try:
            return "; ".join(f"{'.'.join(str(x) for x in er.get('loc', ())) or 'body'}: {er.get('msg')}"
                             for er in errs()[:5])
        except Exception:  # noqa: BLE001
            pass
    return str(e)[:500]


def _attr_value(v: dict) -> Any:
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            return int(v[k]) if k == "intValue" else v[k]
    if "arrayValue" in v:
        return [_attr_value(x) for x in v["arrayValue"].get("values", [])]
    return None


def _attrs(lst: list) -> dict:
    return {a["key"]: _attr_value(a.get("value", {})) for a in lst or []}


def _hex(v: str) -> str:
    """Tempo answers ids in base64 (OTLP JSON from protobuf) or hex: hex either way."""
    if not v:
        return ""
    if re.fullmatch(r"[0-9a-fA-F]{16}|[0-9a-fA-F]{32}", v):
        return v.lower()
    import base64

    try:
        return base64.b64decode(v).hex()
    except ValueError:
        return v


_KINDS = {1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer",
          "SPAN_KIND_INTERNAL": "internal", "SPAN_KIND_SERVER": "server", "SPAN_KIND_CLIENT": "client",
          "SPAN_KIND_PRODUCER": "producer", "SPAN_KIND_CONSUMER": "consumer"}


def _spans(doc: dict) -> list[dict]:
    """OTLP JSON (Tempo's answer) as flat spans, cleaned again (whatever wrote them)."""
    out = []
    for rs in doc.get("resourceSpans") or doc.get("batches") or []:
        res = _attrs((rs.get("resource") or {}).get("attributes"))
        service = str(res.get("service.name", "unknown"))
        instance = res.get("service.instance.id") or res.get("host.name") or res.get("k8s.pod.name")
        for ss in rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []:
            for sp in ss.get("spans", []):
                start = int(sp.get("startTimeUnixNano", 0)) / 1e6
                end = int(sp.get("endTimeUnixNano", 0)) / 1e6
                status = (sp.get("status") or {}).get("code")
                kind = _KINDS.get(sp.get("kind"), "internal")
                attrs = redact.scrub_attributes(_attrs(sp.get("attributes")), server=kind == "server")
                out.append({
                    "span_id": _hex(sp.get("spanId", "")), "parent_id": _hex(sp.get("parentSpanId", "")),
                    "service": service, "instance": instance, "name": redact.scrub_text(sp.get("name", "")),
                    "kind": kind, "start_ms": start, "duration_ms": round(max(end - start, 0), 3),
                    "status": "error" if status in (2, "STATUS_CODE_ERROR") else "ok",
                    "status_message": redact.scrub_error((sp.get("status") or {}).get("message", "") or "") or None,
                    "attributes": attrs,
                    "events": [{"name": e.get("name"), "offset_ms": round(int(e.get("timeUnixNano", 0)) / 1e6 - start, 3),
                                "attributes": {k: (redact.scrub_error(v) if isinstance(v, str) else v)
                                               for k, v in redact.scrub_attributes(_attrs(e.get("attributes"))).items()}}
                               for e in sp.get("events", [])][:20],
                    "links": [{"trace_id": _hex(lk.get("traceId", "")), "span_id": _hex(lk.get("spanId", ""))}
                              for lk in sp.get("links", [])][:20],
                })
    return out
