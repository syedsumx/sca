"""Ops tiles: dashboards (``cairn.sqlworkspace.boards``) that show what Prometheus, Loki and Tempo hold, SLOs,
alerts and managed PostgreSQL instances, next to (or instead of) SQL tiles. One dashboard system: an ops tile is a
tile with one of these keys instead of ``query``/``element``/``metric``:

* ``promql``: ``{"queries": [{"expr", "legend"}], "instant": false, "step": "30s"}``: a range (or instant) query.
* ``logql``: ``{"queries": [{"expr", "legend"}], "mode": "logs"|"metric", "limit": 100, "step": null}``: log lines,
  or a LogQL metric query over the range (``mode`` follows the first expression when not given).
* ``traces``: ``{"query": "<TraceQL>", "limit": 20}``: matching traces, each linked to its request journey.
* ``slo``: ``{"id": "<slo id>"}``: the SLO's SLI, budget left and burn rates now, and its 1-hour burn over the range.
* ``alerts``: ``{"state": "firing"|"open"|..., "labels": {"service": "cairn-api"}, "severity": null}``.
* ``postgres``: ``{"instance": "<name or $instance>"}``: an instance's numbers (``GET /v1/databases/instances/{name}
  /metrics``), as its viewer may read them.

A tile's ``view`` (timeseries, area, bars, stat, gauge, table, logs, heatmap) and ``display`` (``unit``,
``decimals``, ``min``, ``max``, ``thresholds``: ``[{"value": null, "color": "green"}, {"value": 0.05, "color":
"red"}]``) say how it is drawn. Dashboard controls of kind ``variable`` (``$service``) and ``time`` (the range and the
auto-refresh) feed the queries; ``$__interval``, ``$__rate_interval`` and ``$__range`` are always there.

Every query goes through Cairn, as the viewer, never from the browser to a backend: ops tiles need
``VIEW_OPERATIONS`` (read-only telemetry) or ``MANAGE_SYSTEM_OPERATIONS``, decided by the policy engine at every
render and every save; a decision that cannot be had refuses (fail closed). A ``postgres`` tile instead needs what
the instance's metrics need (``VIEW_DATABASE_INSTANCE``), decided by that API. Limits protect the backends:
``CAIRN_OPS_MAX_RANGE`` (7d), ``CAIRN_OPS_MAX_POINTS`` (per series, 1500), ``CAIRN_OPS_MIN_STEP`` (5s),
``CAIRN_OPS_MAX_SERIES`` (50), ``CAIRN_OPS_MAX_LOG_LINES`` (500), ``CAIRN_OPS_MAX_TRACES`` (50),
``CAIRN_OPS_QUERY_TIMEOUT`` (20s), ``CAIRN_OPS_MAX_CONCURRENT`` (8 backend queries at once per process) and
``CAIRN_OPS_MIN_REFRESH`` (10s).
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from typing import Any, Callable, Optional

from cairn.observability import redact
from cairn.observability.backends import TRACE_ID, BackendError

logger = logging.getLogger(__name__)

SOURCES = ("promql", "logql", "traces", "slo", "alerts", "postgres")
VIEWS = ("timeseries", "area", "bars", "stat", "gauge", "table", "logs", "heatmap")
DEFAULT_VIEW = {"promql": "timeseries", "logql": "logs", "traces": "table", "slo": "stat", "alerts": "table",
                "postgres": "stat"}
PRIVILEGES = ("VIEW_OPERATIONS", "MANAGE_SYSTEM_OPERATIONS")
UNITS = ("none", "short", "percent", "percentunit", "s", "ms", "bytes", "decbytes", "bps", "Bps", "reqps", "ops",
         "cps", "rps", "wps", "iops", "count")
COLORS = ("green", "red", "orange", "yellow", "blue", "purple", "grey", "text")
RELATIVE = re.compile(r"^(\d{1,4})([smhdw])$")
VAR_VALUE = re.compile(r"^[A-Za-z0-9_.:/*|+ @-]{0,200}$")    # nothing that leaves a label matcher's string
VAR_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
INSTANCE = re.compile(r"^(\$[A-Za-z_][A-Za-z0-9_]*|[a-z0-9][a-z0-9-]{0,62})$")
_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class OpsError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def seconds(d: str) -> int:
    m = RELATIVE.match(str(d or "").strip())
    if not m:
        raise OpsError(f"{d!r}: a duration such as 30s, 5m, 1h, 7d", 422)
    return int(m.group(1)) * _UNIT_S[m.group(2)]


def _dur(s: float) -> str:
    s = max(1, int(math.ceil(s)))
    for u, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s % n == 0:
            return f"{s // n}{u}"
    return f"{s}s"


class Limits:
    """What one ops query may ask of a backend (``CAIRN_OPS_*``)."""

    def __init__(self, env: Optional[dict] = None):
        env = env if env is not None else os.environ

        def num(name: str, default: int) -> int:
            try:
                return max(1, int(env.get(name, default)))
            except (TypeError, ValueError):
                return default

        self.max_range_s = seconds(env.get("CAIRN_OPS_MAX_RANGE", "7d"))
        self.min_step_s = seconds(env.get("CAIRN_OPS_MIN_STEP", "5s"))
        self.max_points = num("CAIRN_OPS_MAX_POINTS", 1500)
        self.max_series = num("CAIRN_OPS_MAX_SERIES", 50)
        self.max_log_lines = num("CAIRN_OPS_MAX_LOG_LINES", 500)
        self.max_traces = num("CAIRN_OPS_MAX_TRACES", 50)
        self.timeout_s = seconds(env.get("CAIRN_OPS_QUERY_TIMEOUT", "20s"))
        self.max_concurrent = num("CAIRN_OPS_MAX_CONCURRENT", 8)
        self.min_refresh_s = seconds(env.get("CAIRN_OPS_MIN_REFRESH", "10s"))

    def public(self) -> dict:
        return {"max_range": _dur(self.max_range_s), "min_step": _dur(self.min_step_s), "max_points": self.max_points,
                "max_series": self.max_series, "max_log_lines": self.max_log_lines, "max_traces": self.max_traces,
                "query_timeout": _dur(self.timeout_s), "max_concurrent": self.max_concurrent,
                "min_refresh": _dur(self.min_refresh_s)}


# ---- validation (structural: a dashboard saves without its backends) -------------------------------------------------
def source_of(tile: dict) -> Optional[str]:
    found = [k for k in SOURCES if tile.get(k) is not None]
    if len(found) > 1:
        raise OpsError(f"a tile has one source, not {', '.join(found)}")
    return found[0] if found else None


def _text(v: Any, what: str, n: int = 4000, required: bool = True) -> str:
    if v is None and not required:
        return ""
    if not isinstance(v, str) or (required and not v.strip()) or len(v) > n:
        raise OpsError(f"{what}: text of at most {n} characters" + (", not empty" if required else ""))
    return v.strip()


def _queries(spec: dict, what: str) -> list[dict]:
    raw = spec.get("queries")
    if raw is None and spec.get("expr") is not None:              # shorthand: one expression
        raw = [{"expr": spec.get("expr"), "legend": spec.get("legend", "")}]
    if not isinstance(raw, list) or not 1 <= len(raw) <= 8:
        raise OpsError(f"{what}.queries: 1 to 8 {{expr, legend}}")
    out = []
    for i, q in enumerate(raw):
        if not isinstance(q, dict):
            raise OpsError(f"{what}.queries[{i}]: {{expr, legend}}")
        out.append({"expr": _text(q.get("expr"), f"{what}.queries[{i}].expr"),
                    "legend": _text(q.get("legend"), f"{what}.queries[{i}].legend", 200, required=False)})
    return out


def _step(v: Any, what: str) -> Optional[str]:
    if v in (None, "", "auto"):
        return None
    seconds(v)
    return str(v)


def validate_tile(t: dict) -> dict:
    """An ops tile in its stored form (raises OpsError): ``{<source>: {...}, "view", "display"}``."""
    src = source_of(t)
    if src is None:
        raise OpsError("not an ops tile")
    spec = t[src]
    if not isinstance(spec, dict):
        raise OpsError(f"{src}: an object")
    if src == "promql":
        clean: dict[str, Any] = {"queries": _queries(spec, src), "instant": bool(spec.get("instant", False)),
                                 "step": _step(spec.get("step"), f"{src}.step")}
    elif src == "logql":
        qs = _queries(spec, src)
        mode = spec.get("mode") or ("logs" if qs[0]["expr"].lstrip().startswith("{") else "metric")
        if mode not in ("logs", "metric"):
            raise OpsError("logql.mode: logs or metric")
        limit = spec.get("limit", 100)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5000:
            raise OpsError("logql.limit: 1 to 5000 lines")
        clean = {"queries": qs, "mode": mode, "limit": limit, "step": _step(spec.get("step"), f"{src}.step")}
    elif src == "traces":
        limit = spec.get("limit", 20)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise OpsError("traces.limit: 1 to 500 traces")
        clean = {"query": _text(spec.get("query"), "traces.query"), "limit": limit}
    elif src == "slo":
        sid = _text(spec.get("id"), "slo.id", 64)
        if not re.match(r"^(\$[A-Za-z_][A-Za-z0-9_]*|[a-z0-9][a-z0-9\-]{1,62})$", sid):
            raise OpsError("slo.id: an SLO's id (or a $variable)")
        clean = {"id": sid}
    elif src == "alerts":
        state = spec.get("state") or "firing"
        if state not in ("open", "pending", "firing", "resolved"):
            raise OpsError("alerts.state: open, pending, firing or resolved")
        labels = spec.get("labels") or {}
        if not isinstance(labels, dict) or len(labels) > 10 or not all(
                isinstance(k, str) and LABEL.match(k) and isinstance(v, str) and len(v) <= 200 for k, v in labels.items()):
            raise OpsError("alerts.labels: up to 10 {label: value} (values may be $variables)")
        severity = spec.get("severity")
        if severity not in (None, "critical", "warning", "info"):
            raise OpsError("alerts.severity: critical, warning or info")
        clean = {"state": state, "labels": labels, "severity": severity}
    else:
        inst = _text(spec.get("instance"), "postgres.instance", 64)
        if not INSTANCE.match(inst):
            raise OpsError("postgres.instance: an instance's name (or a $variable)")
        clean = {"instance": inst}
    view = t.get("view") or DEFAULT_VIEW[src]
    if view not in VIEWS:
        raise OpsError(f"view is one of {', '.join(VIEWS)}")
    if view == "logs" and not (src == "logql" and clean["mode"] == "logs"):
        raise OpsError("the logs view is for a logql tile in logs mode")
    return {src: clean, "view": view, "display": _display(t.get("display"))}


def _display(d: Any) -> dict:
    d = d or {}
    if not isinstance(d, dict):
        raise OpsError("display: {unit, decimals, min, max, thresholds}")
    out: dict[str, Any] = {}
    unit = d.get("unit")
    if unit is not None:
        if not isinstance(unit, str) or not re.match(r"^[A-Za-z0-9_:./%-]{1,32}$", unit):
            raise OpsError(f"display.unit: one of {', '.join(UNITS)} (or a Grafana unit id)")
        out["unit"] = unit
    if d.get("decimals") is not None:
        if not isinstance(d["decimals"], int) or isinstance(d["decimals"], bool) or not 0 <= d["decimals"] <= 6:
            raise OpsError("display.decimals: 0 to 6")
        out["decimals"] = d["decimals"]
    for k in ("min", "max"):
        if d.get(k) is not None:
            if not isinstance(d[k], (int, float)) or isinstance(d[k], bool):
                raise OpsError(f"display.{k}: a number")
            out[k] = d[k]
    steps = d.get("thresholds")
    if steps is not None:
        if not isinstance(steps, list) or len(steps) > 10:
            raise OpsError("display.thresholds: up to 10 {value, color}")
        clean = []
        for s in steps:
            if not isinstance(s, dict) or not (s.get("value") is None or (isinstance(s.get("value"), (int, float))
                                                                           and not isinstance(s.get("value"), bool))):
                raise OpsError("display.thresholds: {value: a number (null: the base), color}")
            color = s.get("color") or "green"
            if color not in COLORS and not re.match(r"^#[0-9a-fA-F]{6}$", str(color)):
                raise OpsError(f"display.thresholds color: one of {', '.join(COLORS)} or #rrggbb")
            clean.append({"value": s.get("value"), "color": color})
        clean.sort(key=lambda s: -math.inf if s["value"] is None else s["value"])
        out["thresholds"] = clean
    return out


def validate_control(c: dict, cid: str) -> dict:
    """A ``variable`` or ``time`` control in its stored form."""
    kind = c.get("kind")
    label = str(c.get("label") or cid)[:128]
    if kind == "time":
        default = c.get("default") or "1h"
        seconds(default)
        refresh = c.get("refresh") or 0
        if not isinstance(refresh, int) or isinstance(refresh, bool) or refresh < 0 or refresh > 86400:
            raise OpsError(f"{cid}: refresh is seconds between refreshes (0: off)")
        return {"id": cid, "label": label, "kind": "time", "default": default, "refresh": refresh,
                "column": "", "targets": None, "source": None}
    options = c.get("options")
    if options is not None and (not isinstance(options, list) or len(options) > 500 or
                                not all(isinstance(o, str) and VAR_VALUE.match(o) for o in options)):
        raise OpsError(f"{cid}: options are up to 500 plain values")
    q = c.get("query")
    if q is not None:
        if not isinstance(q, dict) or q.get("source") not in ("label_values", "loki_label_values", "postgres_instances",
                                                               "slos"):
            raise OpsError(f"{cid}: query.source is label_values, loki_label_values, postgres_instances or slos")
        if q["source"] in ("label_values", "loki_label_values") and not LABEL.match(str(q.get("label") or "")):
            raise OpsError(f"{cid}: query.label is a label name")
        q = {"source": q["source"], **({"label": q["label"]} if q.get("label") else {}),
             **({"match": _text(q.get("match"), f"{cid}: query.match", 500)} if q.get("match") else {})}
    default = c.get("default")
    if default is not None and (not isinstance(default, str) or not VAR_VALUE.match(default)):
        raise OpsError(f"{cid}: default is a plain value (letters, digits and . _ : / * | + - @)")
    return {"id": cid, "label": label, "kind": "variable", "default": default, "options": options, "query": q,
            "multi": bool(c.get("multi", False)), "column": "", "targets": None, "source": None}


# ---- variables and the time range ------------------------------------------------------------------------------------
def window(controls: list[dict], values: dict, now: Optional[float] = None) -> tuple[float, float]:
    """The range the dashboard shows: the time control's value (``"6h"`` or ``[from_ms, to_ms]``) or its default."""
    now = now or time.time()
    tc = next((c for c in controls if c.get("kind") == "time"), None)
    v = values.get(tc["id"]) if tc else values.get("time")
    v = v if v not in (None, "") else (tc["default"] if tc else "1h")
    if isinstance(v, str):
        return now - seconds(v), now
    if isinstance(v, list) and len(v) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
        start, end = v[0] / 1000, v[1] / 1000
        if not 0 < start < end:
            raise OpsError("time: [from_ms, to_ms], from before to", 422)
        return start, end
    raise OpsError('time: a range such as "1h", or [from_ms, to_ms]', 422)


def variables(controls: list[dict], values: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for c in controls:
        if c.get("kind") != "variable":
            continue
        v = values.get(c["id"], c.get("default"))
        if isinstance(v, list):
            v = "|".join(str(x) for x in v) if c.get("multi") else (str(v[0]) if v else "")
        v = "" if v is None else str(v)
        if not VAR_VALUE.match(v):
            raise OpsError(f"${c['id']}: letters, digits and . _ : / * | + - @ only", 422)
        out[c["id"]] = v
    return out


def interpolate(text: str, vars_: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        return vars_[name] if name in vars_ else m.group(0)

    return VAR_REF.sub(sub, text)


def legend(template: str, labels: dict, expr: str) -> str:
    if template:
        return re.sub(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", lambda m: str(labels.get(m.group(1), "")), template)
    shown = {k: v for k, v in labels.items() if k != "__name__"}
    if not shown:
        return labels.get("__name__") or expr[:80]
    return (labels.get("__name__", "") + "{" + ", ".join(f'{k}="{v}"' for k, v in sorted(shown.items())) + "}")


# ---- running tiles ---------------------------------------------------------------------------------------------------
class OpsTiles:
    """Runs ops tiles as a viewer: the privilege first, then the limits, then the backend."""

    def __init__(self, observability, authorize: Callable[[str, str], bool], pg: Optional[Callable[[], Any]] = None,
                 env: Optional[dict] = None):
        """``authorize(actor, privilege)`` decides (False when it cannot); ``pg()`` is the managed PostgreSQL
        instances service, when this Cairn has one."""
        self.obs = observability
        self._authorize = authorize
        self.pg = pg or (lambda: None)
        self.limits = Limits(env)
        self._slots = threading.BoundedSemaphore(self.limits.max_concurrent)

    # ---- who ---------------------------------------------------------------------------------------------------
    def allowed(self, actor: str) -> bool:
        for p in PRIVILEGES:
            try:
                if self._authorize(actor, p):
                    return True
            except Exception:  # noqa: BLE001 - fail closed: a decision that cannot be had is a refusal
                logger.warning("could not decide %s for %s", p, actor, exc_info=True)
        return False

    def require(self, actor: str) -> None:
        if not self.allowed(actor):
            raise OpsError("ops tiles need VIEW_OPERATIONS (or MANAGE_SYSTEM_OPERATIONS)", 403)

    # ---- one tile ----------------------------------------------------------------------------------------------
    def run(self, actor: str, tile: dict, vars_: dict[str, str], start: float, end: float) -> dict:
        src = source_of(tile)
        spec = tile[src]
        if src != "postgres":
            self.require(actor)
        span = end - start
        if span > self.limits.max_range_s:
            raise OpsError(f"the range is {_dur(span)}; at most {_dur(self.limits.max_range_s)} (CAIRN_OPS_MAX_RANGE)", 422)
        builtins = self._builtins(spec, start, end)
        allv = {**builtins, **vars_}
        if not self._slots.acquire(timeout=self.limits.timeout_s):
            raise OpsError("too many ops queries at once here; try again in a moment", 429)
        from cairn.observability.service import ObservabilityError

        try:
            return getattr(self, f"_{src}")(actor, spec, allv, start, end, builtins)
        except (BackendError, ObservabilityError) as e:
            raise OpsError(str(e), e.status)
        finally:
            self._slots.release()

    def _builtins(self, spec: dict, start: float, end: float) -> dict[str, str]:
        step = self.step(spec.get("step") if isinstance(spec, dict) else None, start, end)
        return {"__interval": _dur(step), "__rate_interval": _dur(max(4 * step, 60)), "__range": _dur(end - start),
                "__range_s": str(int(end - start))}

    def step(self, want: Optional[str], start: float, end: float) -> float:
        span = max(end - start, 1)
        auto = max(self.limits.min_step_s, math.ceil(span / 250))
        step = max(seconds(want), self.limits.min_step_s) if want else auto
        if span / step > self.limits.max_points:
            raise OpsError(f"{int(span / step)} points per series; at most {self.limits.max_points} "
                           f"(raise the step to at least {_dur(span / self.limits.max_points)})", 422)
        return float(step)

    def _cap(self, series: list, what: str) -> None:
        if len(series) > self.limits.max_series:
            raise OpsError(f"{what} returns {len(series)} series; at most {self.limits.max_series} "
                           f"(aggregate it, e.g. sum by (...) or topk)", 422)

    def _series(self, rows: list[dict], q: dict, expr: str) -> list[dict]:
        return [{"name": legend(q["legend"], r["labels"], expr), "labels": r["labels"],
                 "points": [[int(t * 1000), v] for t, v in r["values"]]} for r in rows]

    def _promql(self, actor, spec, vars_, start, end, builtins) -> dict:
        prom = self.obs._prom()
        step = self.step(spec.get("step"), start, end)
        out: list[dict] = []
        for q in spec["queries"]:
            expr = interpolate(q["expr"], vars_)
            if spec.get("instant"):
                rows = prom.query(expr, end)
                self._cap(rows, "the query")
                out += [{"name": legend(q["legend"], r["labels"], expr), "labels": r["labels"],
                         "points": [[int(end * 1000), r["value"]]]} for r in rows]
            else:
                rows = prom.query_range(expr, start, end, step, timeout=f"{self.limits.timeout_s}s")
                self._cap(rows, "the query")
                out += self._series(rows, q, expr)
            self._cap(out, "the tile")
        return {"kind": "series", "instant": bool(spec.get("instant")), "series": out, "step_s": step,
                "start_ms": int(start * 1000), "end_ms": int(end * 1000)}

    def _loki(self):
        if self.obs.backends.loki is None:
            raise OpsError("the logs backend is not configured: set CAIRN_OBS_LOKI_URL", 503)
        return self.obs.backends.loki

    def _logql(self, actor, spec, vars_, start, end, builtins) -> dict:
        loki = self._loki()
        if spec["mode"] == "metric":
            step = self.step(spec.get("step"), start, end)
            out: list[dict] = []
            for q in spec["queries"]:
                expr = interpolate(q["expr"], vars_)
                rows = loki.query_range(expr, start, end, step)
                self._cap(rows, "the query")
                out += self._series(rows, q, expr)
                self._cap(out, "the tile")
            return {"kind": "series", "instant": False, "series": out, "step_s": step,
                    "start_ms": int(start * 1000), "end_ms": int(end * 1000)}
        limit = min(spec["limit"], self.limits.max_log_lines)
        lines: list[dict] = []
        for q in spec["queries"]:
            lines += loki.lines(interpolate(q["expr"], vars_), start, end, limit)
        lines.sort(key=lambda r: r["ts_ms"], reverse=True)
        items = [{"ts_ms": r["ts_ms"], "service": r["labels"].get("service_name"),
                  "level": r["labels"].get("severity_text") or r["labels"].get("detected_level") or r["labels"].get("level"),
                  "trace_id": r["labels"].get("trace_id"), "line": redact.scrub_error(r["line"])} for r in lines[:limit]]
        return {"kind": "logs", "lines": items, "truncated": len(lines) > limit}

    def _traces(self, actor, spec, vars_, start, end, builtins) -> dict:
        tempo = self.obs.backends.tempo
        if tempo is None:
            raise OpsError("the trace backend is not configured: set CAIRN_OBS_TEMPO_URL", 503)
        limit = min(spec["limit"], self.limits.max_traces)
        rows = tempo.traceql(interpolate(spec["query"], vars_), start, end, limit)
        return {"kind": "traces", "traces": [{**r, "journey": f"/observability?tab=journey&trace={r['trace_id']}"}
                                             for r in rows[:limit] if TRACE_ID.match(r["trace_id"])]}

    def _slo(self, actor, spec, vars_, start, end, builtins) -> dict:
        from cairn.observability.service import Indicator, ObservabilityError

        sid = interpolate(spec["id"], vars_)
        if not sid or sid.startswith("$"):
            raise OpsError(f"pick an SLO ({spec['id']} has no value)", 422)
        try:
            status = self.obs.slo_status(sid)
            slo = self.obs.get("slo", sid)
        except ObservabilityError as e:
            raise OpsError(str(e), e.status)
        budget = 1 - slo["objective"] / 100
        ind = Indicator.model_validate(slo["indicator"])
        step = self.step(None, start, end)
        rows = self.obs._prom().query_range(f"({ind.bad_ratio('1h')}) / {budget}", start, end, step,
                                            timeout=f"{self.limits.timeout_s}s")
        series = [{"name": "1h burn rate", "labels": {}, "points": [[int(t * 1000), v] for t, v in r["values"]]}
                  for r in rows[:1]]
        return {"kind": "slo", "slo": {"id": sid, "name": slo["name"], "service": slo.get("service")},
                "status": {k: status[k] for k in ("objective", "window", "sli", "budget_consumed", "budget_remaining",
                                                  "burn")}, "firing": len(status["firing"]), "series": series,
                "start_ms": int(start * 1000), "end_ms": int(end * 1000)}

    def _alerts(self, actor, spec, vars_, start, end, builtins) -> dict:
        want = {k: interpolate(v, vars_) for k, v in spec["labels"].items()}
        rows = []
        for a in self.obs.alerts(spec["state"], 1000):
            if spec.get("severity") and a["severity"] != spec["severity"]:
                continue
            if all(_matches(a["labels"].get(k, ""), v) for k, v in want.items()):
                rows.append({k: a[k] for k in ("id", "name", "severity", "state", "labels", "value", "summary",
                                                "firing_since", "started_at", "silenced_by", "acknowledged_by",
                                                "incident_id")})
        return {"kind": "alerts", "alerts": rows[:200], "total": len(rows)}

    def _postgres(self, actor, spec, vars_, start, end, builtins) -> dict:
        name = interpolate(spec["instance"], vars_)
        if not re.match(r"^[a-z0-9][a-z0-9-]{0,62}$", name):
            raise OpsError("pick an instance ($instance has no value)" if name.startswith("$") else
                           f"{name!r} is not an instance name", 422)
        svc = self.pg()
        if svc is None or not hasattr(svc, "metrics"):
            raise OpsError("managed PostgreSQL instances (GET /v1/databases/instances/{name}/metrics) are not "
                           "available on this Cairn", 501)
        try:
            m = svc.metrics(actor, name)
        except Exception as e:  # noqa: BLE001 - that API's own refusal (403, 404, 409) shows in the tile
            if not hasattr(e, "status"):
                raise
            raise OpsError(str(e), getattr(e, "status", 502))
        return {"kind": "postgres", "instance": name, **postgres_view(m)}

    # ---- a variable's choices ----------------------------------------------------------------------------------
    def options(self, actor: str, control: dict, start: Optional[float] = None) -> list[str]:
        q = control.get("query")
        if not q:
            return list(control.get("options") or [])
        if q["source"] == "postgres_instances":
            svc = self.pg()
            if svc is None or not hasattr(svc, "list"):
                return []
            return [i["name"] for i in svc.list(actor) if isinstance(i, dict) and i.get("name")]
        self.require(actor)
        if q["source"] == "slos":
            return [s["id"] for s in self.obs.list("slo")]
        start = start or time.time() - 6 * 3600
        try:
            if q["source"] == "loki_label_values":
                vals = self._loki().label_values(q["label"], start)
            else:
                prom = self.obs._prom()
                vals = prom.label_values(q["label"], q.get("match", ""), start)
        except BackendError as e:
            raise OpsError(str(e), e.status)
        except Exception as e:  # noqa: BLE001 - an unconfigured backend (ObservabilityError)
            if hasattr(e, "status"):
                raise OpsError(str(e), e.status)
            raise
        return sorted(v for v in vals if VAR_VALUE.match(v))[:500]


def _matches(have: str, want: str) -> bool:
    if any(ch in want for ch in "*|+") or ".*" in want:
        try:
            return re.fullmatch(want, have) is not None
        except re.error:
            return False
    return have == want


def postgres_view(m: dict) -> dict:
    """An instance's metrics as tiles show them: headline values, per-database rows, and any time series the API
    answers (``series``: [{name, labels, points: [[ms, v]]}])."""
    conns = m.get("connections") or {}
    dbs = m.get("databases") or []
    reads = sum((d.get("blocks_read") or 0) + (d.get("blocks_hit") or 0) for d in dbs)
    hits = sum(d.get("blocks_hit") or 0 for d in dbs)
    values = [
        {"name": "connections", "value": conns.get("active", conns.get("total")) if isinstance(conns, dict) else conns},
        {"name": "max connections", "value": conns.get("max") if isinstance(conns, dict) else None},
        {"name": "cache hit ratio", "value": round(hits / reads, 4) if reads else None, "unit": "percentunit"},
        {"name": "commits", "value": sum(d.get("commits") or 0 for d in dbs)},
        {"name": "rollbacks", "value": sum(d.get("rollbacks") or 0 for d in dbs)},
        {"name": "deadlocks", "value": sum(d.get("deadlocks") or 0 for d in dbs)},
        {"name": "locks waiting", "value": m.get("locks_waiting")},
        {"name": "longest transaction", "value": m.get("longest_transaction_s"), "unit": "s"},
        {"name": "size", "value": sum(d.get("size_bytes") or 0 for d in dbs) or None, "unit": "bytes"},
        {"name": "uptime", "value": m.get("uptime_s"), "unit": "s"},
    ]
    series = m.get("series") if isinstance(m.get("series"), list) else []
    return {"values": [v for v in values if v["value"] is not None],
            "databases": [{k: d.get(k) for k in ("database", "connections", "commits", "rollbacks", "cache_hit_ratio",
                                                  "deadlocks", "temp_files", "size_bytes")} for d in dbs],
            "replication": m.get("replication"), "at_ms": m.get("at_ms"), "series": series}


def build_ops_router(ops: OpsTiles, current_principal) -> Any:
    """``/v1/observability/tiles``: an ops tile tried before it is saved (the tile editor's preview), a variable's
    choices while it is being defined, and the limits. The same privilege and limits as on a dashboard."""
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/observability/tiles", tags=["observability"])

    def call(fn):
        try:
            return fn()
        except OpsError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/limits")
    def limits(p=Depends(current_principal)):
        return {**ops.limits.public(), "allowed": ops.allowed(p.urn), "sources": list(SOURCES), "views": list(VIEWS),
                "units": list(UNITS)}

    @router.post("/preview")
    async def preview(body: dict = Body(...), p=Depends(current_principal)):
        """``{"tile": {...}, "controls": [...], "values": {"time": "1h", "service": "cairn-api"}}``."""
        def run():
            tile = body.get("tile")
            if not isinstance(tile, dict):
                raise OpsError("tile: an ops tile", 422)
            if source_of(tile) != "postgres":
                ops.require(p.urn)
            controls = [validate_control(c, str(c.get("id") or "")) for c in body.get("controls") or []
                        if isinstance(c, dict) and c.get("kind") in ("variable", "time")]
            values = body.get("values") if isinstance(body.get("values"), dict) else {}
            clean = validate_tile(tile)
            start, end = window(controls, values)
            return {"ops": ops.run(p.urn, clean, variables(controls, values), start, end),
                    "window": {"start_ms": int(start * 1000), "end_ms": int(end * 1000)}}

        return await run_in_threadpool(lambda: call(run))

    @router.post("/options")
    async def options(body: dict = Body(...), p=Depends(current_principal)):
        """``{"control": {"kind": "variable", "query": {...}}}``: the choices it would offer."""
        def run():
            c = body.get("control")
            if not isinstance(c, dict):
                raise OpsError("control: a variable control", 422)
            return {"options": ops.options(p.urn, validate_control({**c, "kind": "variable"},
                                                                   str(c.get("id") or "v")))}

        return await run_in_threadpool(lambda: call(run))

    return router
