"""Model monitoring: what a served model is asked and answers, how far that drifts from what it was trained on,
and how right it is once the truth is known, with an alert when it drifts.

Every call to a model through Cairn (``POST /v1/ml/serve/<model>/invocations``) is logged: when, by whom,
its status, latency and number of rows, and an id returned as ``X-Cairn-Inference-Id``. A model's managers
(its owner, ``MANAGE_ML_MODEL``) turn monitoring on with ``PUT /v1/ml/models/<model>/monitor``:

* ``baseline``: what normal looks like. ``{"table": "<namespace>.<table>"}`` (the training table, read from
  the lakehouse by Cairn) or ``{"since_ms": ..., "until_ms": ...}`` (a reference period of the model's own
  logged calls). ``features`` (default: every input column) are compared with it.
* Each check (every ``every_minutes``, default 60, on the scheduler; or ``POST .../monitor/run``) takes the
  calls of the last ``window_minutes`` (default 1440) and computes, per feature, the population stability
  index (PSI: ten quantile bins of the baseline for numbers, its 20 most common values for anything else),
  the same for the predictions against a reference period, the error rate and latency (p50, p95), and,
  for calls with labels, accuracy (classes) or mean absolute error (numbers).
* A feature (or the predictions) with PSI above ``drift_threshold`` (default 0.2) is drifting; with fewer
  than ``min_rows`` (default 100) rows in the window the check says so instead. Turning to drift notifies
  the model's owner and ``notify`` (``ml.drift_detected``); turning back, ``ml.drift_resolved``.
* ``POST /v1/ml/models/<model>/feedback`` with ``inference_id`` and ``labels`` (one per row) records the
  truth, by the model's managers or whoever made the call.

**Payloads.** Inputs and predictions are stored only for monitored models, and only while
``log_payloads`` is on (default true): they are the data the model was given, so only the model's
managers can read them (``GET .../monitor/inferences``) and they are deleted after ``retention_days``
(default 30). Metrics and checks are visible to whoever may see the model.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
import time
import uuid
from collections import Counter
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Float, Integer, String, Table, Text, delete, insert, select, update

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

EPS = 1e-4
BINS = 10
TOP = 20
MAX_BASELINE = 100_000

ml_monitors = Table(
    "cairn_ml_monitors", metadata,
    Column("model", String(256), primary_key=True),
    Column("baseline", Text, nullable=False),                 # JSON
    Column("features", Text, nullable=True),                  # JSON list; null: every input column
    Column("drift_threshold", Float, nullable=False),
    Column("min_rows", Integer, nullable=False),
    Column("window_minutes", Integer, nullable=False),
    Column("every_minutes", Integer, nullable=False),
    Column("log_payloads", Boolean, nullable=False),
    Column("retention_days", Integer, nullable=False),
    Column("notify", Text, nullable=True),                    # JSON list of people or groups
    Column("created_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("profile", Text, nullable=True),                   # the baseline's bins, computed once
    Column("last_run_ms", BigInteger, nullable=True),
    Column("status", String(32), nullable=True),
    Column("error", String(2000), nullable=True),
)

ml_inferences = Table(
    "cairn_ml_inferences", metadata,
    Column("id", String(32), primary_key=True),
    Column("model", String(256), nullable=False, index=True),
    Column("ts_ms", BigInteger, nullable=False, index=True),
    Column("caller", String(256), nullable=False),
    Column("status", Integer, nullable=False),
    Column("latency_ms", Integer, nullable=False),
    Column("rows", Integer, nullable=True),
    Column("inputs", Text, nullable=True),                    # JSON records
    Column("predictions", Text, nullable=True),               # JSON list
    Column("labels", Text, nullable=True),                    # JSON list
    Column("labelled_ms", BigInteger, nullable=True),
)

ml_monitor_runs = Table(
    "cairn_ml_monitor_runs", metadata,
    Column("id", String(32), primary_key=True),
    Column("model", String(256), nullable=False, index=True),
    Column("ts_ms", BigInteger, nullable=False),
    Column("window_from_ms", BigInteger, nullable=False),
    Column("rows", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("metrics", Text, nullable=False),                  # JSON
)


class MonitorError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- MLflow's payloads
def records(body: Any) -> Optional[list[dict]]:
    """The rows of an MLflow scoring request (dataframe_split, dataframe_records, inputs, instances)."""
    if not isinstance(body, dict):
        return None
    if isinstance(body.get("dataframe_split"), dict):
        split = body["dataframe_split"]
        cols = [str(c) for c in split.get("columns") or []]
        return [dict(zip(cols, row)) for row in split.get("data") or [] if isinstance(row, list)]
    if isinstance(body.get("dataframe_records"), list):
        return [r for r in body["dataframe_records"] if isinstance(r, dict)]
    for k in ("inputs", "instances"):
        v = body.get(k)
        if isinstance(v, dict):                                # columns of values
            cols = list(v)
            n = max((len(x) for x in v.values() if isinstance(x, list)), default=0)
            return [{c: (v[c][i] if isinstance(v[c], list) and i < len(v[c]) else v[c]) for c in cols} for i in range(n)]
        if isinstance(v, list):
            return [r if isinstance(r, dict) else ({str(i): x for i, x in enumerate(r)} if isinstance(r, list) else {"0": r})
                    for r in v]
    return None


def predictions(body: Any) -> Optional[list]:
    if isinstance(body, dict):
        body = body.get("predictions", body.get("outputs"))
    return body if isinstance(body, list) else None


# --------------------------------------------------------------------------- drift
def _numeric(values: list) -> bool:
    vals = [v for v in values if v is not None]
    return bool(vals) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals)


def profile(values: list) -> dict:
    """A baseline column as bins: quantile edges for numbers, the most common values for anything else."""
    vals = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not vals:
        return {"kind": "empty"}
    if _numeric(vals):
        s = sorted(float(v) for v in vals)
        edges = sorted({s[min(len(s) - 1, int(len(s) * i / BINS))] for i in range(1, BINS)})
        p = _shares(_bin_numbers(s, edges), len(edges) + 1)
        return {"kind": "numeric", "edges": edges, "shares": p, "mean": statistics.fmean(s)}
    counts = Counter(str(v) for v in vals)
    top = [k for k, _ in counts.most_common(TOP)]
    shares = [counts[k] / len(vals) for k in top] + [sum(c for k, c in counts.items() if k not in top) / len(vals)]
    return {"kind": "categorical", "values": top, "shares": shares}


def _bin_numbers(values: list, edges: list) -> list[int]:
    import bisect

    out = [0] * (len(edges) + 1)
    for v in values:
        out[bisect.bisect_right(edges, float(v))] += 1
    return out


def _shares(counts: list[int], n: int) -> list[float]:
    total = sum(counts)
    return [c / total if total else 0.0 for c in counts] + [0.0] * (n - len(counts))


def psi(base: dict, values: list) -> Optional[float]:
    """Population stability index of ``values`` against a baseline profile (None: nothing to compare)."""
    vals = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if base.get("kind") == "empty" or not vals:
        return None
    if base["kind"] == "numeric":
        if not _numeric(vals):
            return None
        actual = _shares(_bin_numbers(vals, base["edges"]), len(base["edges"]) + 1)
    else:
        idx = {v: i for i, v in enumerate(base["values"])}
        counts = [0] * (len(base["values"]) + 1)
        for v in vals:
            counts[idx.get(str(v), len(base["values"]))] += 1
        actual = _shares(counts, len(counts))
    return round(sum((a - e) * math.log(max(a, EPS) / max(e, EPS)) for a, e in zip(actual, base["shares"])), 4)


def _pct(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


# --------------------------------------------------------------------------- the service
class ModelMonitoring:
    def __init__(self, engine, may_view: Callable[[Any, str], bool], may_manage: Callable[[Any, str], bool],
                 owner: Callable[[str], Optional[str]], read_table: Optional[Callable[[str, list], list[dict]]] = None,
                 notify: Optional[Callable[..., None]] = None, audit=None):
        """``read_table(namespace.table, columns)``: up to 100,000 rows of a lakehouse table, read by Cairn;
        ``notify(type, title, body, recipients, payload)``."""
        self.engine, self.may_view, self.may_manage, self.owner = engine, may_view, may_manage, owner
        self.read_table, self.notify, self.audit = read_table, notify, audit

    # ---- logging every call ------------------------------------------------------------------
    def monitor(self, model: str) -> Optional[dict]:
        with self.engine.connect() as conn:
            r = conn.execute(select(ml_monitors).where(ml_monitors.c.model == model)).first()
        if r is None:
            return None
        d = dict(r._mapping)
        d["baseline"] = json.loads(d["baseline"])
        d["features"] = json.loads(d["features"]) if d["features"] else None
        d["notify"] = json.loads(d["notify"]) if d["notify"] else []
        return d

    def record(self, model: str, caller: str, status: int, latency_ms: int, request: bytes, response: bytes) -> str:
        """Logs one call (the gateway calls this); payloads only for a monitored model that logs them."""
        iid = uuid.uuid4().hex
        rows, inputs, preds = None, None, None
        try:
            req = records(json.loads(request or b"null"))
            rows = len(req) if req is not None else None
            m = self.monitor(model)
            if m is not None and m["log_payloads"] and req is not None:
                inputs = json.dumps(req, default=str)
                if status < 300:
                    p = predictions(json.loads(response or b"null"))
                    preds = json.dumps(p, default=str) if p is not None else None
        except (ValueError, TypeError):
            pass                                              # not JSON: logged without payloads
        with self.engine.begin() as conn:
            conn.execute(insert(ml_inferences).values(id=iid, model=model, ts_ms=_now(), caller=caller, status=status,
                                                      latency_ms=latency_ms, rows=rows, inputs=inputs, predictions=preds))
        return iid

    # ---- configuration -----------------------------------------------------------------------
    def configure(self, p, model: str, body: dict) -> dict:
        if not self.may_manage(p, model):
            raise MonitorError(f"monitoring {model} needs MANAGE_ML_MODEL on it, or owning it", 403)
        base = body.get("baseline")
        if not isinstance(base, dict) or not (isinstance(base.get("table"), str) or
                                               isinstance(base.get("since_ms"), int) and isinstance(base.get("until_ms"), int)):
            raise MonitorError('baseline: {"table": "<namespace>.<table>"} or {"since_ms": ..., "until_ms": ...}')
        if "table" in base:
            if base["table"].count(".") < 1:
                raise MonitorError("baseline.table: <namespace>.<table>")
            if self.read_table is None:
                raise MonitorError("a table baseline needs Cairn's lakehouse catalog (CAIRN_PIPES_CATALOG)", 409)
        elif base["since_ms"] >= base["until_ms"]:
            raise MonitorError("baseline: since_ms before until_ms")
        features = body.get("features")
        if features is not None and (not isinstance(features, list) or not all(isinstance(f, str) and f for f in features)):
            raise MonitorError("features: column names")
        notify = body.get("notify") or []
        if not isinstance(notify, list) or not all(isinstance(n, str) and n.startswith("urn:li:") for n in notify):
            raise MonitorError("notify: people or groups (urns)")

        def num(key, default, lo, hi, kind=int):
            v = body.get(key, default)
            if isinstance(v, bool) or not isinstance(v, (int, float) if kind is float else int) or not lo <= v <= hi:
                raise MonitorError(f"{key}: {lo} to {hi}")
            return kind(v)
        values = {"baseline": json.dumps(base), "features": json.dumps(features) if features else None,
                  "drift_threshold": num("drift_threshold", 0.2, 0.01, 10, float), "min_rows": num("min_rows", 100, 1, 10**7),
                  "window_minutes": num("window_minutes", 1440, 1, 60 * 24 * 90),
                  "every_minutes": num("every_minutes", 60, 1, 60 * 24 * 7),
                  "log_payloads": bool(body.get("log_payloads", True)), "retention_days": num("retention_days", 30, 1, 3650),
                  "notify": json.dumps(notify), "profile": None, "error": None}
        with self.engine.begin() as conn:
            if conn.execute(select(ml_monitors.c.model).where(ml_monitors.c.model == model)).first():
                conn.execute(update(ml_monitors).where(ml_monitors.c.model == model).values(**values))
            else:
                conn.execute(insert(ml_monitors).values(model=model, created_by=p.urn, created_ms=_now(), **values))
        if "table" in base:
            try:
                self._profile(self.monitor(model))           # the table is read now: a wrong one fails here
            except MonitorError:
                self._forget(model)
                raise
            except Exception as e:
                self._forget(model)
                raise MonitorError(f"the baseline table could not be read: {e}")
        if self.audit is not None:
            self.audit.record("ml", "ml.monitor_configured", p.urn, model, {"baseline": base, "features": features})
        return self.get(p, model)

    def _forget(self, model: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(delete(ml_monitors).where(ml_monitors.c.model == model))

    def get(self, p, model: str) -> dict:
        if not self.may_view(p, model):
            raise MonitorError(f"no model {model}", 404)
        m = self.monitor(model)
        if m is None:
            raise MonitorError(f"{model} is not monitored", 404)
        m.pop("profile", None)
        with self.engine.connect() as conn:
            last = conn.execute(select(ml_monitor_runs).where(ml_monitor_runs.c.model == model)
                                .order_by(ml_monitor_runs.c.ts_ms.desc()).limit(1)).first()
        m["last_check"] = self._run(last) if last else None
        return m

    def list(self, p) -> list[dict]:
        """The monitors of the models the caller may see (``GET /v1/ml/monitors``: what ``cairn export`` reads)."""
        with self.engine.connect() as conn:
            names = [r[0] for r in conn.execute(select(ml_monitors.c.model).order_by(ml_monitors.c.model))]
        out = []
        for name in names:
            if self.may_view(p, name):
                m = self.monitor(name)
                if m is not None:
                    m.pop("profile", None)
                    out.append(m)
        return out

    def remove(self, p, model: str) -> None:
        if not self.may_manage(p, model):
            raise MonitorError(f"monitoring {model} needs MANAGE_ML_MODEL on it, or owning it", 403)
        if self.monitor(model) is None:
            raise MonitorError(f"{model} is not monitored", 404)
        self._forget(model)
        with self.engine.begin() as conn:            # the payloads go with it; the call log and checks stay
            conn.execute(update(ml_inferences).where(ml_inferences.c.model == model).values(inputs=None, predictions=None))
        if self.audit is not None:
            self.audit.record("ml", "ml.monitor_removed", p.urn, model, {})

    # ---- truth -------------------------------------------------------------------------------
    def feedback(self, p, model: str, body: dict) -> dict:
        iid, labels = body.get("inference_id"), body.get("labels")
        if not isinstance(iid, str) or not isinstance(labels, list):
            raise MonitorError("inference_id and labels (one per row)")
        with self.engine.connect() as conn:
            r = conn.execute(select(ml_inferences).where(ml_inferences.c.id == iid, ml_inferences.c.model == model)).first()
        if r is None or not (r.caller == p.urn or self.may_manage(p, model)):
            raise MonitorError(f"no call {iid} to {model} of yours", 404)
        if r.rows is not None and len(labels) != r.rows:
            raise MonitorError(f"that call had {r.rows} rows: give {r.rows} labels")
        with self.engine.begin() as conn:
            conn.execute(update(ml_inferences).where(ml_inferences.c.id == iid).values(
                labels=json.dumps(labels, default=str), labelled_ms=_now()))
        return {"inference_id": iid, "labels": len(labels)}

    # ---- checks ------------------------------------------------------------------------------
    def _profile(self, m: dict) -> dict:
        if m["profile"]:
            return json.loads(m["profile"])
        base = m["baseline"]
        if "table" in base:
            rows = self.read_table(base["table"], m["features"])
            preds: list = []
        else:
            if _now() < base["until_ms"]:
                raise MonitorError("the baseline's reference period is not over yet", 409)
            rows, preds = self._window(m["model"], base["since_ms"], base["until_ms"])
        if not rows:
            raise MonitorError("the baseline has no rows (the table is empty, or no calls were logged then)")
        cols = m["features"] or sorted({k for r in rows for k in r})
        missing = [c for c in cols if all(c not in r for r in rows)]
        if missing:
            raise MonitorError(f"the baseline has no column {missing[0]}")
        prof = {"features": {c: profile([r.get(c) for r in rows]) for c in cols}, "rows": len(rows),
                "predictions": profile(_flat(preds)) if preds else None}
        with self.engine.begin() as conn:
            conn.execute(update(ml_monitors).where(ml_monitors.c.model == m["model"]).values(profile=json.dumps(prof)))
        return prof

    def _window(self, model: str, since: int, until: int) -> tuple[list[dict], list]:
        rows, preds = [], []
        with self.engine.connect() as conn:
            for r in conn.execute(select(ml_inferences.c.inputs, ml_inferences.c.predictions).where(
                    ml_inferences.c.model == model, ml_inferences.c.ts_ms >= since, ml_inferences.c.ts_ms < until,
                    ml_inferences.c.status < 300, ml_inferences.c.inputs.is_not(None))):
                rows += json.loads(r.inputs)
                preds += json.loads(r.predictions) if r.predictions else []
        return rows, preds

    def check(self, model: str, now: Optional[int] = None) -> dict:
        m = self.monitor(model)
        if m is None:
            raise MonitorError(f"{model} is not monitored", 404)
        now = now or _now()
        since = now - m["window_minutes"] * 60_000
        try:
            prof = self._profile(m)
        except MonitorError as e:
            with self.engine.begin() as conn:
                conn.execute(update(ml_monitors).where(ml_monitors.c.model == model).values(
                    error=None if e.status == 409 else str(e)[:2000], last_run_ms=now,
                    status="waiting" if e.status == 409 else "error"))
            raise
        with self.engine.connect() as conn:
            calls = conn.execute(select(ml_inferences).where(ml_inferences.c.model == model,
                                                             ml_inferences.c.ts_ms >= since,
                                                             ml_inferences.c.ts_ms <= now)).all()
        ok = [c for c in calls if c.status < 300]
        rows, preds, labelled = [], [], []
        for c in ok:
            if c.inputs:
                rows += json.loads(c.inputs)
            if c.predictions:
                p = json.loads(c.predictions)
                preds += p
                if c.labels:
                    labelled += list(zip(p, json.loads(c.labels)))
        lat = [float(c.latency_ms) for c in calls]
        metrics: dict[str, Any] = {
            "calls": len(calls), "errors": len(calls) - len(ok),
            "error_rate": round((len(calls) - len(ok)) / len(calls), 4) if calls else None,
            "latency_p50_ms": _pct(lat, 0.5), "latency_p95_ms": _pct(lat, 0.95), "rows": len(rows),
            "features": {}, "drifted": []}
        enough = len(rows) >= m["min_rows"]
        for col, base in prof["features"].items():
            vals = [r.get(col) for r in rows]
            score = psi(base, vals)
            entry: dict[str, Any] = {"psi": score}
            if base.get("kind") == "numeric" and _numeric(vals) and any(v is not None for v in vals):
                entry.update(baseline_mean=round(base["mean"], 6),
                             mean=round(statistics.fmean(float(v) for v in vals if v is not None), 6))
            if sum(v is None for v in vals) and vals:
                entry["null_share"] = round(sum(v is None for v in vals) / len(vals), 4)
            metrics["features"][col] = entry
            if enough and score is not None and score > m["drift_threshold"]:
                metrics["drifted"].append(col)
        if prof.get("predictions"):
            score = psi(prof["predictions"], _flat(preds))
            metrics["prediction_psi"] = score
            if enough and score is not None and score > m["drift_threshold"]:
                metrics["drifted"].append("(predictions)")
        if labelled:
            pairs = [(_scalar(a), _scalar(b)) for a, b in labelled]
            numbers = all(isinstance(x, (int, float)) and not isinstance(x, bool) for pair in pairs for x in pair)
            if numbers and any(isinstance(x, float) and not float(x).is_integer() for pair in pairs for x in pair):
                metrics["mae"] = round(statistics.fmean(abs(float(a) - float(b)) for a, b in pairs), 6)
            else:
                metrics["accuracy"] = round(sum(str(a) == str(b) for a, b in pairs) / len(pairs), 4)
            metrics["labelled_rows"] = len(pairs)
        status = "insufficient" if not enough else ("drift" if metrics["drifted"] else "ok")
        run = {"id": uuid.uuid4().hex, "model": model, "ts_ms": now, "window_from_ms": since, "rows": len(rows),
               "status": status, "metrics": json.dumps(metrics, default=str)}
        with self.engine.begin() as conn:
            conn.execute(insert(ml_monitor_runs).values(**run))
            conn.execute(update(ml_monitors).where(ml_monitors.c.model == model).values(
                last_run_ms=now, status=status, error=None))
        before = m["status"]
        if self.notify is not None and status != before and (status == "drift" or before == "drift" and status == "ok"):
            recipients = [u for u in [self.owner(model), *m["notify"]] if u]
            if status == "drift":
                self.notify("ml.drift_detected", f"Model {model} is drifting",
                            f"{', '.join(metrics['drifted'])} drifted from the baseline (PSI above {m['drift_threshold']}) "
                            f"over the last {m['window_minutes']} minutes.", recipients,
                            {"model": model, "drifted": metrics["drifted"]})
            else:
                self.notify("ml.drift_resolved", f"Model {model} is back within its baseline",
                            "No feature drifts any more.", recipients, {"model": model})
        return self._run_dict(run)

    @staticmethod
    def _run_dict(run: dict) -> dict:
        return {**{k: v for k, v in run.items() if k != "metrics"}, "metrics": json.loads(run["metrics"])}

    def _run(self, r) -> dict:
        return self._run_dict(dict(r._mapping))

    def checks(self, p, model: str, limit: int = 50) -> list[dict]:
        if not self.may_view(p, model):
            raise MonitorError(f"no model {model}", 404)
        with self.engine.connect() as conn:
            return [self._run(r) for r in conn.execute(select(ml_monitor_runs).where(ml_monitor_runs.c.model == model)
                                                        .order_by(ml_monitor_runs.c.ts_ms.desc()).limit(max(1, min(limit, 500))))]

    def run_now(self, p, model: str) -> dict:
        if not self.may_manage(p, model):
            raise MonitorError(f"checking {model} needs MANAGE_ML_MODEL on it, or owning it", 403)
        return self.check(model)

    def inferences(self, p, model: str, limit: int = 100) -> list[dict]:
        if not self.may_manage(p, model):
            raise MonitorError(f"the calls to {model} (with what they were given) are for its managers", 403)
        with self.engine.connect() as conn:
            rows = conn.execute(select(ml_inferences).where(ml_inferences.c.model == model)
                                .order_by(ml_inferences.c.ts_ms.desc()).limit(max(1, min(limit, 1000)))).all()
        return [{"id": r.id, "ts_ms": r.ts_ms, "caller": r.caller, "status": r.status, "latency_ms": r.latency_ms,
                 "rows": r.rows, "inputs": json.loads(r.inputs) if r.inputs else None,
                 "predictions": json.loads(r.predictions) if r.predictions else None,
                 "labels": json.loads(r.labels) if r.labels else None} for r in rows]

    def run_due(self, now: Optional[int] = None) -> int:
        """The scheduler's tick: checks every monitor that is due, and deletes payloads past retention."""
        now = now or _now()
        n = 0
        with self.engine.connect() as conn:
            due = conn.execute(select(ml_monitors.c.model, ml_monitors.c.last_run_ms, ml_monitors.c.every_minutes,
                                      ml_monitors.c.retention_days)).all()
        for r in due:
            with self.engine.begin() as conn:
                conn.execute(delete(ml_inferences).where(ml_inferences.c.model == r.model,
                                                         ml_inferences.c.ts_ms < now - r.retention_days * 86_400_000))
            if r.last_run_ms and now - r.last_run_ms < r.every_minutes * 60_000:
                continue
            try:
                self.check(r.model, now)
                n += 1
            except Exception as e:  # noqa: BLE001 - recorded; the others go on
                logger.warning("model monitor %s: %s", r.model, e)
        return n


def _scalar(v: Any) -> Any:
    if isinstance(v, dict) and len(v) == 1:
        return next(iter(v.values()))
    if isinstance(v, list) and len(v) == 1:
        return v[0]
    return v


def _flat(preds: list) -> list:
    return [_scalar(p) for p in preds]


def build_monitoring_router(mon: ModelMonitoring, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/ml", tags=["ml"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except MonitorError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/monitors")
    async def list_(p=Depends(current_principal)):
        return {"monitors": await call(lambda: mon.list(p))}

    @router.put("/models/{model}/monitor")
    async def configure(model: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: mon.configure(p, model, body))

    @router.get("/models/{model}/monitor")
    async def get(model: str, p=Depends(current_principal)):
        return await call(lambda: mon.get(p, model))

    @router.delete("/models/{model}/monitor", status_code=204)
    async def remove(model: str, p=Depends(current_principal)):
        await call(lambda: mon.remove(p, model))

    @router.post("/models/{model}/monitor/run")
    async def run(model: str, p=Depends(current_principal)):
        return await call(lambda: mon.run_now(p, model))

    @router.get("/models/{model}/monitor/checks")
    async def checks(model: str, limit: int = 50, p=Depends(current_principal)):
        return await call(lambda: mon.checks(p, model, limit))

    @router.get("/models/{model}/monitor/inferences")
    async def inferences(model: str, limit: int = 100, p=Depends(current_principal)):
        return await call(lambda: mon.inferences(p, model, limit))

    @router.post("/models/{model}/feedback")
    async def feedback(model: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: mon.feedback(p, model, body))

    return router
