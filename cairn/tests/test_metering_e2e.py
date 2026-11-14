"""Usage metering end to end: a tenant computes its daily usage from its own records (Trino's query history,
warehouse node time, AI calls, licence seats) and reports it, signed, to the control plane's API; the control
plane keeps it per day and bills settled days to Stripe's meters (stripe-mock), each once. Needs docker."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from test_signup_e2e import _images, api, services  # noqa: F401  (fixtures: stripe-mock, Mailpit, the control plane)

pytestmark = pytest.mark.skipif(not _images(), reason="needs docker, stripe-mock and Mailpit")
MASTER = "metering-master-key-7d1e"


def ms(day: str, hour: int = 12) -> int:
    return int(datetime.fromisoformat(f"{day}T{hour:02d}:00:00+00:00").timestamp() * 1000)


def trino_event(qid: str, day: str, cpu_s: float) -> dict:
    return {"createTime": f"{day}T10:00:00.000Z", "endTime": f"{day}T10:00:05.000Z",
            "metadata": {"queryId": qid, "queryState": "FINISHED", "query": "SELECT 1"},
            "context": {"user": "kim", "source": "cairn-sql"}, "statistics": {"cpuTime": cpu_s, "wallTime": 5.0}}


def test_a_tenants_usage_is_reported_signed_and_billed_once(api, tmp_path):  # noqa: F811
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, select

    from cairn.ai.llm import LLMUsage
    from cairn.ai.usage import UsageMeter
    from cairn.common.db import init_db
    from cairn.common.licence import LicenceService
    from cairn.dataaccess.query_history import QueryHistory
    from cairn.hosting.metering import UsageReporter, metering_sent, sign, tenant_key, tenant_usage
    from cairn.hosting.signup import signups
    from cairn.reports.costs import CostService

    cp, c, _, _ = api
    assert cp.metering is not None
    customer = cp.signup.stripe.post("/v1/customers", {"email": "kim@acme.test"})["id"]
    with cp.engine.begin() as conn:
        conn.execute(signups.insert().values(id="s1", tenant="acme", email="kim@acme.test", organization="Acme", plan="team",
                                             status="active", stripe_customer=customer, created_ms=1, updated_ms=1))

    # The tenant: its own database, with a day's queries, warehouse time, AI calls and people.
    day = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()
    tenant_db = create_engine(f"sqlite:///{tmp_path}/tenant.db")
    init_db(tenant_db)
    qh = QueryHistory(tenant_db)
    for i, cpu in enumerate((3600.4, 1800.2, 0.5)):
        qh.record(trino_event(f"q{i}", day, cpu))
    qh.record(trino_event("other-day", (datetime.fromisoformat(day) - timedelta(days=1)).date().isoformat(), 999))
    costs = CostService(tenant_db, None, lambda u: True)
    costs.record_run("warehouse:r1", None, "urn:li:warehouse:analysts", "warehouse", ms(day, 8), ms(day, 9), [], 3, 1.0)
    meter = UsageMeter(tenant_db)
    meter._now_ms = lambda: ms(day)
    ai = meter.record("urn:li:corpuser:kim", "ask", "anthropic", LLMUsage(model="claude-opus-5-5", input_tokens=200_000,
                                                                           output_tokens=20_000))
    licence = LicenceService(tenant_db)
    for who in ("kim", "lee", "max"):
        licence._touch(f"urn:li:corpuser:{who}")
    reporter = UsageReporter(tenant_db, "acme", "http://testserver", tenant_key(MASTER, "acme"), licence, http=c)
    assert reporter.usage(day) == {"seats": 3, "query_cpu_seconds": 5401, "warehouse_node_seconds": 10800,
                                   "ai_usd_cents": round(ai * 100)}

    # Reported once the day is over (by an hour); the control plane keeps it.
    now = ms((datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat(), 2)
    assert day in reporter.report(now, days_back=1)
    assert reporter.report(now, days_back=1) == []                               # accepted: not sent again
    ops = TestClient(c.app, headers={"Authorization": "Bearer svc-secret"})
    got = {r["metric"]: r["value"] for r in ops.get("/v1/metering/acme").json() if r["day"] == day}
    assert got == {"seats": 3, "query_cpu_seconds": 5401, "warehouse_node_seconds": 10800, "ai_usd_cents": round(ai * 100)}

    # Only the tenant's key signs its reports; days must be over; unknown tenants are refused.
    body = json.dumps({"tenant": "acme", "day": day, "metrics": {"seats": 999}}).encode()
    assert c.post("/v1/metering/report", content=body, headers={"X-Cairn-Signature": sign("wrong", body)}).status_code == 401
    other = json.dumps({"tenant": "acme", "day": day, "metrics": {"seats": 999}}).encode()
    assert c.post("/v1/metering/report", content=other,
                  headers={"X-Cairn-Signature": sign(tenant_key(MASTER, "beta"), other)}).status_code == 401
    today = json.dumps({"tenant": "acme", "day": datetime.now(timezone.utc).date().isoformat(), "metrics": {}}).encode()
    assert c.post("/v1/metering/report", content=today,
                  headers={"X-Cairn-Signature": sign(tenant_key(MASTER, "acme"), today)}).status_code == 400
    ghost = json.dumps({"tenant": "ghost", "day": day, "metrics": {"seats": 1}}).encode()
    assert c.post("/v1/metering/report", content=ghost,
                  headers={"X-Cairn-Signature": sign(tenant_key(MASTER, "ghost"), ghost)}).status_code == 404

    # Billed once the day has settled, as Stripe meter events (stripe-mock), each metric with a meter once.
    m = cp.metering
    assert m.bill(now) == 0                                                     # not settled yet (6 hours)
    settled = now + 6 * 3_600_000
    assert m.bill(settled) == 3                                                 # seats has no meter: not billed
    assert m.bill(settled) == 0
    with cp.engine.connect() as conn:
        rows = {r.metric: r for r in conn.execute(select(tenant_usage).where(tenant_usage.c.day == day))}
    assert rows["query_cpu_seconds"].billed_ms and rows["seats"].billed_ms is None and rows["ai_usd_cents"].error is None
    # A late report after billing changes nothing billed.
    late = json.dumps({"tenant": "acme", "day": day, "metrics": {"query_cpu_seconds": 1}}).encode()
    assert c.post("/v1/metering/report", content=late,
                  headers={"X-Cairn-Signature": sign(tenant_key(MASTER, "acme"), late)}).json()["kept"] == 0
    with tenant_db.connect() as conn:
        assert [r.day for r in conn.execute(select(metering_sent))] == [day]
