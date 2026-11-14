"""Where Cairn reads telemetry back: Prometheus (or anything that answers its query API: VictoriaMetrics, Mimir,
Grafana Cloud, Thanos), Loki and Tempo (or Grafana Cloud's). Vendors without those APIs (Datadog, Honeycomb,
New Relic) are linked to instead: ``CAIRN_OBS_TRACE_URL`` and ``CAIRN_OBS_LOGS_URL`` are URL templates with
``{trace_id}`` (and ``{service}``, ``{start_ms}``, ``{end_ms}``).

Each backend is configured by ``CAIRN_OBS_<NAME>_URL`` (``PROMETHEUS``, ``LOKI``, ``TEMPO``) and authenticated
from a file, never the command line: ``CAIRN_OBS_<NAME>_TOKEN_FILE`` (a bearer token), or
``CAIRN_OBS_<NAME>_BASIC_AUTH_FILE`` (``user:password``, Grafana Cloud's instance id and API key), or
``CAIRN_OBS_<NAME>_HEADERS_FILE`` (``name=value`` lines); ``CAIRN_OBS_LOKI_TENANT`` / ``CAIRN_OBS_TEMPO_TENANT``
set ``X-Scope-OrgID`` for multi-tenant installs. Files are re-read when they change (rotated Secrets).
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any, Optional

import httpx

TIMEOUT = httpx.Timeout(20.0, connect=5.0)
MAX_BYTES = 64 << 20          # an answer is read up to this many bytes, and no further (a caller may ask for less)
TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


class BackendError(RuntimeError):
    """A backend did not answer, or refused: the caller says so (502) rather than show nothing as healthy."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


class _Backend:
    name = ""

    def __init__(self, url: str, env: Optional[dict] = None, client: Optional[httpx.Client] = None):
        self.url = url.rstrip("/")
        self.env = env if env is not None else os.environ
        self._client = client

    def _headers(self) -> dict:
        n = self.name.upper()
        h: dict[str, str] = {"Accept": "application/json"}
        if self.env.get(f"CAIRN_OBS_{n}_TOKEN_FILE"):
            h["Authorization"] = "Bearer " + _read(self.env[f"CAIRN_OBS_{n}_TOKEN_FILE"])
        elif self.env.get(f"CAIRN_OBS_{n}_BASIC_AUTH_FILE"):
            h["Authorization"] = "Basic " + base64.b64encode(_read(self.env[f"CAIRN_OBS_{n}_BASIC_AUTH_FILE"]).encode()).decode()
        if self.env.get(f"CAIRN_OBS_{n}_HEADERS_FILE"):
            for line in _read(self.env[f"CAIRN_OBS_{n}_HEADERS_FILE"]).splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    h[k.strip()] = v.strip()
        if self.env.get(f"CAIRN_OBS_{n}_TENANT"):
            h["X-Scope-OrgID"] = self.env[f"CAIRN_OBS_{n}_TENANT"]
        return h

    def _get(self, path: str, params: Optional[dict] = None, timeout_s: Optional[float] = None,
             max_bytes: Optional[int] = None) -> Any:
        """GET and parse JSON. ``timeout_s``: the client's timeout for this call (else TIMEOUT); ``max_bytes``: the
        answer is streamed and abandoned past this size (else MAX_BYTES), never loaded whole first."""
        limit = max_bytes or MAX_BYTES
        timeout = httpx.Timeout(float(timeout_s), connect=min(5.0, float(timeout_s))) if timeout_s else TIMEOUT
        try:
            client = self._client or httpx.Client(timeout=TIMEOUT)
            try:
                with client.stream("GET", self.url + path, params=params, headers=self._headers(),
                                   timeout=timeout) as r:
                    status = r.status_code
                    declared = r.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > limit:
                        raise BackendError(f"{self.name} answered {int(declared)} bytes; at most {limit} are read "
                                           "(narrow the query)", 422)
                    body = bytearray()
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) > limit:
                            raise BackendError(f"{self.name} answered more than {limit} bytes; at most {limit} are "
                                               "read (narrow the query)", 422)
            finally:
                if self._client is None:
                    client.close()
        except OSError as e:
            raise BackendError(f"{self.name} cannot be read: {e}"[:300])
        except httpx.HTTPError as e:
            raise BackendError(f"{self.name} did not answer: {type(e).__name__}")
        if status == 404:
            return None
        if status in (401, 403):
            raise BackendError(f"{self.name} refused Cairn's credentials ({status})")
        if status >= 300:
            raise BackendError(f"{self.name} answered {status}: {bytes(body[:200]).decode('utf-8', 'replace')}")
        try:
            return json.loads(bytes(body))
        except ValueError:
            raise BackendError(f"{self.name} did not answer JSON")


class Prometheus(_Backend):
    name = "prometheus"

    def query(self, expr: str, at: Optional[float] = None, timeout_s: Optional[float] = None,
              max_bytes: Optional[int] = None) -> list[dict]:
        """An instant query: [{labels, value}] (a scalar comes back as one unlabelled sample). ``timeout_s``: also
        Prometheus's own ``timeout`` for the evaluation, so it stops working when Cairn stops waiting."""
        params = {"query": expr, **({"time": f"{at:.3f}"} if at else {}), **_prom_timeout(timeout_s)}
        return _samples(self._get("/api/v1/query", params, timeout_s, max_bytes), self.name)

    def query_range(self, expr: str, start: float, end: float, step: float, timeout_s: Optional[float] = None,
                    max_bytes: Optional[int] = None) -> list[dict]:
        params = {"query": expr, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": f"{max(step, 1):.0f}s",
                  **_prom_timeout(timeout_s)}
        return _matrix(self._get("/api/v1/query_range", params, timeout_s, max_bytes), self.name)

    def label_values(self, label: str, match: str = "", start: Optional[float] = None) -> list[str]:
        """The values a label takes (in series matching ``match``, when given): a dashboard variable's choices."""
        params: dict[str, Any] = {}
        if match:
            params["match[]"] = match
        if start:
            params["start"] = f"{start:.3f}"
        return _values(self._get(f"/api/v1/label/{label}/values", params), self.name)


class Loki(_Backend):
    name = "loki"

    # Loki's HTTP API takes no per-request timeout (its limit is limits_config.query_timeout, server side):
    # ``timeout_s`` bounds the client's wait, ``max_bytes`` what is read.
    def query_range(self, expr: str, start: float, end: float, step: float, timeout_s: Optional[float] = None,
                    max_bytes: Optional[int] = None) -> list[dict]:
        """A LogQL metric query over a range: [{labels, values: [(t, v)]}] (as Prometheus answers)."""
        return _matrix(self._get("/loki/api/v1/query_range", {"query": expr, "start": str(int(start * 1e9)),
                                                              "end": str(int(end * 1e9)), "step": f"{max(step, 1):.0f}s"},
                                 timeout_s, max_bytes), self.name)

    def label_values(self, label: str, start: Optional[float] = None) -> list[str]:
        return _values(self._get(f"/loki/api/v1/label/{label}/values",
                                 {"start": str(int(start * 1e9))} if start else None), self.name)

    def query(self, expr: str, at: Optional[float] = None) -> list[dict]:
        """A LogQL metric query, instant: [{labels, value}]."""
        doc = self._get("/loki/api/v1/query", {"query": expr, **({"time": str(int(at * 1e9))} if at else {})})
        return _samples(doc, self.name)

    def lines(self, expr: str, start: float, end: float, limit: int = 200, timeout_s: Optional[float] = None,
              max_bytes: Optional[int] = None) -> list[dict]:
        """Log lines, newest first: [{ts_ms, labels, line}] (labels include structured metadata)."""
        doc = self._get("/loki/api/v1/query_range", {"query": expr, "start": str(int(start * 1e9)),
                                                     "end": str(int(end * 1e9)), "limit": str(limit),
                                                     "direction": "backward"}, timeout_s, max_bytes)
        if not doc or doc.get("status") != "success":
            raise BackendError(f"{self.name}: {(doc or {}).get('error', 'no answer')}"[:300], 422)
        out = []
        for stream in doc["data"].get("result", []):
            labels = stream.get("stream", {})
            for v in stream.get("values", []):
                meta = v[2] if len(v) > 2 and isinstance(v[2], dict) else {}
                flat = {**(meta.get("structuredMetadata") or {}), **(meta.get("parsed") or {})} if meta else {}
                out.append({"ts_ms": int(v[0]) // 1_000_000, "labels": {**labels, **flat}, "line": v[1]})
        out.sort(key=lambda e: e["ts_ms"], reverse=True)
        return out[:limit]


class Tempo(_Backend):
    name = "tempo"

    def trace(self, trace_id: str) -> Optional[dict]:
        """The trace as OTLP JSON (``{"resourceSpans"|"batches": [...]}``), or None when Tempo has no such trace."""
        doc = self._get(f"/api/traces/{trace_id}")
        if doc is None:
            return None
        return doc.get("trace", doc) if isinstance(doc, dict) else None

    def search(self, tags: dict[str, str], limit: int = 20, min_duration_ms: Optional[int] = None,
               since_s: int = 3600) -> list[dict]:
        """Recent traces with these resource/span tags: [{trace_id, root_service, root_name, start_ms, duration_ms}]."""
        now = int(time.time())
        params: dict[str, Any] = {"limit": str(limit), "start": str(now - since_s), "end": str(now)}
        if tags:
            params["tags"] = " ".join(f"{k}={_logfmt(v)}" for k, v in tags.items())
        if min_duration_ms:
            params["minDuration"] = f"{int(min_duration_ms)}ms"
        doc = self._get("/api/search", params) or {}
        return [{"trace_id": t.get("traceID", "").rjust(32, "0"), "root_service": t.get("rootServiceName"),
                 "root_name": t.get("rootTraceName"), "start_ms": int(t.get("startTimeUnixNano", 0)) // 1_000_000,
                 "duration_ms": t.get("durationMs", 0)} for t in doc.get("traces", [])]


    def traceql(self, q: str, start: float, end: float, limit: int = 20, timeout_s: Optional[float] = None,
                max_bytes: Optional[int] = None) -> list[dict]:
        """A TraceQL search: [{trace_id, root_service, root_name, start_ms, duration_ms}], newest first. Tempo's
        search API takes no per-request timeout (its limit is the query frontend's, server side): ``timeout_s``
        bounds the client's wait, ``max_bytes`` what is read; ``spss`` keeps each trace's spans to a few."""
        doc = self._get("/api/search", {"q": q, "start": str(int(start)), "end": str(int(end) + 1), "limit": str(limit),
                                        "spss": "3"}, timeout_s, max_bytes)
        if doc is None:
            raise BackendError(f"{self.name} has no TraceQL search here", 501)
        out = [{"trace_id": t.get("traceID", "").rjust(32, "0"), "root_service": t.get("rootServiceName"),
                "root_name": t.get("rootTraceName"), "start_ms": int(t.get("startTimeUnixNano", 0)) // 1_000_000,
                "duration_ms": t.get("durationMs", 0)} for t in doc.get("traces", [])]
        out.sort(key=lambda t: t["start_ms"], reverse=True)
        return out


def _prom_timeout(timeout_s: Optional[float]) -> dict:
    """Prometheus's ``timeout`` parameter: its evaluation stops at the client's timeout (a second less, so the
    backend answers "timeout" before the client gives up, when it can)."""
    if not timeout_s:
        return {}
    return {"timeout": f"{max(1, int(timeout_s) - 1)}s"}


def _matrix(doc: Any, name: str) -> list[dict]:
    if not doc or doc.get("status") != "success":
        raise BackendError(f"{name}: {(doc or {}).get('error', 'no answer')}"[:300], 422)
    data = doc["data"]
    if data.get("resultType", "matrix") != "matrix":
        raise BackendError(f"{name}: a range query answers a matrix, not a {data.get('resultType')}", 422)
    return [{"labels": r.get("metric", {}), "values": [(float(t), _num(v)) for t, v in r.get("values", [])]}
            for r in data.get("result", [])]


def _values(doc: Any, name: str) -> list[str]:
    if not doc or doc.get("status") != "success":
        raise BackendError(f"{name}: {(doc or {}).get('error', 'no answer')}"[:300], 422)
    return [str(v) for v in doc.get("data") or []]


def _logfmt(v: str) -> str:
    return v if re.match(r"^[A-Za-z0-9_.:/-]+$", v) else '"' + v.replace('"', '\\"') + '"'


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f          # NaN: no data (0/0)


def _samples(doc: Any, name: str) -> list[dict]:
    if not doc or doc.get("status") != "success":
        raise BackendError(f"{name}: {(doc or {}).get('error', 'no answer')}"[:300], 422)
    data = doc["data"]
    if data.get("resultType") == "scalar":
        return [{"labels": {}, "value": _num(data["result"][1])}]
    out = []
    for r in data.get("result", []):
        if "value" in r:
            out.append({"labels": r.get("metric", {}), "value": _num(r["value"][1])})
        elif r.get("values"):
            out.append({"labels": r.get("metric", {}), "value": _num(r["values"][-1][1])})
    return out


class Backends:
    """The configured backends (each None when not configured) and the vendor link templates."""

    def __init__(self, env: Optional[dict] = None, client: Optional[httpx.Client] = None):
        env = env if env is not None else dict(os.environ)
        self.env = env
        p, lk, t = env.get("CAIRN_OBS_PROMETHEUS_URL"), env.get("CAIRN_OBS_LOKI_URL"), env.get("CAIRN_OBS_TEMPO_URL")
        self.prometheus = Prometheus(p, env, client) if p else None
        self.loki = Loki(lk, env, client) if lk else None
        self.tempo = Tempo(t, env, client) if t else None
        self.trace_url = env.get("CAIRN_OBS_TRACE_URL", "")
        self.logs_url = env.get("CAIRN_OBS_LOGS_URL", "")
        self.vendor = env.get("CAIRN_OBS_VENDOR", "")       # what the links lead to (Datadog, Honeycomb, ...)

    def links(self, trace_id: str, service: str = "", start_ms: int = 0, end_ms: int = 0) -> dict:
        vals = {"trace_id": trace_id, "service": service, "start_ms": start_ms, "end_ms": end_ms}
        out = {}
        if self.trace_url:
            out["trace"] = self.trace_url.format(**vals)
        if self.logs_url:
            out["logs"] = self.logs_url.format(**vals)
        return out

    def summary(self) -> dict:
        return {"prometheus": bool(self.prometheus), "loki": bool(self.loki), "tempo": bool(self.tempo),
                "trace_links": bool(self.trace_url), "log_links": bool(self.logs_url), "vendor": self.vendor or None}
