"""Context layer: query patterns found by question, approved and versioned definitions from
accepted findings, and Snowflake / Databricks semantic exports."""

import pytest

from cairn.ai.embeddings import HashingEmbedder
from cairn.context.layer import ContextLayer, export_databricks, export_snowflake
from cairn.context.miner import ContextStore, Finding

ORDERS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,sales.public.orders,PROD)"
CUST = "urn:li:dataset:(urn:li:dataPlatform:snowflake,sales.public.customers,PROD)"
SECRET = "urn:li:dataset:(urn:li:dataPlatform:snowflake,hr.public.salaries,PROD)"


@pytest.fixture
def layer(engine):
    emb = HashingEmbedder()
    return ContextLayer(engine, lambda text: emb.embed([text])[0])


def test_answers_find_how_a_question_was_answered_before(layer):
    layer.index_queries([
        {"urn": "urn:li:query:q1", "name": "Monthly revenue by region", "subjects": [ORDERS, CUST],
         "sql": "SELECT c.region, date_trunc('month', o.created_at) AS month, SUM(o.amount) AS revenue "
                "FROM orders o JOIN customers c ON o.customer_id = c.id GROUP BY 1, 2", "platform": "snowflake"},
        {"urn": "urn:li:query:q2", "subjects": [SECRET], "platform": "snowflake",
         "sql": "SELECT department, AVG(salary) AS avg_salary FROM salaries GROUP BY department"},
        {"urn": "urn:li:query:q3", "subjects": [CUST], "platform": "snowflake",
         "sql": "SELECT COUNT(*) AS customers FROM customers WHERE status = 'active'"},
    ])
    got = layer.answers("what was revenue per region last month?", lambda u: True)
    assert got[0]["urn"] == "urn:li:query:q1" and "SUM(o.amount)" in got[0]["sql"]
    assert got[0]["title"] == "Monthly revenue by region"
    hidden = layer.answers("average salary by department", lambda u: u != SECRET)
    assert "urn:li:query:q2" not in [a["urn"] for a in hidden]                 # its table is not visible
    assert layer.answers("average salary by department", lambda u: True)[0]["urn"] == "urn:li:query:q2"


def metric_finding(fid="fnd_1", canonical="sum(amount)"):
    return {"id": fid, "type": "METRIC_DEFINITION", "entities": [ORDERS], "data": {"name": "revenue", "canonical": canonical},
            "proposed_changes": [{"kind": "create_term", "value": {"name": "Revenue", "expression": canonical}}]}


def join_finding():
    return {"id": "fnd_j", "type": "JOIN_RELATIONSHIP", "entities": [ORDERS, CUST], "data": {"count": 4},
            "proposed_changes": [{"kind": "create_relationship", "value": {
                "source": ORDERS, "destination": CUST, "source_field": "customer_id", "destination_field": "id"}}]}


def test_definitions_are_versioned(layer):
    d1 = layer.record(metric_finding(), "urn:li:corpuser:ana", {"description": "Gross order value."})
    assert d1["version"] == 1 and d1["doc"]["expression"] == "sum(amount)" and d1["name"] == "revenue"
    same = layer.record(metric_finding(), "urn:li:corpuser:ana", {"description": "Gross order value."})
    assert same["version"] == 1                                                   # nothing changed
    d2 = layer.record(metric_finding("fnd_2"), "urn:li:corpuser:bob",
                      {"expression": "sum(amount) - sum(refunds)", "description": "Net of refunds."})
    assert d2["id"] == d1["id"] and d2["version"] == 2
    assert [v["version"] for v in layer.versions(d1["id"])] == [2, 1]
    j = layer.record(join_finding(), "urn:li:corpuser:ana")
    assert j["kind"] == "join" and j["name"] == "orders_to_customers"
    assert layer.record({"id": "x", "type": "DOC_STALENESS", "entities": []}, "a") is None
    layer.retire(j["id"], "a")
    assert [d["kind"] for d in layer.definitions()] == ["metric"]


def test_exports(layer):
    layer.record(metric_finding(), "a", {"description": "Gross 'order' value."})
    layer.record(join_finding(), "a")
    sf = export_snowflake(layer.definitions(), "sales_sv")
    assert "CREATE OR REPLACE SEMANTIC VIEW sales_sv" in sf
    assert "customers AS SALES.PUBLIC.CUSTOMERS PRIMARY KEY (id)" in sf and "orders AS SALES.PUBLIC.ORDERS" in sf
    assert "orders_to_customers AS orders (customer_id) REFERENCES customers" in sf
    assert "orders.revenue AS sum(amount) COMMENT = 'Gross ''order'' value.'" in sf
    db = export_databricks(layer.definitions(), "main.sales.orders_metrics", ORDERS)
    assert db.startswith("CREATE OR REPLACE VIEW main.sales.orders_metrics\nWITH METRICS\nLANGUAGE YAML\nAS $$\n")
    assert "source: sales.public.orders" in db and "'on': source.customer_id = customers.id" in db   # quoted: YAML 1.1 reads on as true
    assert "- name: revenue\n  expr: sum(amount)" in db
    only = export_snowflake(layer.definitions(), "v", datasets=[CUST])
    assert "METRICS" not in only and "RELATIONSHIPS" not in only


def test_accepting_a_finding_records_the_definition(engine, graph, layer):
    store = ContextStore(engine, graph, layer=layer)
    f = Finding(type="METRIC_DEFINITION", key=("revenue", "sum(amount)"), title="Metric revenue", summary_md="x",
                confidence=0.8, entities=[ORDERS],
                proposed_changes=[{"kind": "create_term", "entity_urn": None, "field_path": None,
                                   "value": {"name": "Revenue", "definition": "Computed as `sum(amount)`.",
                                             "expression": "sum(amount)"}}],
                data={"name": "revenue", "canonical": "sum(amount)"})
    store.upsert([f])
    fid = store.list()[0]["id"]
    out = store.accept(fid, "urn:li:corpuser:ana", {"name": "Net revenue", "expression": "sum(amount) - sum(refunds)"})
    assert out["definition"]["name"] == "Net revenue" and out["definition"]["doc"]["expression"] == "sum(amount) - sum(refunds)"
    term = graph.aspects[("urn:li:glossaryTerm:Mined.NetRevenue", "glossaryTermInfo")]
    assert term["customProperties"]["cairn_expression"] == "sum(amount) - sum(refunds)"
