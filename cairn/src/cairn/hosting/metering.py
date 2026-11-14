"""Usage metering for hosted tenants: each tenant reports its daily usage to the control plane, which bills it
through Stripe's meters.

**In a tenant** (``CAIRN_METERING_URL``, ``CAIRN_METERING_KEY_FILE`` and the tenant's ``CAIRN_TENANT_ID``, set by
``tenant.sh --metering-key``),
the notifier reports each finished day (UTC) once it is over by an hour, and sends it again until the control
plane accepts it. A day's usage, all whole numbers:

* ``seats``: people active in the last 30 days, as the licence counts them;
* ``query_cpu_seconds``: CPU time of the day's finished Trino queries (Trino's query history);
* ``warehouse_node_seconds``: node time of the team warehouses (what their proxy reports);
* ``ai_usd_cents``: AI calls through Cairn, at the providers' prices.

The report is JSON, signed with ``X-Cairn-Signature: sha256=<hex HMAC-SHA256 of the body>`` with the tenant's
metering key, and carries the tenant and the day.

**On the control plane** (``CAIRN_SIGNUP`` with ``metering_key_file`` and ``meters``),
``POST /v1/metering/report`` checks the signature against the tenant's key (derived from the control plane's
metering key and the tenant's name, so no per-tenant secret is stored), and keeps one value per tenant, day
and metric (a later report of the same day replaces it until it is billed). Once a day has settled
(``settle_hours`` after it ended, default 6), the notifier sends each metric that has a Stripe meter
(``meters``: ``{"query_cpu_seconds": "cairn_cpu_seconds", ...}``) as a meter event for the tenant's Stripe
customer, with the identifier ``<tenant>-<day>-<metric>``, so Stripe counts it once whatever is retried.
``GET /v1/metering/<tenant>`` (with the control plane's operator token) lists a tenant's usage.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import BigInteger, Column, String, Table, Text, and_, func, insert, select, update
from starlette.requests import Request  # module level: FastAPI resolves the endpoints' annotations here

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

METRICS = ("seats", "query_cpu_seconds", "warehouse_node_seconds", "ai_usd_cents")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

metering_sent = Table(                                  # in a tenant: the days the control plane accepted
    "cairn_metering_sent", metadata,
    Column("day", String(10), primary_key=True),
    Column("sent_ms", BigInteger, nullable=False),
    Column("report", Text, nullable=False),
)

tenant_usage = Table(                                   # on the control plane: what each tenant used, per day
    "cairn_tenant_usage", metadata,
    Column("tenant", String(32), primary_key=True),
    Column("day", String(10), primary_key=True),
    Column("metric", String(64), primary_key=True),
    Column("value", BigInteger, nullable=False),
    Column("received_ms", BigInteger, nullable=False),
    Column("billed_ms", BigInteger, nullable=True),
    Column("error", Text, nullable=True),
)


class MeteringError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def tenant_key(master: str, tenant: str) -> str:
    """A tenant's metering key, derived from the control plane's: HMAC-SHA256(master, "metering:" + tenant)."""
    return hmac.new(master.encode(), f"metering:{tenant}".encode(), hashlib.sha256).hexdigest()


def sign(key: str, body: bytes) -> str:
    return "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


def _bounds(day: str) -> tuple[int, int]:
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


# --------------------------------------------------------------------------- in a tenant
class UsageReporter:
    def __init__(self, engine, tenant: str, url: str, key: str, licence=None, http=None):
        self.engine, self.tenant, self.url, self.key, self.licence, self.http = engine, tenant, url.rstrip("/"), key, licence, http

    @classmethod
    def from_env(cls, engine, licence=None) -> Optional["UsageReporter"]:
        url, key_file = os.environ.get("CAIRN_METERING_URL", ""), os.environ.get("CAIRN_METERING_KEY_FILE", "")
        if not url:
            return None
        tenant = os.environ.get("CAIRN_TENANT_ID", "")
        if not tenant or not key_file or not os.path.isfile(key_file):
            raise ValueError("CAIRN_METERING_URL needs CAIRN_METERING_KEY_FILE and the tenant (CAIRN_TENANT_ID)")
        with open(key_file) as f:
            return cls(engine, tenant, url, f.read().strip(), licence)

    def usage(self, day: str) -> dict:
        from cairn.ai.usage import usage_table
        from cairn.dataaccess.query_history import query_history
        from cairn.reports.costs import run_costs

        start, end = _bounds(day)
        with self.engine.connect() as conn:
            cpu_ms = conn.execute(select(func.coalesce(func.sum(query_history.c.cpu_ms), 0)).where(
                query_history.c.create_ms >= start, query_history.c.create_ms < end,
                query_history.c.state == "FINISHED")).scalar()
            node = conn.execute(select(func.coalesce(func.sum(run_costs.c.cores * run_costs.c.seconds), 0)).where(
                run_costs.c.day == day, run_costs.c.engine == "warehouse")).scalar()
            ai = conn.execute(select(func.coalesce(func.sum(usage_table.c.cost_usd), 0)).where(
                usage_table.c.ts_ms >= start, usage_table.c.ts_ms < end)).scalar()
        seats = 0
        if self.licence is not None:
            try:
                seats = int(self.licence.seats_used())
            except Exception:  # noqa: BLE001 - reported as 0 rather than not at all
                logger.warning("metering: active users unavailable", exc_info=True)
        return {"seats": seats, "query_cpu_seconds": int(round((cpu_ms or 0) / 1000)),
                "warehouse_node_seconds": int(round(node or 0)), "ai_usd_cents": int(round((ai or 0) * 100))}

    def report(self, now_ms: Optional[int] = None, days_back: int = 7) -> list[str]:
        """Sends each finished day (over by an hour) the control plane has not accepted yet."""
        import httpx

        now = datetime.fromtimestamp((now_ms or _now()) / 1000, tz=timezone.utc)
        last = (now - timedelta(hours=1)).date() - timedelta(days=1)
        with self.engine.connect() as conn:
            sent = {r.day for r in conn.execute(select(metering_sent.c.day))}
        done = []
        for i in range(days_back - 1, -1, -1):
            day = (last - timedelta(days=i)).isoformat()
            if day in sent:
                continue
            body = json.dumps({"tenant": self.tenant, "day": day, "metrics": self.usage(day),
                               "sent_ms": now_ms or _now()}, sort_keys=True).encode()
            client = self.http or httpx.Client(timeout=30)
            try:
                r = client.post(f"{self.url}/v1/metering/report", content=body, headers={
                    "Content-Type": "application/json", "X-Cairn-Signature": sign(self.key, body)})
            except Exception as e:  # noqa: BLE001 - sent again next time
                logger.warning("metering: the control plane is unreachable: %s", e)
                break
            finally:
                if self.http is None:
                    client.close()
            if r.status_code >= 300:
                logger.warning("metering: the control plane refused %s: %s %s", day, r.status_code, r.text[:200])
                break
            with self.engine.begin() as conn:
                conn.execute(insert(metering_sent).values(day=day, sent_ms=_now(), report=body.decode()))
            done.append(day)
        return done


# --------------------------------------------------------------------------- on the control plane
class Metering:
    def __init__(self, engine, master_key: str, meters: dict, stripe, customer_of, settle_hours: int = 6):
        """``customer_of(tenant)``: the tenant's Stripe customer (None: not billed)."""
        self.engine, self.master, self.meters, self.stripe = engine, master_key, meters, stripe
        self.customer_of, self.settle_hours = customer_of, settle_hours

    def receive(self, body: bytes, signature: str) -> dict:
        try:
            report = json.loads(body)
            tenant, day, metrics = str(report["tenant"]), str(report["day"]), report["metrics"]
        except (ValueError, KeyError, TypeError):
            raise MeteringError("a report is {tenant, day, metrics}")
        if not hmac.compare_digest(sign(tenant_key(self.master, tenant), body), signature or ""):
            raise MeteringError("bad signature", 401)
        if not DAY.match(day) or date.fromisoformat(day) >= datetime.now(timezone.utc).date():
            raise MeteringError("day: a finished day (UTC)")
        if not isinstance(metrics, dict) or not all(
                k in METRICS and isinstance(v, int) and not isinstance(v, bool) and v >= 0 for k, v in metrics.items()):
            raise MeteringError(f"metrics: whole numbers for {', '.join(METRICS)}")
        if self.customer_of(tenant) is None:
            raise MeteringError(f"no tenant {tenant}", 404)
        now, kept = _now(), 0
        with self.engine.begin() as conn:
            for metric, value in metrics.items():
                key = and_(tenant_usage.c.tenant == tenant, tenant_usage.c.day == day, tenant_usage.c.metric == metric)
                row = conn.execute(select(tenant_usage.c.billed_ms).where(key)).first()
                if row is None:
                    conn.execute(insert(tenant_usage).values(tenant=tenant, day=day, metric=metric, value=value,
                                                             received_ms=now))
                    kept += 1
                elif row.billed_ms is None:                  # not billed yet: the latest report wins
                    conn.execute(update(tenant_usage).where(key).values(value=value, received_ms=now))
                    kept += 1
        return {"tenant": tenant, "day": day, "kept": kept}

    def bill(self, now_ms: Optional[int] = None) -> int:
        """Sends settled, unbilled usage to Stripe's meters, each once (its identifier)."""
        now = now_ms or _now()
        with self.engine.connect() as conn:
            rows = conn.execute(select(tenant_usage).where(tenant_usage.c.billed_ms.is_(None))
                                .order_by(tenant_usage.c.day)).all()
        n = 0
        for r in rows:
            event = self.meters.get(r.metric)
            if not event:
                continue
            _, end = _bounds(r.day)
            if now < end + self.settle_hours * 3_600_000:
                continue
            customer = self.customer_of(r.tenant)
            key = and_(tenant_usage.c.tenant == r.tenant, tenant_usage.c.day == r.day, tenant_usage.c.metric == r.metric)
            try:
                if not customer:
                    raise MeteringError(f"{r.tenant} has no Stripe customer")
                self.stripe.post("/v1/billing/meter_events", {
                    "event_name": event, "identifier": f"{r.tenant}-{r.day}-{r.metric}", "timestamp": end // 1000 - 1,
                    "payload": {"stripe_customer_id": customer, "value": str(r.value)}},
                    idempotency_key=f"meter-{r.tenant}-{r.day}-{r.metric}")
                with self.engine.begin() as conn:
                    conn.execute(update(tenant_usage).where(key).values(billed_ms=_now(), error=None))
                n += 1
            except Exception as e:  # noqa: BLE001 - kept, sent again next time
                with self.engine.begin() as conn:
                    conn.execute(update(tenant_usage).where(key).values(error=str(e)[:2000]))
        return n

    def usage(self, tenant: str, days: int = 31) -> list[dict]:
        since = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(select(tenant_usage).where(
                tenant_usage.c.tenant == tenant, tenant_usage.c.day >= since).order_by(tenant_usage.c.day, tenant_usage.c.metric))]


def build_metering_router(m: Metering, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(tags=["hosting"])

    @router.post("/v1/metering/report")
    async def report(request: Request):
        try:
            return await run_in_threadpool(m.receive, await request.body(), request.headers.get("x-cairn-signature", ""))
        except MeteringError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/v1/metering/{tenant}")
    def usage(tenant: str, days: int = 31, p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return m.usage(tenant, max(1, min(days, 400)))

    return router

