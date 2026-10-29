"""Column-level lineage from SQL: writing queries and view definitions are traced column by column to
the physical columns they read, and merged into the target's upstream lineage without losing edges."""

from cairn.context.column_lineage import extract, publish, to_mcps
from cairn.context.miner import fetch_snapshot

P = "urn:li:dataPlatform:postgres"
ORDERS = f"urn:li:dataset:({P},shop.raw.orders,PROD)"
REFUNDS = f"urn:li:dataset:({P},shop.raw.refunds,PROD)"
CUSTOMERS = f"urn:li:dataset:({P},shop.raw.customers,PROD)"
ENRICHED = f"urn:li:dataset:({P},shop.mart.orders_enriched,PROD)"
VIEW = f"urn:li:dataset:({P},shop.mart.big_orders,PROD)"
F = "urn:li:schemaField:"


def schema(*cols):
    return {"fields": [{"fieldPath": c, "nativeDataType": "int"} for c in cols]}


def seed(graph):
    graph.aspects[(ORDERS, "schemaMetadata")] = schema("id", "amount", "customer_id")
    graph.aspects[(REFUNDS, "schemaMetadata")] = schema("order_id", "amount")
    graph.aspects[(CUSTOMERS, "schemaMetadata")] = schema("id", "region")
    graph.aspects[(ENRICHED, "schemaMetadata")] = schema("order_id", "revenue", "region")
    graph.aspects[(VIEW, "schemaMetadata")] = schema("id", "amount2")
    graph.aspects[(VIEW, "viewProperties")] = {"materialized": False, "viewLanguage": "SQL", "viewLogic": (
        "WITH x AS (SELECT * FROM raw.orders) SELECT id, amount * 2 FROM x WHERE amount > 100")}
    graph.aspects[("urn:li:query:q1", "queryProperties")] = {"statement": {"value": """
        INSERT INTO mart.orders_enriched (order_id, revenue, region)
        SELECT o.id, o.amount - COALESCE(r.amount, 0), c.region
        FROM raw.orders o LEFT JOIN raw.refunds r ON r.order_id = o.id JOIN raw.customers c ON c.id = o.customer_id"""}}
    graph.aspects[("urn:li:query:q1", "querySubjects")] = {"subjects": [{"entity": ENRICHED}, {"entity": ORDERS}]}
    graph.aspects[("urn:li:query:q2", "queryProperties")] = {"statement": {"value": "SELECT id FROM raw.orders"}}


def test_traces_inserts_and_views(graph):
    seed(graph)
    edges = {(e.target, e.column): e for e in extract(fetch_snapshot(graph))}
    assert edges[(ENRICHED, "order_id")].sources == [(ORDERS, "id")] and edges[(ENRICHED, "order_id")].operation == "COPY"
    rev = edges[(ENRICHED, "revenue")]
    assert rev.sources == sorted([(ORDERS, "amount"), (REFUNDS, "amount")]) and rev.operation == "SQL"
    assert rev.query == "urn:li:query:q1"
    assert edges[(ENRICHED, "region")].sources == [(CUSTOMERS, "region")]
    assert edges[(VIEW, "id")].sources == [(ORDERS, "id")]                # through the CTE and the star
    assert edges[(VIEW, "amount2")].sources == [(ORDERS, "amount")]       # named by the view's schema
    assert len(edges) == 5                                                # a plain SELECT writes nothing


def test_merges_without_losing_edges(graph):
    seed(graph)
    kept = {"upstreamType": "FIELD_SET", "upstreams": [f"{F}({ORDERS},customer_id)"], "downstreamType": "FIELD",
            "downstreams": [f"{F}({ENRICHED},region)"], "transformOperation": "MANUAL"}
    graph.aspects[(ENRICHED, "upstreamLineage")] = {
        "upstreams": [{"dataset": ORDERS, "type": "TRANSFORMED", "auditStamp": {"time": 1, "actor": "urn:li:corpuser:x"}}],
        "fineGrainedLineages": [kept]}
    assert publish(fetch_snapshot(graph), graph) == {"column_edges": 5, "datasets_updated": 2}
    lin = graph.aspects[(ENRICHED, "upstreamLineage")]
    assert {u["dataset"] for u in lin["upstreams"]} == {ORDERS, REFUNDS, CUSTOMERS}
    assert lin["upstreams"][0]["auditStamp"]["time"] == 1                 # what was there stays as it was
    assert lin["fineGrainedLineages"][0] == kept and len(lin["fineGrainedLineages"]) == 4
    rev = next(f for f in lin["fineGrainedLineages"] if f["downstreams"] == [f"{F}({ENRICHED},revenue)"])
    assert rev["upstreams"] == sorted([f"{F}({ORDERS},amount)", f"{F}({REFUNDS},amount)"]) and rev["query"] == "urn:li:query:q1"
    before = len(graph.emitted)
    assert publish(fetch_snapshot(graph), graph)["datasets_updated"] == 0     # nothing new, nothing sent
    assert len(graph.emitted) == before


def test_bad_sql_is_skipped(graph):
    seed(graph)
    graph.aspects[("urn:li:query:q3", "queryProperties")] = {"statement": {"value": "INSERT INTO ??? garbage ((("}}
    graph.aspects[("urn:li:query:q4", "queryProperties")] = {"statement": {"value": "INSERT INTO nowhere SELECT 1"}}
    assert len(extract(fetch_snapshot(graph))) == 5
    assert to_mcps([], graph) == []


def test_miner_runs_it(graph, engine):
    from cairn.context.miner import ContextMiner, ContextStore

    seed(graph)
    status = ContextMiner(graph, ContextStore(engine, graph)).run_once()
    assert status["column_edges"] == 5 and status["datasets_updated"] == 2
