"""Cost budgets through the Cairn API: spend from the cost report (tables, warehouse node-hours, AI calls) per
scope and period, each threshold and the forecast alerted once a period to the people the budget names (their
in-app inbox, through the notification hub), and budgets that only operators set."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.common.security import Principal
from cairn.governance.abac import Policy, PolicyBundle, Target
from cairn.services import build_services

OPS, KIM, LEE = "urn:li:corpuser:ops", "urn:li:corpuser:kim", "urn:li:corpuser:lee"
FIN = "urn:li:domain:finance"
GL = "urn:li:dataset:(urn:li:dataPlatform:snowflake,fin.ledger,PROD)"
AR = "urn:li:dataset:(urn:li:dataPlatform:snowflake,fin.receivables,PROD)"
WEB = "urn:li:dataset:(urn:li:dataPlatform:iceberg,web.clicks,PROD)"


def ms(day: str, hour: int = 12) -> int:
    return int(datetime.fromisoformat(f"{day}T{hour:02d}:00:00+00:00").timestamp() * 1000)


@pytest.fixture
def api(settings, graph, monkeypatch):
    monkeypatch.setenv("CAIRN_WAREHOUSES", '{"warehouses": [{"name": "analysts", "port": 1, "upstream": "http://x"}]}')
    graph.aspects[(GL, "domains")] = {"domains": [FIN]}
    graph.aspects[(AR, "domains")] = {"domains": [FIN]}
    graph.aspects[(WEB, "ownership")] = {"owners": [{"owner": "urn:li:corpGroup:web", "type": "TECHNICAL_OWNER"}]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(
        id="ops", effect="allow", actions=["MANAGE_SYSTEM_OPERATIONS", "MANAGE_GLOBAL_SETTINGS"], target=Target(),
        condition={"attr": "subject.urn", "op": "eq", "value": OPS})]))
    app = create_api_app(s)
    return s, {u: TestClient(app, headers={"X-Cairn-Actor": u}) for u in (OPS, KIM, LEE)}


def test_spend_per_scope_alerts_once_per_period(api):
    from cairn.ai.llm import LLMUsage
    from cairn.ai.usage import UsageMeter
    from cairn.reports.costs import CostRow

    s, c = api
    # The cost report: tables (posted, as any source not collected by Cairn), a warehouse, AI calls.
    rows = [{"urn": GL, "day": "2026-11-02", "storage_usd": 10, "compute_usd": 30},
            {"urn": AR, "day": "2026-11-03", "storage_usd": 5, "compute_usd": 15},
            {"urn": WEB, "day": "2026-11-03", "compute_usd": 100},
            {"urn": GL, "day": "2026-10-30", "compute_usd": 999}]                     # last month
    assert c[KIM].post("/reports/costs", json={"rows": rows}).status_code == 403
    assert c[OPS].post("/reports/costs", json={"rows": rows}).json() == {"ingested": 4}
    s.costs.record_run("warehouse:r1", None, "urn:li:warehouse:analysts", "warehouse", ms("2026-11-03", 8),
                       ms("2026-11-03", 10), [], 3, 2.0)                                # 3 nodes x 2 h x $2 = $12
    meter = UsageMeter(s.engine)
    meter._now_ms = lambda: ms("2026-11-03")
    ai_usd = meter.record(KIM, "ask", "anthropic", LLMUsage(model="claude-opus-5-5", input_tokens=1_000_000,
                                                            output_tokens=100_000))
    assert ai_usd > 0

    # Only operators set budgets; bad ones are refused.
    fin = {"name": "Finance", "amount_usd": 100, "scope": {"domain": FIN}, "notify": [KIM], "thresholds": [50, 80, 100]}
    assert c[KIM].post("/v1/budgets", json=fin).status_code == 403
    for bad, msg in [({**fin, "scope": {"team": "x"}}, "scope"), ({**fin, "amount_usd": 0}, "amount_usd"),
                     ({**fin, "period": "year"}, "period"), ({**fin, "scope": {"warehouse": "nope"}}, "warehouse"),
                     ({**fin, "notify": ["kim"]}, "notify")]:
        r = c[OPS].post("/v1/budgets", json=bad)
        assert r.status_code == 400 and msg in r.json()["detail"], (bad, r.text)
    r = c[OPS].post("/v1/budgets", json=fin)
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    assert c[OPS].post("/v1/budgets", json=fin).status_code == 409
    wh = c[OPS].post("/v1/budgets", json={"name": "Analysts warehouse", "amount_usd": 10, "scope": {"warehouse": "analysts"},
                                          "period": "week", "thresholds": [100]}).json()["id"]
    web = c[OPS].post("/v1/budgets", json={"name": "Web team", "amount_usd": 1000, "scope": {"owner": "urn:li:corpGroup:web"},
                                           "notify": [LEE]}).json()["id"]
    every = c[OPS].post("/v1/budgets", json={"name": "Everything", "amount_usd": 5000, "scope": {"all": True}}).json()["id"]

    now = ms("2026-11-04")
    b = s.budgets
    st = b.status(Principal(OPS, "token"), fid, now)
    assert st["spent_usd"] == 60 and st["period_start"] == "2026-11-01" and st["period_end"] == "2026-11-30"
    assert [t["source"] for t in st["top"]] == [GL, AR] and st["projected_usd"] is None   # too early to forecast
    assert b.spend(b._get(wh), now)["spent_usd"] == 12
    assert b.spend(b._get(web), now)["spent_usd"] == 100
    assert b.spend(b._get(every), now)["spent_usd"] == round(60 + 100 + 12 + ai_usd, 2)

    # Finance has spent 60%: the 50% alert goes to Kim and the creator, once.
    out = b.check(fid, now)
    assert out["sent"] == ["50"]
    assert b.check(fid, now)["sent"] == []
    inbox = c[KIM].get("/notify/inbox").json()
    assert [n["title"] for n in inbox] == ["Budget Finance: 50% spent"], inbox
    assert c[OPS].get("/notify/inbox").json()[0]["title"] == "Budget Finance: 50% spent"
    assert c[LEE].get("/notify/inbox").json() == []
    # More is spent: 80% and 100% cross together, one message for the highest.
    s.costs.ingest([CostRow(urn=AR, day="2026-11-04", compute_usd=45)])
    out = b.check(fid, ms("2026-11-05"))
    assert sorted(out["sent"]) == ["100", "80"] and out["spent_usd"] == 105
    assert [n["title"] for n in c[KIM].get("/notify/inbox").json()][0] == "Budget Finance: 100% spent"
    assert len(c[KIM].get("/notify/inbox").json()) == 2
    # A new month: the thresholds start again (nothing spent yet, nothing sent).
    assert b.check(fid, ms("2026-12-02"))["sent"] == [] and b.status(Principal(OPS, "token"), fid,
                                                                       ms("2026-12-02"))["spent_usd"] == 0

    # The forecast: the web team spent 100 in the first 7 days of 30: on course for ~429, under 1000; then
    # a budget of 300 is on course to be exceeded, said once.
    assert b.check(web, ms("2026-11-07"))["sent"] == []
    c[OPS].patch(f"/v1/budgets/{web}", json={"amount_usd": 300})
    out = b.check(web, ms("2026-11-07"))
    assert out["sent"] == ["forecast"] and out["projected_usd"] > 300
    assert c[LEE].get("/notify/inbox").json()[0]["title"] == "Budget Web team is on course to be exceeded"
    assert b.check(web, ms("2026-11-08"))["sent"] == []

    # Who sees what.
    assert {x["name"] for x in c[KIM].get("/v1/budgets").json()} == {"Finance"}
    assert {x["name"] for x in c[OPS].get("/v1/budgets").json()} == {"Finance", "Analysts warehouse", "Web team", "Everything"}
    assert c[LEE].get(f"/v1/budgets/{fid}").status_code == 404
    got = c[KIM].get(f"/v1/budgets/{fid}").json()
    assert got["name"] == "Finance" and got["scope"] == {"domain": FIN}
    assert c[KIM].post(f"/v1/budgets/{fid}/check").status_code == 403
    assert c[OPS].post(f"/v1/budgets/{fid}/check").status_code == 200
    assert c[OPS].delete(f"/v1/budgets/{every}").status_code == 204
    assert c[OPS].get(f"/v1/budgets/{every}").status_code == 404
    assert {e["action"] for e in s.audit.entries("costs", limit=20)} >= {"budget.created", "budget.changed", "budget.deleted"}


def test_periods_are_calendar_periods_in_utc():
    from cairn.reports.budgets import period_bounds

    now = int(datetime(2026, 11, 4, 15, tzinfo=timezone.utc).timestamp() * 1000)     # a Wednesday
    assert [d.strftime("%Y-%m-%d") for d in period_bounds("month", now)] == ["2026-11-01", "2026-12-01"]
    assert [d.strftime("%Y-%m-%d") for d in period_bounds("week", now)] == ["2026-11-02", "2026-11-09"]
    assert [d.strftime("%Y-%m-%d") for d in period_bounds("day", now)] == ["2026-11-04", "2026-11-05"]
    feb = int(datetime(2028, 2, 10, tzinfo=timezone.utc).timestamp() * 1000)
    assert period_bounds("month", feb)[1].day == 1 and (period_bounds("month", feb)[1] - period_bounds("month", feb)[0]).days == 29
