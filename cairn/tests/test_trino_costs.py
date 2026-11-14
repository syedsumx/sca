"""Cost per query, person, team and table on Trino (L60): each query priced from its CPU time and the bytes it
scanned, split evenly across a person's teams and the tables it read, and added to the per-table cost report.
(On real Trino: test_trino_costs_come_from_real_queries in test_trino_e2e.py.)"""

from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from cairn.common.db import get_engine, init_db
from cairn.dataaccess.query_history import price, query_history
from cairn.reports.costs import CostService

ORDERS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.customers,PROD)"
HOUR, TB = 3_600_000, 10**12
GROUPS = {"urn:li:corpuser:ana": ["urn:li:corpGroup:finance", "urn:li:corpGroup:analysts"],
          "urn:li:corpuser:bob": ["urn:li:corpGroup:sales"]}


@pytest.fixture
def costs(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_COST_TRINO_USD_PER_CPU_HOUR", "2")
    monkeypatch.setenv("CAIRN_COST_TRINO_USD_PER_TB_SCANNED", "5")
    monkeypatch.delenv("CAIRN_COST_TEAMS", raising=False)
    engine = get_engine(f"sqlite:///{tmp_path}/c.db")
    init_db(engine)
    now = int(time.time() * 1000)
    rows = [("q1", "ana", HOUR, TB, [ORDERS]),                     # $2 + $5 = $7, all on orders
            ("q2", "ana", 2 * HOUR, 0, [ORDERS, CUSTOMERS]),        # $4, half on each table
            ("q3", "bob", 0, 2 * TB, [CUSTOMERS]),                  # $10 on customers
            ("q4", "bob", HOUR // 2, 0, [])]                        # $1, no table (SELECT 1)
    with engine.begin() as conn:
        for qid, user, cpu, scanned, tables in rows:
            conn.execute(query_history.insert().values(
                query_id=qid, user=user, source="", state="FINISHED", sql="select", create_ms=now - 1000, wall_ms=1,
                cpu_ms=cpu, queued_ms=0, input_bytes=scanned, input_rows=0, output_rows=0, peak_memory_bytes=0,
                tables=json.dumps(tables), server="", splits=0))
        conn.execute(query_history.insert().values(                 # older than the window: not counted
            query_id="old", user="ana", source="", state="FINISHED", sql="select", create_ms=now - 40 * 86_400_000,
            wall_ms=1, cpu_ms=100 * HOUR, queued_ms=0, input_bytes=0, input_rows=0, output_rows=0, peak_memory_bytes=0,
            tables=json.dumps([ORDERS]), server="", splits=0))
    return CostService(engine, None, lambda urn: False)


def by_key(report):
    return {r["key"]: r for r in report["rows"]}


def test_a_query_is_priced_from_its_cpu_and_scanned_bytes(monkeypatch):
    monkeypatch.setenv("CAIRN_COST_TRINO_USD_PER_CPU_HOUR", "2")
    monkeypatch.setenv("CAIRN_COST_TRINO_USD_PER_TB_SCANNED", "5")
    assert price(HOUR, TB) == 7.0 and price(HOUR // 4, TB // 10) == 1.0
    monkeypatch.delenv("CAIRN_COST_TRINO_USD_PER_TB_SCANNED")
    assert price(HOUR, TB) == 2.0                                    # CPU only
    monkeypatch.delenv("CAIRN_COST_TRINO_USD_PER_CPU_HOUR")
    assert price(HOUR, TB) is None                                   # not priced


def test_per_person_team_and_table(costs):
    people = costs.trino("user", 30)
    assert people["priced"] and people["total_usd"] == 22.0
    assert {k: r["usd"] for k, r in by_key(people).items()} == {"urn:li:corpuser:bob": 11.0, "urn:li:corpuser:ana": 11.0}
    assert by_key(people)["urn:li:corpuser:ana"]["queries"] == 2 and by_key(people)["urn:li:corpuser:ana"]["cpu_hours"] == 3
    # A person's queries split evenly among their teams; the total stays the same.
    teams = costs.trino("team", 30, groups_of=lambda u: GROUPS.get(u, []))
    assert {k: r["usd"] for k, r in by_key(teams).items()} == {
        "urn:li:corpGroup:sales": 11.0, "urn:li:corpGroup:finance": 5.5, "urn:li:corpGroup:analysts": 5.5}
    assert teams["total_usd"] == 22.0
    # Each query's cost splits evenly across the tables it read; one that read none has no table to go to.
    tables = costs.trino("table", 30)
    assert {k: r["usd"] for k, r in by_key(tables).items()} == {CUSTOMERS: 12.0, ORDERS: 9.0}
    assert by_key(tables)[ORDERS]["queries"] == 2 and by_key(tables)[ORDERS]["scanned_bytes"] == TB
    # Narrowed to one person, and the older query outside the window.
    assert {r["key"] for r in costs.trino("table", 30, user="bob")["rows"]} == {CUSTOMERS}
    assert costs.trino("user", 60)["total_usd"] == 222.0


def test_teams_can_be_limited_and_unknown_groups_are_unassigned(costs, monkeypatch):
    monkeypatch.setenv("CAIRN_COST_TEAMS", "urn:li:corpGroup:finance")
    teams = by_key(costs.trino("team", 30, groups_of=lambda u: GROUPS.get(u, [])))
    assert teams["urn:li:corpGroup:finance"]["usd"] == 11.0 and teams["(no team)"]["usd"] == 11.0

    def broken(u):
        raise RuntimeError("directory down")
    assert {r["key"] for r in costs.trino("team", 30, groups_of=broken)["rows"]} == {"(no team)"}
    with pytest.raises(ValueError):
        costs.trino("team", 30)
    with pytest.raises(ValueError):
        costs.trino("query", 30)


def test_unpriced_trino_still_reports_cpu_and_bytes(costs, monkeypatch):
    monkeypatch.delenv("CAIRN_COST_TRINO_USD_PER_CPU_HOUR")
    monkeypatch.delenv("CAIRN_COST_TRINO_USD_PER_TB_SCANNED")
    r = costs.trino("user", 30)
    assert not r["priced"] and r["total_usd"] is None and by_key(r)["urn:li:corpuser:ana"]["usd"] is None
    assert by_key(r)["urn:li:corpuser:ana"]["cpu_hours"] == 3


def test_the_table_cost_report_includes_trino(costs):
    exp = {t["urn"]: t for t in costs.expensive(30, min_cost_usd=0)}
    assert exp[CUSTOMERS]["compute_usd"] == 12.0 and exp[CUSTOMERS]["reads"] == 2
    assert exp[ORDERS]["compute_usd"] == 9.0 and exp[ORDERS]["reads"] == 2
    assert exp[ORDERS]["platform"] == "iceberg"
    assert costs.idle(30) == []                                       # read tables are not idle


def test_the_api_shows_people_their_own_and_operators_everything(costs):
    from cairn.reports.api import build_router

    class P:
        def __init__(self, urn):
            self.urn = urn

    def principal(request: Request):
        return P(request.headers["x-user"])

    def authorize(p, privilege, urn, raise_on_deny=True):
        return p.urn == "urn:li:corpuser:ops"

    class Reports:
        pass
    app = FastAPI()
    app.include_router(build_router(Reports(), principal, authorize, lambda p: (lambda urn: urn != ORDERS or
                                                                                p.urn == "urn:li:corpuser:ops"),
                                    lambda urns: {u: {"name": u.split(",")[1]} for u in urns}, costs,
                                    lambda u: GROUPS.get(u, [])))
    c = TestClient(app)
    ops = c.get("/reports/costs/trino", params={"by": "user"}, headers={"x-user": "urn:li:corpuser:ops"}).json()
    assert ops["scope"] == "all" and ops["total_usd"] == 22.0
    ana = c.get("/reports/costs/trino", params={"by": "user"}, headers={"x-user": "urn:li:corpuser:ana"}).json()
    assert ana["scope"] == "mine" and [r["key"] for r in ana["rows"]] == ["urn:li:corpuser:ana"]
    # By table, only tables the person may see, with their names.
    bob = c.get("/reports/costs/trino", params={"by": "table"}, headers={"x-user": "urn:li:corpuser:bob"}).json()
    assert [(r["key"], r["name"]) for r in bob["rows"]] == [(CUSTOMERS, "sales.customers")]
    ana_t = c.get("/reports/costs/trino", params={"by": "table"}, headers={"x-user": "urn:li:corpuser:ana"}).json()
    assert ORDERS not in {r["key"] for r in ana_t["rows"]}           # not hers to see
    assert c.get("/reports/costs/trino", params={"by": "team"}, headers={"x-user": "urn:li:corpuser:ops"}).json()["rows"]
    assert c.get("/reports/costs/trino", params={"by": "x"}, headers={"x-user": "urn:li:corpuser:ops"}).status_code == 422
    assert c.get("/reports/costs/trino", headers={"x-user": "urn:li:service:etl"}).status_code == 403
