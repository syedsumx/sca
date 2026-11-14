"""Semantic layer for agents: metrics imported from MetricFlow, Cube and LookML become findings;
approved ones are queried by name with SQL compiled from the definition, never from free text."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from cairn.ai.embeddings import HashingEmbedder
from cairn.context.layer import ContextLayer
from cairn.context.miner import ContextStore
from cairn.context.semantic import (SemanticError, SemanticService, compile_query, parse_cube, parse_lookml,
                                    parse_metricflow)
from cairn.coreproxy.masking import DatasetPlan
from cairn.dataaccess.service import DataAccessService
from cairn.quality.collectors import ConnectionRegistry

ORDERS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"
ANA, EVE = "urn:li:corpuser:ana", "urn:li:corpuser:eve"

METRICFLOW = """
semantic_models:
  - name: orders
    model: ref('orders')
    defaults: {agg_time_dimension: ordered_at}
    measures:
      - {name: order_total, agg: sum, expr: amount}
      - {name: order_count, agg: count, expr: id}
      - {name: refunds, agg: sum, expr: refund}
    dimensions:
      - {name: region, type: categorical}
      - {name: ordered_at, type: time}
metrics:
  - {name: revenue, type: simple, description: Booked revenue, type_params: {measure: order_total}}
  - {name: orders, type: simple, type_params: {measure: {name: order_count}}}
  - {name: aov, type: ratio, type_params: {numerator: order_total, denominator: order_count}}
  - {name: net_revenue, type: derived, type_params: {expr: revenue - refunds_m, metrics: [revenue, refunds_m]}}
  - {name: refunds_m, type: simple, type_params: {measure: refunds}}
  - {name: unknown, type: simple, type_params: {measure: nope}}
"""

CUBE = """
cubes:
  - name: orders
    sql_table: main.orders
    measures:
      - {name: count, type: count}
      - {name: total, sql: "{CUBE}.amount", type: sum, description: Gross}
      - {name: buyers, sql: user_id, type: count_distinct}
    dimensions:
      - {name: region, sql: "{CUBE}.region", type: string}
      - {name: ordered_at, sql: ordered_at, type: time}
"""

LOOKML = """
view: orders {
  sql_table_name: main.orders ;;
  dimension: region {
    type: string
    sql: ${TABLE}.region ;;
  }
  dimension_group: ordered {
    type: time
    sql: ${TABLE}.ordered_at ;;
  }
  measure: count {
    type: count
  }
  measure: total {
    type: sum
    sql: ${TABLE}.amount ;;
    description: "Gross amount"
  }
  measure: ratio {
    type: number
    sql: ${total} / ${count} ;;
  }
}
"""


def test_metricflow_simple_ratio_and_derived():
    got = {m["name"]: m for m in parse_metricflow(METRICFLOW)}
    assert set(got) == {"revenue", "orders", "aov", "refunds_m"}               # derived comes before its part
    assert got["revenue"]["expression"] == "SUM(amount)" and got["revenue"]["table"] == "orders"
    assert got["revenue"]["dimensions"] == ["ordered_at", "region"] and got["revenue"]["time_dimension"] == "ordered_at"
    assert got["aov"]["expression"] == "SUM(amount) / NULLIF(COUNT(id), 0)"
    reordered = METRICFLOW.replace("  - {name: refunds_m, type: simple, type_params: {measure: refunds}}\n", "") \
        .replace("  - {name: net_revenue", "  - {name: refunds_m, type: simple, type_params: {measure: refunds}}\n"
                                          "  - {name: net_revenue")
    net = {m["name"]: m for m in parse_metricflow(reordered)}["net_revenue"]
    assert net["expression"] == "(SUM(amount)) - (SUM(refund))"


def test_cube_and_lookml():
    cube = {m["name"]: m for m in parse_cube(CUBE)}
    assert cube["orders.count"]["expression"] == "COUNT(*)" and cube["orders.total"]["expression"] == "SUM(amount)"
    assert cube["orders.buyers"]["expression"] == "COUNT(DISTINCT user_id)"
    assert cube["orders.total"]["dimensions"] == ["ordered_at", "region"] and cube["orders.total"]["time_dimension"] == "ordered_at"
    look = {m["name"]: m for m in parse_lookml(LOOKML)}
    assert set(look) == {"orders.count", "orders.total"}                        # field references are skipped
    assert look["orders.total"]["expression"] == "SUM(amount)" and look["orders.total"]["description"] == "Gross amount"
    assert look["orders.total"]["table"] == "main.orders" and look["orders.total"]["time_dimension"] == "ordered_at"


def definition(**doc):
    return {"name": "revenue", "version": 1, "doc": {"expression": "SUM(amount)", "dimensions": ["region"],
                                                       "time_dimension": "ordered_at", **doc}}


def test_compile_validates_everything_and_never_takes_sql():
    sql = compile_query(definition(), "main.orders", "postgres", ["region"],
                        [{"dimension": "region", "op": "in", "value": ["EU", "US"]}], "month", 50)
    assert sql == ("SELECT DATE_TRUNC('MONTH', ordered_at) AS period, region, SUM(amount) AS revenue FROM main.orders "
                   "WHERE region IN ('EU', 'US') GROUP BY 1, 2 ORDER BY 1, 2 LIMIT 50")
    injected = compile_query(definition(), "main.orders", "postgres", [],
                             [{"dimension": "region", "op": "=", "value": "x' OR 1=1 --"}], None, 10)
    assert "'x'' OR 1=1 --'" in injected                                       # a value stays a literal
    for dims, filters, grain, msg in (
            (["email"], [], None, "no dimension"),
            ([], [{"dimension": "email", "op": "=", "value": 1}], None, "only use the metric's dimensions"),
            ([], [{"dimension": "region", "op": "like", "value": "%"}], None, "op is one of"),
            ([], [{"dimension": "region", "op": "in", "value": []}], None, "non-empty list"),
            ([], [], "hour", "time_grain is one of")):
        with pytest.raises(SemanticError, match=msg):
            compile_query(definition(), "main.orders", "postgres", dims, filters, grain, 10)
    with pytest.raises(SemanticError, match="no time dimension"):
        compile_query(definition(time_dimension=None), "t", "postgres", [], [], "day", 10)
    with pytest.raises(SemanticError, match="subquery"):
        compile_query(definition(expression="(SELECT max(salary) FROM hr.salaries)"), "t", "postgres", [], [], None, 10)
    assert compile_query(definition(), "t", "postgres", [], [], None, 10_000).endswith("LIMIT 1000")


class Audit:
    def __init__(self):
        self.events = []

    def record(self, category, action, actor, subject, details):
        self.events.append((action, actor, subject, details))


@pytest.fixture
def stack(tmp_path, engine):
    url = f"sqlite:///{tmp_path}/wh.db"
    with create_engine(url).begin() as c:
        c.execute(text("CREATE TABLE orders (id INTEGER, region TEXT, amount REAL, refund REAL, ordered_at TEXT)"))
        c.execute(text("INSERT INTO orders VALUES (1, 'EU', 9.5, 0, '2026-01-02'), (2, 'EU', 3.0, 1, '2026-02-01'), "
                       "(3, 'US', 7.0, 0, '2026-02-03')"))
    audit = Audit()
    data = DataAccessService(ConnectionRegistry({"wh": {"url": url, "catalog": {"platform": "sqlite"}}}),
                             lambda a, u: DatasetPlan(), lambda a, u: a != EVE, audit, known=lambda u: u == ORDERS)
    emb = HashingEmbedder()
    store = ContextStore(engine, None, layer=ContextLayer(engine, lambda t: emb.embed([t])[0]))
    return SemanticService(store, data, lambda: [ORDERS], audit), store, audit


def test_import_approve_list_and_query(stack):
    sem, store, audit = stack
    out = sem.import_definitions("metricflow", METRICFLOW.replace("ref('orders')", "ref('main.orders')"),
                                 "models/semantic.yml", ANA)
    assert out["metrics"] == 4 and out["mapped"] == 4 and out["new"] == 4
    assert ("semantic.imported", ANA, "models/semantic.yml") == audit.events[-1][:3]
    assert sem.metrics(lambda u: True) == []                                   # nothing is governed until approved
    revenue = next(f for f in store.list("METRIC_DEFINITION") if f["title"].startswith("Metric “revenue”"))
    d = store.accept(revenue["id"], ANA)["definition"]
    assert d["doc"]["dimensions"] == ["ordered_at", "region"] and d["doc"]["source"] == "dbt-semantic-layer"
    [listed] = sem.metrics(lambda u: True)
    assert listed["name"] == "revenue" and listed["description"] == "Booked revenue" and listed["dataset"] == ORDERS
    assert sem.metrics(lambda u: u != ORDERS) == [] and sem.metrics(lambda u: True, "booked")

    got = sem.query(ANA, "revenue", lambda u: True, ["region"], [{"dimension": "region", "op": "=", "value": "EU"}])
    assert got["sql"].startswith("SELECT region, SUM(amount) AS revenue FROM main.orders WHERE region = 'EU'")
    assert got["result"]["rows"] == [["EU", 12.5]] and got["version"] == 1
    with pytest.raises(SemanticError) as e:
        sem.query(ANA, "nope", lambda u: True)
    assert e.value.status == 404
    with pytest.raises(SemanticError) as e:
        sem.query(ANA, "revenue", lambda u: u != ORDERS)
    assert e.value.status == 403
    with pytest.raises(SemanticError) as e:                                    # data access still decides
        sem.query(EVE, "revenue", lambda u: True)
    assert e.value.status == 403
    sem.data.registry = None                                                  # no warehouse serves it now
    with pytest.raises(SemanticError) as e:                                    # a bad ask is still told first
        sem.query(ANA, "revenue", lambda u: True, ["email"])
    assert e.value.status == 422
    with pytest.raises(SemanticError) as e:
        sem.query(ANA, "revenue", lambda u: True, ["region"])
    assert e.value.status == 404
    with pytest.raises(SemanticError, match="format is one of"):
        sem.import_definitions("dax", "x", "r", ANA)
    with pytest.raises(SemanticError, match="no metrics"):
        sem.import_definitions("cube", "cubes: []", "r", ANA)


def test_api_and_mcp_tool(settings, graph, stack):
    from cairn.ai.mcp_server import CairnTools, _principal
    from cairn.common.security import Principal
    from cairn.app import create_api_app
    from cairn.services import build_services

    sem, store, _ = stack
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.semantic_layer = sem
    admin = "urn:li:corpuser:admin"
    from cairn.governance.abac import Policy, PolicyBundle

    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": admin}),
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE"], condition={"attr": "subject.urn", "op": "exists"})]))
    c = TestClient(create_api_app(s))
    body = {"format": "cube", "content": CUBE, "ref": "cube/orders.yml"}
    assert c.post("/context/semantic/import", json=body, headers={"X-Cairn-Actor": ANA}).status_code == 403
    r = c.post("/context/semantic/import", json=body, headers={"X-Cairn-Actor": admin})
    assert r.status_code == 200 and r.json()["metrics"] == 3, r.text
    total = next(f for f in store.list("METRIC_DEFINITION") if "orders.total" in f["title"])
    store.accept(total["id"], admin)
    assert [m["name"] for m in c.get("/context/metrics", headers={"X-Cairn-Actor": ANA}).json()] == ["orders.total"]
    q = c.post("/context/metrics/orders.total/query", json={"dimensions": ["region"]}, headers={"X-Cairn-Actor": ANA})
    assert q.status_code == 200 and q.json()["result"]["rows"] == [["EU", 12.5], ["US", 7.0]], q.text
    bad = c.post("/context/metrics/orders.total/query", json={"dimensions": ["amount"]}, headers={"X-Cairn-Actor": ANA})
    assert bad.status_code == 422

    token = _principal.set(Principal(ANA, "agent", agent="agt_bi", scopes=("read", "query_data")))
    try:
        tools = CairnTools(s)
        assert [m["name"] for m in tools.list_metrics()["metrics"]] == ["orders.total"]
        assert tools.query_metric("orders.total", ["region"])["result"]["rows"][0] == ["EU", 12.5]
        with pytest.raises(ValueError):
            tools.query_metric("orders.total", ["amount"])
    finally:
        _principal.reset(token)
