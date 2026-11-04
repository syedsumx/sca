"""Cost budgets: what a part of the platform may spend each period, and an alert before and when it does.

``POST /v1/budgets`` (``MANAGE_SYSTEM_OPERATIONS``) with ``name``, ``amount_usd``, ``period`` (``month``,
the default; ``week``, from Monday; or ``day``; calendar periods in UTC) and one ``scope``:

* ``{"all": true}``: everything Cairn prices;
* ``{"domain": "urn:li:domain:..."}`` or ``{"owner": "urn:li:corpGroup:..."}`` (a person or a group):
  the tables in that domain, or owned by them, in the catalog;
* ``{"platform": "snowflake"}`` or ``{"tables": ["urn:li:dataset:...", ...]}``;
* ``{"warehouse": "<name>"}``: a Cairn warehouse's node-hours;
* ``{"ai": true}`` or ``{"ai": "urn:li:corpuser:..."}``: AI spend (everyone's, or one person's).

**Spend** is what Cairn's cost report already holds, with nothing counted twice: tables' daily storage
and compute (``cairn_asset_costs``: warehouse collectors, ``POST /reports/costs``, Spark runs split across
what they wrote), runs charged to nothing else (warehouse node-hours; Spark runs that wrote nothing), and
AI calls (``cairn_ai_usage``).

**Alerts.** Each check (hourly on the scheduler, or ``POST /v1/budgets/<id>/check``) compares the period's
spend with ``thresholds`` (percent of the amount; default 80 and 100) and, with ``forecast`` (default on),
the period's projected spend (the spend so far, extended at its daily rate over the whole period, once a
fifth of it has gone by). Each threshold and the forecast alert at most once a period, to the budget's
``notify`` (people or groups) and its creator: ``budget.threshold_crossed`` and ``budget.forecast_exceeded``.
``GET /v1/budgets/<id>`` shows the period's spend by day, the largest contributors, the forecast and the
alerts sent; budgets are listed for operators, their creators and those they notify.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Float, String, Table, Text, and_, delete, insert, select, update

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

PERIODS = ("month", "week", "day")
SCOPES = ("all", "domain", "owner", "platform", "tables", "warehouse", "ai")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,127}$")
FORECAST_AFTER = 0.2

budgets = Table(
    "cairn_budgets", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(128), nullable=False, unique=True),
    Column("amount_usd", Float, nullable=False),
    Column("period", String(8), nullable=False),
    Column("scope", Text, nullable=False),                    # JSON, one key of SCOPES
    Column("thresholds", Text, nullable=False),               # JSON list of percents
    Column("forecast", Boolean, nullable=False),
    Column("notify", Text, nullable=False),                   # JSON list of urns
    Column("enabled", Boolean, nullable=False),
    Column("created_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("checked_ms", BigInteger, nullable=True),
    Column("spent_usd", Float, nullable=True),                 # as of the last check
)

budget_alerts = Table(
    "cairn_budget_alerts", metadata,
    Column("budget_id", String(32), primary_key=True),
    Column("period", String(10), primary_key=True),           # the period's first day
    Column("kind", String(16), primary_key=True),             # "80", "100", "forecast"
    Column("sent_ms", BigInteger, nullable=False),
    Column("spent_usd", Float, nullable=False),
)


class BudgetError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def period_bounds(period: str, now_ms: int) -> tuple[datetime, datetime]:
    """The calendar period (UTC) holding ``now_ms``: [start, end)."""
    now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return day, day + timedelta(days=1)
    if period == "week":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=7)
    start = day.replace(day=1)
    return start, start + timedelta(days=calendar.monthrange(start.year, start.month)[1])


def _days(start: datetime, end: datetime) -> list[str]:
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((end - start).days)]


class Budgets:
    def __init__(self, engine, graph=None, notify: Optional[Callable[..., None]] = None,
                 may_operate: Optional[Callable[[Any], bool]] = None, warehouses: Optional[Callable[[], list[str]]] = None,
                 audit=None):
        """``notify(type, title, body, recipients, payload)``; ``may_operate(principal)``:
        MANAGE_SYSTEM_OPERATIONS; ``warehouses()``: the warehouse names."""
        self.engine, self.graph, self.notify, self.audit = engine, graph, notify, audit
        self.may_operate = may_operate or (lambda p: False)
        self.warehouses = warehouses

    # ---- definitions ---------------------------------------------------------------------------
    def _check(self, body: dict, partial: bool = False) -> dict:
        out: dict = {}
        if not partial or "name" in body:
            name = body.get("name")
            if not isinstance(name, str) or not NAME.match(name):
                raise BudgetError("name: letters, digits, spaces, _ . - (up to 128)")
            out["name"] = name
        if not partial or "amount_usd" in body:
            a = body.get("amount_usd")
            if isinstance(a, bool) or not isinstance(a, (int, float)) or not 0 < a < 1e12:
                raise BudgetError("amount_usd: more than 0")
            out["amount_usd"] = float(a)
        if not partial or "period" in body:
            per = body.get("period", "month")
            if per not in PERIODS:
                raise BudgetError(f"period: {', '.join(PERIODS)}")
            out["period"] = per
        if not partial or "scope" in body:
            out["scope"] = json.dumps(self._scope(body.get("scope")))
        if not partial or "thresholds" in body:
            th = body.get("thresholds", [80, 100])
            if not isinstance(th, list) or not th or len(th) > 10 or \
                    not all(isinstance(t, int) and not isinstance(t, bool) and 1 <= t <= 1000 for t in th):
                raise BudgetError("thresholds: percents of the amount, 1 to 1000")
            out["thresholds"] = json.dumps(sorted(set(th)))
        if not partial or "forecast" in body:
            out["forecast"] = bool(body.get("forecast", True))
        if not partial or "notify" in body:
            n = body.get("notify") or []
            if not isinstance(n, list) or not all(isinstance(u, str) and re.match(r"^urn:li:corp(user|Group):.+", u) for u in n):
                raise BudgetError("notify: people or groups (urn:li:corpuser:..., urn:li:corpGroup:...)")
            out["notify"] = json.dumps(n)
        if "enabled" in body:
            out["enabled"] = bool(body["enabled"])
        return out

    def _scope(self, scope: Any) -> dict:
        if not isinstance(scope, dict) or len(scope) != 1 or next(iter(scope)) not in SCOPES:
            raise BudgetError(f"scope: one of {', '.join(SCOPES)}")
        k, v = next(iter(scope.items()))
        if k == "all" and v is not True:
            raise BudgetError('scope: {"all": true}')
        if k == "domain" and not (isinstance(v, str) and v.startswith("urn:li:domain:")):
            raise BudgetError("scope.domain: a domain urn")
        if k == "owner" and not (isinstance(v, str) and re.match(r"^urn:li:corp(user|Group):.+", v)):
            raise BudgetError("scope.owner: a person or a group")
        if k == "platform" and not (isinstance(v, str) and re.match(r"^[a-z0-9_-]{1,64}$", v)):
            raise BudgetError("scope.platform: a platform name, such as snowflake")
        if k == "tables" and not (isinstance(v, list) and v and all(isinstance(u, str) and u.startswith("urn:li:dataset:(")
                                                                   for u in v)):
            raise BudgetError("scope.tables: dataset urns")
        if k == "warehouse":
            if not isinstance(v, str) or (self.warehouses is not None and v not in self.warehouses()):
                raise BudgetError("scope.warehouse: one of Cairn's warehouses")
        if k == "ai" and not (v is True or isinstance(v, str) and v.startswith("urn:li:corpuser:")):
            raise BudgetError('scope.ai: true, or a person')
        return {k: v}

    def _row(self, r) -> dict:
        d = dict(r._mapping)
        return {**d, "scope": json.loads(d["scope"]), "thresholds": json.loads(d["thresholds"]),
                "notify": json.loads(d["notify"])}

    def _get(self, bid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(budgets).where(budgets.c.id == bid)).first()
        if r is None:
            raise BudgetError("no such budget", 404)
        return self._row(r)

    def _sees(self, p, b: dict) -> bool:
        return self.may_operate(p) or b["created_by"] == p.urn or p.urn in b["notify"] or \
            any(g in b["notify"] for g in getattr(p, "groups", None) or [])

    def create(self, p, body: dict) -> dict:
        if not self.may_operate(p):
            raise BudgetError("budgets are set by operators (MANAGE_SYSTEM_OPERATIONS)", 403)
        values = self._check(body)
        bid = uuid.uuid4().hex
        with self.engine.begin() as conn:
            if conn.execute(select(budgets.c.id).where(budgets.c.name == values["name"])).first():
                raise BudgetError(f"a budget named {values['name']} exists", 409)
            conn.execute(insert(budgets).values(id=bid, enabled=values.pop("enabled", True), created_by=p.urn,
                                                created_ms=_now(), **values))
        if self.audit is not None:
            self.audit.record("costs", "budget.created", p.urn, None, {"id": bid, **body})
        return self.status(p, bid)

    def update(self, p, bid: str, body: dict) -> dict:
        if not self.may_operate(p):
            raise BudgetError("budgets are set by operators (MANAGE_SYSTEM_OPERATIONS)", 403)
        self._get(bid)
        values = self._check(body, partial=True)
        with self.engine.begin() as conn:
            if "name" in values and conn.execute(select(budgets.c.id).where(
                    budgets.c.name == values["name"], budgets.c.id != bid)).first():
                raise BudgetError(f"a budget named {values['name']} exists", 409)
            conn.execute(update(budgets).where(budgets.c.id == bid).values(**values))
        if self.audit is not None:
            self.audit.record("costs", "budget.changed", p.urn, None, {"id": bid, **body})
        return self.status(p, bid)

    def delete(self, p, bid: str) -> None:
        if not self.may_operate(p):
            raise BudgetError("budgets are set by operators (MANAGE_SYSTEM_OPERATIONS)", 403)
        self._get(bid)
        with self.engine.begin() as conn:
            conn.execute(delete(budget_alerts).where(budget_alerts.c.budget_id == bid))
            conn.execute(delete(budgets).where(budgets.c.id == bid))
        if self.audit is not None:
            self.audit.record("costs", "budget.deleted", p.urn, None, {"id": bid})

    def list(self, p) -> list[dict]:
        with self.engine.connect() as conn:
            rows = [self._row(r) for r in conn.execute(select(budgets).order_by(budgets.c.name))]
        return [b for b in rows if self._sees(p, b)]

    # ---- spend ---------------------------------------------------------------------------------
    def _in_scope(self, scope: dict) -> Callable[[str, str], bool]:
        """(urn, platform) -> does a table's cost count for this scope."""
        k, v = next(iter(scope.items()))
        cache: dict[str, bool] = {}

        def aspect_has(urn: str, aspect: str, pick: Callable[[dict], list]) -> bool:
            if urn not in cache:
                try:
                    cache[urn] = v in pick(self.graph.get_aspect(urn, aspect) or {}) if self.graph is not None else False
                except Exception:  # noqa: BLE001 - the catalog is unreachable: not counted, and logged
                    logger.warning("budget scope: %s of %s unavailable", aspect, urn)
                    cache[urn] = False
            return cache[urn]
        if k == "all":
            return lambda urn, platform: True
        if k == "platform":
            return lambda urn, platform: platform == v
        if k == "tables":
            wanted = set(v)
            return lambda urn, platform: urn in wanted
        if k == "domain":
            return lambda urn, platform: aspect_has(urn, "domains", lambda a: a.get("domains") or [])
        if k == "owner":
            return lambda urn, platform: aspect_has(urn, "ownership",
                                                    lambda a: [o.get("owner") for o in a.get("owners") or []])
        return lambda urn, platform: False                   # warehouse, ai: no tables

    def spend(self, b: dict, now_ms: Optional[int] = None) -> dict:
        from cairn.ai.usage import usage_table
        from cairn.reports.costs import asset_costs, run_costs

        now_ms = now_ms or _now()
        start, end = period_bounds(b["period"], now_ms)
        days = _days(start, end)
        first, last = days[0], days[-1]
        by_day: dict[str, float] = defaultdict(float)
        by_source: dict[str, float] = defaultdict(float)
        scope = b["scope"]
        k, v = next(iter(scope.items()))
        with self.engine.connect() as conn:
            if k in ("all", "domain", "owner", "platform", "tables"):
                counts = self._in_scope(scope)
                q = select(asset_costs.c.urn, asset_costs.c.platform, asset_costs.c.day,
                           asset_costs.c.storage_usd + asset_costs.c.compute_usd).where(
                    asset_costs.c.day >= first, asset_costs.c.day <= last)
                if k == "tables":
                    q = q.where(asset_costs.c.urn.in_(v))
                elif k == "platform":
                    q = q.where(asset_costs.c.platform == v)
                for urn, platform, day, usd in conn.execute(q):
                    if usd and counts(urn, platform):
                        by_day[day] += usd
                        by_source[urn] += usd
            if k in ("all", "warehouse"):
                q = select(run_costs.c.job, run_costs.c.day, run_costs.c.usd).where(
                    run_costs.c.day >= first, run_costs.c.day <= last, run_costs.c.parent.is_(None),
                    run_costs.c.outputs == "[]", run_costs.c.usd > 0)  # charged to no table: not counted twice
                if k == "warehouse":
                    q = q.where(run_costs.c.job == f"urn:li:warehouse:{v}")
                for job, day, usd in conn.execute(q):
                    by_day[day] += usd
                    by_source[job] += usd
            if k in ("all", "ai"):
                q = select(usage_table.c.actor, usage_table.c.ts_ms, usage_table.c.cost_usd).where(
                    usage_table.c.ts_ms >= int(start.timestamp() * 1000), usage_table.c.ts_ms < int(end.timestamp() * 1000))
                if k == "ai" and v is not True:
                    q = q.where(usage_table.c.actor == v)
                for actor, ts, usd in conn.execute(q):
                    if usd:
                        day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                        by_day[day] += usd
                        by_source[f"ai:{actor}"] += usd
        spent = round(sum(by_day.values()), 2)
        length = (end - start).total_seconds() * 1000
        elapsed = min(1.0, max(0.0, (now_ms - start.timestamp() * 1000) / length))
        projected = round(spent / elapsed, 2) if elapsed >= FORECAST_AFTER else None
        top = sorted(by_source.items(), key=lambda kv: -kv[1])[:10]
        return {"period_start": first, "period_end": last, "spent_usd": spent, "projected_usd": projected,
                "percent": round(100 * spent / b["amount_usd"], 1), "elapsed": round(elapsed, 3),
                "by_day": [{"day": d, "usd": round(by_day.get(d, 0.0), 2)} for d in days if d <= last and d in by_day],
                "top": [{"source": s, "usd": round(u, 2)} for s, u in top]}

    def status(self, p, bid: str, now_ms: Optional[int] = None) -> dict:
        b = self._get(bid)
        if not self._sees(p, b):
            raise BudgetError("no such budget", 404)
        s = self.spend(b, now_ms)
        with self.engine.connect() as conn:
            sent = [dict(r._mapping) for r in conn.execute(select(budget_alerts).where(
                budget_alerts.c.budget_id == bid, budget_alerts.c.period == s["period_start"]))]
        return {**b, **s, "alerts": [{"kind": a["kind"], "sent_ms": a["sent_ms"], "spent_usd": a["spent_usd"]} for a in sent]}

    # ---- alerts --------------------------------------------------------------------------------
    def check(self, bid: str, now_ms: Optional[int] = None) -> dict:
        """Compares the period's spend with the thresholds and the forecast; each alert once a period."""
        b = self._get(bid)
        now_ms = now_ms or _now()
        s = self.spend(b, now_ms)
        due = [str(t) for t in b["thresholds"] if s["percent"] >= t]
        over_forecast = b["forecast"] and s["projected_usd"] is not None and s["projected_usd"] > b["amount_usd"] \
            and s["spent_usd"] < b["amount_usd"]
        if over_forecast:
            due.append("forecast")
        sent = []
        for kind in due:
            with self.engine.begin() as conn:
                key = and_(budget_alerts.c.budget_id == bid, budget_alerts.c.period == s["period_start"],
                           budget_alerts.c.kind == kind)
                if conn.execute(select(budget_alerts.c.kind).where(key)).first():
                    continue
                conn.execute(insert(budget_alerts).values(budget_id=bid, period=s["period_start"], kind=kind,
                                                          sent_ms=now_ms, spent_usd=s["spent_usd"]))
            sent.append(kind)
        # One message for the highest threshold just crossed, and one for the forecast.
        crossed = [k for k in sent if k != "forecast"]
        recipients = list(dict.fromkeys([b["created_by"], *b["notify"]]))
        if self.notify is not None and crossed:
            top = max(crossed, key=int)
            self.notify("budget.threshold_crossed", f"Budget {b['name']}: {top}% spent",
                        f"${s['spent_usd']:,.2f} of ${b['amount_usd']:,.2f} this {b['period']} "
                        f"(from {s['period_start']}).", recipients,
                        {"budget": bid, "threshold": int(top), "spent_usd": s["spent_usd"], "amount_usd": b["amount_usd"]})
        if self.notify is not None and "forecast" in sent:
            self.notify("budget.forecast_exceeded", f"Budget {b['name']} is on course to be exceeded",
                        f"${s['spent_usd']:,.2f} spent so far; at this rate ${s['projected_usd']:,.2f} by the end of the "
                        f"{b['period']}, against ${b['amount_usd']:,.2f}.", recipients,
                        {"budget": bid, "projected_usd": s["projected_usd"], "amount_usd": b["amount_usd"]})
        with self.engine.begin() as conn:
            conn.execute(update(budgets).where(budgets.c.id == bid).values(checked_ms=now_ms, spent_usd=s["spent_usd"]))
        return {"budget": bid, "sent": sent, **s}

    def run_due(self, now_ms: Optional[int] = None, every_minutes: int = 60) -> int:
        now_ms = now_ms or _now()
        with self.engine.connect() as conn:
            due = [r.id for r in conn.execute(select(budgets.c.id, budgets.c.checked_ms).where(budgets.c.enabled.is_(True)))
                   if not r.checked_ms or now_ms - r.checked_ms >= every_minutes * 60_000]
        n = 0
        for bid in due:
            try:
                self.check(bid, now_ms)
                n += 1
            except Exception:  # noqa: BLE001 - one budget never stops the others
                logger.exception("budget %s could not be checked", bid)
        return n

    def check_now(self, p, bid: str) -> dict:
        if not self.may_operate(p):
            raise BudgetError("checking a budget now is for operators (MANAGE_SYSTEM_OPERATIONS)", 403)
        return self.check(bid)


def build_budgets_router(b: Budgets, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/budgets", tags=["costs"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except BudgetError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        return await call(lambda: b.list(p))

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: b.create(p, body))

    @router.get("/{bid}")
    async def get(bid: str, p=Depends(current_principal)):
        return await call(lambda: b.status(p, bid))

    @router.patch("/{bid}")
    async def change(bid: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: b.update(p, bid, body))

    @router.delete("/{bid}", status_code=204)
    async def remove(bid: str, p=Depends(current_principal)):
        await call(lambda: b.delete(p, bid))

    @router.post("/{bid}/check")
    async def check(bid: str, p=Depends(current_principal)):
        return await call(lambda: b.check_now(p, bid))

    return router
