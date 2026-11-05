"""Metric alerts: a metric watched over time, its owner told once when it goes wrong and once when it recovers.

``POST /sql/metric-alerts`` with ``{"name", "metric", "grain": "day"|"week"|"month"|"quarter", "rule": {...},
"filters": [...], "recipients": [people], "every_minutes": 60}``. Rules:

* ``{"kind": "change", "drop_pct": 20, "rise_pct": null}``: the last complete period against the one before it
  (revenue down 20% week over week).
* ``{"kind": "range", "periods": 8, "band": 3}``: the last complete period against the usual range of the
  ``periods`` before it (median ± ``band`` robust standard deviations); quiet until 4 of them exist.
* ``{"kind": "threshold", "op": "<", "value": 1000}``: the last complete period against a fixed value.

The period still filling is never judged. Each check runs the metric as the alert's **owner** (their policies,
masks and row filters), so an alert says nothing about rows its owner may not see. Only a change of state is
told (``metric.alert_triggered``, ``metric.alert_resolved``), to the owner and the recipients, so a metric that
stays out of range is reported once. Only the owner changes or deletes an alert; each check is kept on the alert.
"""

from __future__ import annotations

import time
import uuid
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Float, Integer, String, Table, Text, and_, delete, insert, select, update

from cairn.common.db import JSONText, metadata

GRAINS = ("day", "week", "month", "quarter")
KINDS = ("change", "range", "threshold")
OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}
MIN_HISTORY = 4

metric_alerts = Table(
    "cairn_metric_alerts", metadata,
    Column("id", String(32), primary_key=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("name", String(200), nullable=False),
    Column("metric", String(64), nullable=False, index=True),
    Column("grain", String(10), nullable=False),
    Column("rule", JSONText, nullable=False),
    Column("filters", JSONText, nullable=False),
    Column("recipients", JSONText, nullable=False),
    Column("every_minutes", Integer, nullable=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("state", String(10), nullable=False),               # unknown | ok | triggered | learning | error
    Column("last_value", Float, nullable=True),
    Column("last_period", String(32), nullable=True),
    Column("last_message", Text, nullable=True),
    Column("last_checked_ms", BigInteger, nullable=True),
    Column("next_check_ms", BigInteger, nullable=False, index=True),
    Column("created_ms", BigInteger, nullable=False),
)


class MetricAlertError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def period_start(grain: str, now_ms: int) -> date:
    """The first day of the period ``now`` falls in (UTC; weeks start on Monday, as Trino's date_trunc)."""
    d = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date()
    if grain == "day":
        return d
    if grain == "week":
        return d - timedelta(days=d.weekday())
    if grain == "month":
        return d.replace(day=1)
    return d.replace(month=(d.month - 1) // 3 * 3 + 1, day=1)


def _as_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")[:19]).date()
    except ValueError:
        return None


def judge(rule: dict, series: list[tuple[date, float]], grain: str = "day") -> tuple[str, Optional[float], str]:
    """(state, value, message) for complete periods ``series`` (oldest first)."""
    if not series:
        return "learning", None, "no complete period yet"
    last_day, value = series[-1]
    label = last_day.isoformat()
    kind = rule["kind"]
    if kind == "threshold":
        bad = OPS[rule["op"]](value, float(rule["value"]))
        cond = f"{'' if bad else 'not '}{rule['op']} {float(rule['value']):,.2f}"
        return ("triggered" if bad else "ok"), value, f"{label}: {value:,.2f} ({cond})"
    if kind == "change":
        if len(series) < 2:
            return "learning", value, "needs a period before the last one"
        prev = series[-2][1]
        if prev == 0:
            return "ok", value, f"{label}: {value:,.2f}; the period before was 0, so no change in percent"
        pct = (value - prev) / abs(prev) * 100
        drop, rise = rule.get("drop_pct"), rule.get("rise_pct")
        bad = (drop is not None and pct <= -float(drop)) or (rise is not None and pct >= float(rise))
        return ("triggered" if bad else "ok"), value, f"{label}: {value:,.2f}, {pct:+.1f}% on the {grain} before ({prev:,.2f})"
    history = [v for _, v in series[-1 - int(rule.get("periods", 8)):-1]]
    if len(history) < MIN_HISTORY:
        return "learning", value, f"learning the usual range ({len(history)} of {MIN_HISTORY} periods)"
    mid = median(history)
    spread = median([abs(v - mid) for v in history]) * 1.4826 or max(abs(mid) * 0.01, 1e-9)
    band = float(rule.get("band", 3))
    lo, hi = mid - band * spread, mid + band * spread
    bad = not lo <= value <= hi
    return ("triggered" if bad else "ok"), value, f"{label}: {value:,.2f}, usual range {lo:,.2f} to {hi:,.2f}"


class MetricAlerts:
    def __init__(self, engine, metrics, notify: Optional[Callable[[str, str, str, list[str], dict], Any]] = None, audit=None):
        """``metrics``: cairn.sqlworkspace.metrics.Metrics (compiles and runs a metric as a person)."""
        self.engine, self.metrics, self.notify, self.audit = engine, metrics, notify, audit

    # ---- definitions ------------------------------------------------------------------------------
    def _check_body(self, actor: str, body: dict) -> dict:
        if not isinstance(body, dict):
            raise MetricAlertError("an alert is an object")
        name = str(body.get("name") or "").strip()
        if not 1 <= len(name) <= 200:
            raise MetricAlertError("name: 1 to 200 characters")
        grain = body.get("grain", "day")
        if grain not in GRAINS:
            raise MetricAlertError(f"grain: one of {', '.join(GRAINS)}")
        rule = body.get("rule")
        if not isinstance(rule, dict) or rule.get("kind") not in KINDS:
            raise MetricAlertError(f"rule.kind: one of {', '.join(KINDS)}")
        rule = dict(rule)
        try:
            if rule["kind"] == "change":
                for k in ("drop_pct", "rise_pct"):
                    if rule.get(k) is not None and not 0 < float(rule[k]) <= 1000:
                        raise MetricAlertError(f"rule.{k}: above 0 and at most 1000 (percent)")
                if rule.get("drop_pct") is None and rule.get("rise_pct") is None:
                    raise MetricAlertError("a change rule needs drop_pct, rise_pct or both")
            elif rule["kind"] == "range":
                rule["periods"] = int(rule.get("periods", 8))
                rule["band"] = float(rule.get("band", 3))
                if not MIN_HISTORY <= rule["periods"] <= 104 or not 1 <= rule["band"] <= 10:
                    raise MetricAlertError(f"rule.periods: {MIN_HISTORY} to 104; rule.band: 1 to 10")
            else:
                if rule.get("op") not in OPS:
                    raise MetricAlertError(f"rule.op: one of {', '.join(OPS)}")
                rule["value"] = float(rule["value"])
        except (TypeError, ValueError, KeyError):
            raise MetricAlertError("rule: numbers where numbers are asked for")
        recipients = body.get("recipients") or []
        if not isinstance(recipients, list) or len(recipients) > 50 or not all(
                isinstance(r, str) and r.startswith("urn:li:corpuser:") for r in recipients):
            raise MetricAlertError("recipients: up to 50 urn:li:corpuser:… people")
        every = body.get("every_minutes", 60)
        if isinstance(every, bool) or not isinstance(every, int) or not 5 <= every <= 43_200:
            raise MetricAlertError("every_minutes: 5 to 43,200")
        filters = body.get("filters") or []
        metric = str(body.get("metric") or "")
        # The metric must exist, have a time column for the grain, and compile for the owner with these filters.
        from cairn.sqlworkspace.metrics import MetricError

        try:
            self.metrics.compile(actor, {"metrics": [metric], "grain": grain, "filters": filters, "limit": 10})
        except MetricError as e:
            raise MetricAlertError(str(e), e.status)
        return {"name": name, "metric": metric, "grain": grain, "rule": rule, "filters": filters,
                "recipients": sorted(set(recipients)), "every_minutes": every, "enabled": bool(body.get("enabled", True))}

    def save(self, actor: str, body: dict, aid: Optional[str] = None) -> dict:
        values = self._check_body(actor, body)
        with self.engine.begin() as conn:
            if aid is None:
                aid = uuid.uuid4().hex
                conn.execute(insert(metric_alerts).values(id=aid, owner=actor, state="unknown", next_check_ms=_now(),
                                                          created_ms=_now(), **values))
            elif not conn.execute(update(metric_alerts).where(and_(metric_alerts.c.id == aid, metric_alerts.c.owner == actor))
                                  .values(state="unknown", next_check_ms=_now(), **values)).rowcount:
                raise MetricAlertError("no such alert of yours", 404)
        self._audit(actor, "metric_alert.saved", aid, {"metric": values["metric"], "rule": values["rule"]["kind"]})
        return self.get(actor, aid)

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(select(metric_alerts).where(metric_alerts.c.owner == actor)
                                                           .order_by(metric_alerts.c.name))]

    def get(self, actor: str, aid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(metric_alerts).where(and_(metric_alerts.c.id == aid, metric_alerts.c.owner == actor))).first()
        if r is None:
            raise MetricAlertError("no such alert of yours", 404)
        return dict(r._mapping)

    def delete(self, actor: str, aid: str) -> None:
        with self.engine.begin() as conn:
            if not conn.execute(delete(metric_alerts).where(and_(metric_alerts.c.id == aid, metric_alerts.c.owner == actor))).rowcount:
                raise MetricAlertError("no such alert of yours", 404)
        self._audit(actor, "metric_alert.deleted", aid, {})

    # ---- checking ---------------------------------------------------------------------------------
    def series(self, a: dict, now_ms: int) -> list[tuple[date, float]]:
        """The metric per complete period, as the alert's owner, oldest first."""
        from cairn.sqlworkspace.metrics import PERIOD

        out = self.metrics.query(a["owner"], {"metrics": [a["metric"]], "grain": a["grain"], "filters": a["filters"],
                                              "limit": 1000})
        cols = out["columns"]
        if PERIOD not in cols or a["metric"] not in cols:
            raise MetricAlertError("the metric's query did not return its period and value")
        pi, vi = cols.index(PERIOD), cols.index(a["metric"])
        current = period_start(a["grain"], now_ms)
        rows = []
        for r in out["rows"]:
            d = _as_date(r[pi])
            if d is not None and d < current and r[vi] is not None:   # the period still filling is never judged
                rows.append((d, float(r[vi])))
        return sorted(rows)

    def check(self, aid: str, now: Optional[int] = None) -> dict:
        now = now or _now()
        with self.engine.connect() as conn:
            r = conn.execute(select(metric_alerts).where(metric_alerts.c.id == aid)).first()
        if r is None:
            raise MetricAlertError("no such alert", 404)
        a = dict(r._mapping)
        period = None
        try:
            series = self.series(a, now)
            state, value, message = judge(a["rule"], series, a["grain"])
            period = series[-1][0].isoformat() if series else None
        except Exception as e:  # noqa: BLE001 - the metric could not be read (its owner's access, the warehouse)
            state, value, message = "error", None, f"the metric could not be read: {str(e)[:500]}"
        with self.engine.begin() as conn:
            conn.execute(update(metric_alerts).where(metric_alerts.c.id == aid).values(
                state=state, last_value=value, last_period=period, last_message=message, last_checked_ms=now,
                next_check_ms=now + a["every_minutes"] * 60_000))
        # Told once per change of state: into trouble, out of it, or failing to read.
        was = a["state"]
        if state != was and state != "learning" and not (was in ("unknown", "learning") and state == "ok"):
            who = [a["owner"], *[p for p in a["recipients"] if p != a["owner"]]]
            payload = {"alert": aid, "metric": a["metric"], "value": value, "period": period}
            if state == "triggered":
                self._tell("metric.alert_triggered", f"{a['name']}: {a['metric']} needs a look", message, who, payload)
            elif state == "ok" and was == "triggered":
                self._tell("metric.alert_resolved", f"{a['name']}: {a['metric']} is back to normal", message, who, payload)
            elif state == "error":
                self._tell("metric.alert_triggered", f"{a['name']}: the check failed", message, [a["owner"]], payload)
            self._audit("urn:li:corpuser:cairn-system", f"metric_alert.{state}", aid, {"metric": a["metric"], "value": value})
        return {**a, "state": state, "last_value": value, "last_period": period, "last_message": message,
                "last_checked_ms": now}

    def run_due(self, now: Optional[int] = None) -> int:
        """Checks every enabled alert that is due (the scheduler's leader calls this each tick)."""
        now = now or _now()
        with self.engine.connect() as conn:
            due = conn.execute(select(metric_alerts.c.id).where(and_(metric_alerts.c.enabled,
                                                                     metric_alerts.c.next_check_ms <= now))).scalars().all()
        for aid in due:
            # Claim it first, so two schedulers do not both check (and tell) it.
            with self.engine.begin() as conn:
                n = conn.execute(update(metric_alerts).where(and_(metric_alerts.c.id == aid,
                                                                  metric_alerts.c.next_check_ms <= now))
                                 .values(next_check_ms=now + 60_000)).rowcount
            if n:
                self.check(aid, now)
        return len(due)

    def _tell(self, kind, title, body, who, payload) -> None:
        if self.notify is not None:
            try:
                self.notify(kind, title, body, who, payload)
            except Exception:  # noqa: BLE001 - a notification never fails the check
                pass

    def _audit(self, actor: str, action: str, aid: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("sql", action, actor, f"metric_alert:{aid}", details)


def build_metric_alerts_router(ma: MetricAlerts, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/sql/metric-alerts", tags=["sql"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except MetricAlertError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        return await call(lambda: ma.list(p.urn))

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: ma.save(p.urn, body))

    @router.get("/{aid}")
    async def get(aid: str, p=Depends(current_principal)):
        return await call(lambda: ma.get(p.urn, aid))

    @router.put("/{aid}")
    async def change(aid: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: ma.save(p.urn, body, aid))

    @router.delete("/{aid}", status_code=204)
    async def drop(aid: str, p=Depends(current_principal)):
        await call(lambda: ma.delete(p.urn, aid))

    @router.post("/{aid}/check")
    async def check(aid: str, p=Depends(current_principal)):
        await call(lambda: ma.get(p.urn, aid))                 # only its owner checks it now
        return await call(lambda: ma.check(aid))

    return router
