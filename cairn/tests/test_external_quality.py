"""Other quality tools' results: dbt tests, Great Expectations and Soda outputs parsed to the dataset
and column they check, kept with history, sent to Cairn Core as assertions, and counted in health."""

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.quality.external import ExternalError, ExternalResults, parse_dbt, parse_great_expectations, parse_soda
from cairn.services import build_services

ADMIN, ANA = "urn:li:corpuser:admin", "urn:li:corpuser:ana"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.core.orders,PROD)"

MANIFEST = {"metadata": {"adapter_type": "snowflake"}, "nodes": {
    "model.shop.orders": {"database": "ANALYTICS", "schema": "CORE", "alias": "ORDERS", "name": "orders"},
    "test.shop.not_null_orders_id.abc": {"name": "not_null_orders_id", "column_name": "id",
                                        "depends_on": {"nodes": ["model.shop.orders"]}},
    "test.shop.accepted_values_orders_status.def": {
        "name": "accepted_values_orders_status", "test_metadata": {"kwargs": {"column_name": "status"}},
        "depends_on": {"nodes": ["macro.dbt.test_accepted_values", "model.shop.orders"]}}}}
RUN_RESULTS = {"metadata": {"generated_at": "2026-10-30T02:00:00Z"}, "results": [
    {"unique_id": "test.shop.not_null_orders_id.abc", "status": "pass", "failures": 0, "message": None},
    {"unique_id": "test.shop.accepted_values_orders_status.def", "status": "fail", "failures": 3,
     "message": "Got 3 results, configured to fail if != 0"},
    {"unique_id": "model.shop.orders", "status": "success"}]}

GX = {"success": False, "meta": {"batch_spec": {"table_name": "orders", "schema_name": "core"},
                                 "run_id": {"run_time": "2026-10-30T03:00:00Z"}},
      "results": [{"success": True, "expectation_config": {"expectation_type": "expect_column_values_to_not_be_null",
                                                           "kwargs": {"column": "id"}}, "result": {}},
                  {"success": False, "expectation_config": {"expectation_type": "expect_column_values_to_be_between",
                                                            "kwargs": {"column": "amount", "min_value": 0}},
                   "result": {"unexpected_count": 2, "partial_unexpected_list": [-5, -1]}}]}

SODA = {"dataTimestamp": "2026-10-30T04:00:00Z", "checks": [
    {"name": "row_count > 0", "table": "orders", "outcome": "pass", "diagnostics": {"value": 1200}},
    {"name": "missing_count(email) = 0", "table": "customers", "column": "email", "outcome": "warn",
     "diagnostics": {"value": 4}}]}


def test_parsers():
    dbt = parse_dbt({"run_results": RUN_RESULTS, "manifest": MANIFEST}, "postgres")
    assert [(r["dataset"], r["column"], r["status"]) for r in dbt] == [(ORDERS, "id", "PASS"), (ORDERS, "status", "FAIL")]
    assert "3 results" in dbt[1]["message"]
    gx = parse_great_expectations(GX, "snowflake", None)
    assert gx[1]["dataset"] == "urn:li:dataset:(urn:li:dataPlatform:snowflake,core.orders,PROD)"
    assert gx[1]["name"] == "expect_column_values_to_be_between(amount)" and "-5" in gx[1]["message"]
    soda = parse_soda(SODA, "postgres")
    assert soda[1]["status"] == "WARN" and soda[1]["dataset"].endswith(",customers,PROD)")
    with pytest.raises(ExternalError):
        parse_dbt({}, "x")


def test_ingest_history_assertions_and_owners_told(engine, graph):
    from cairn.notify.hub import NotificationHub

    graph.aspects[(ORDERS, "ownership")] = {"owners": [{"owner": ANA}]}
    hub = NotificationHub(engine, "https://cairn", "s", owners=lambda urn: [ANA] if urn == ORDERS else [])
    ext = ExternalResults(engine, graph, hub)
    out = ext.ingest("dbt", {"run_results": RUN_RESULTS, "manifest": MANIFEST})
    assert out == {"tool": "dbt", "results": 2, "datasets": 1, "failing": 1, "newly_failing": 1}
    told = [n for n in hub.inbox(ANA) if n["type"] == "quality.external_failed"]
    assert len(told) == 1 and "accepted_values_orders_status" in told[0]["title"]
    ext.ingest("dbt", {"run_results": RUN_RESULTS, "manifest": MANIFEST})
    assert len([n for n in hub.inbox(ANA) if n["type"] == "quality.external_failed"]) == 1      # still failing
    checks = ext.for_dataset(ORDERS)
    assert [c["status"] for c in checks] == ["FAIL", "PASS"] and len(checks[0]["history"]) == 2
    assertions = [u for (u, a) in graph.aspects if a == "assertionInfo"]
    assert len(assertions) == 2
    info = graph.aspects[(assertions[0], "assertionInfo")]
    assert info["source"]["type"] == "EXTERNAL" and info["customAssertion"]["entity"] == ORDERS
    assert graph.aspects[(assertions[0], "dataPlatformInstance")]["platform"] == "urn:li:dataPlatform:dbt"
    events = [v for (u, a), vs in graph.timeseries.items() if a == "assertionRunEvent" for v in vs]
    assert {e["result"]["type"] for e in events} == {"SUCCESS", "FAILURE"}


def test_api_and_health(settings, graph):
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE"], condition={"attr": "subject.urn", "op": "exists"})]))
    c = TestClient(create_api_app(s))
    body = {"results": [{"table": "analytics.core.orders", "check": "freshness < 2h", "status": "fail",
                         "message": "last load 5h ago", "url": "https://getmontecarlo.com/alerts/1"}]}
    assert c.post("/quality/external/monte_carlo", json=body, headers={"X-Cairn-Actor": ANA}).status_code == 403
    assert c.post("/quality/external/nope", json=body, headers={"X-Cairn-Actor": ADMIN}).status_code == 422
    assert c.post("/quality/external/generic", json={"results": [{"check": "x", "status": "fail"}]},
                  headers={"X-Cairn-Actor": ADMIN}).status_code == 422
    r = c.post("/quality/external/monte_carlo", json=body, headers={"X-Cairn-Actor": ADMIN})
    assert r.status_code == 200 and r.json()["failing"] == 1, r.text
    health = c.get(f"/quality/datasets/{ORDERS}/health", headers={"X-Cairn-Actor": ANA}).json()
    assert health["status"] == "FAIL" and health["external"][0]["url"] == "https://getmontecarlo.com/alerts/1"
