"""OpenLineage in: run, job and dataset events become pipelines, tasks with inputs, outputs and column
lineage, and run history, without touching what crawlers catalogued."""

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services
from cairn.sources.openlineage import OpenLineageError, OpenLineageReceiver, dataset_urn, job_urns

ADMIN, ANA = "urn:li:corpuser:admin", "urn:li:corpuser:ana"
RAW = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.public.orders,PROD)"
OUT = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.core.orders,PROD)"
JOB = "urn:li:dataJob:(urn:li:dataFlow:(airflow,orders_daily,PROD),load_orders)"


def test_naming():
    assert dataset_urn("postgres://db.internal:5432", "shop.public.orders") == RAW
    assert dataset_urn("snowflake://acme-xy123", "ANALYTICS.CORE.ORDERS") == OUT
    assert dataset_urn("s3://lake", "/raw/orders/2026") == "urn:li:dataset:(urn:li:dataPlatform:s3,lake/raw/orders/2026,PROD)"
    assert dataset_urn("abfss://raw@acme.dfs.core.windows.net", "/sales") == \
        "urn:li:dataset:(urn:li:dataPlatform:abs,raw/sales,PROD)"
    assert dataset_urn("file", "/tmp/x.csv") == "urn:li:dataset:(urn:li:dataPlatform:file,/tmp/x.csv,PROD)"
    assert dataset_urn("kafka://broker:9092", "orders") == "urn:li:dataset:(urn:li:dataPlatform:kafka,orders,PROD)"
    assert job_urns("airflow://prod", "orders_daily.load_orders") == \
        ("airflow", "urn:li:dataFlow:(airflow,orders_daily,PROD)", JOB)
    assert job_urns("spark_cluster_1", "etl.write_orders")[1] == "urn:li:dataFlow:(spark,spark_cluster_1,PROD)"


def event(kind, run="r-1", **extra):
    return {"eventType": kind, "eventTime": "2026-10-30T02:00:00Z", "producer": "https://github.com/apache/airflow",
            "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
            "run": {"runId": run}, "job": {"namespace": "airflow://prod", "name": "orders_daily.load_orders",
                                           "facets": {"sql": {"query": "INSERT INTO core.orders SELECT * FROM orders"}}},
            **extra}


IN = [{"namespace": "postgres://db.internal:5432", "name": "shop.public.orders"}]
OUTPUTS = [{"namespace": "snowflake://acme-xy123", "name": "ANALYTICS.CORE.ORDERS", "facets": {
    "schema": {"fields": [{"name": "id", "type": "NUMBER"}, {"name": "total", "type": "FLOAT"}]},
    "columnLineage": {"fields": {"total": {"inputFields": [
        {"namespace": "postgres://db.internal:5432", "name": "shop.public.orders", "field": "amount"},
        {"namespace": "postgres://db.internal:5432", "name": "shop.public.orders", "field": "tax"}],
        "transformationType": "AGGREGATION"}}}}}]


def test_run_events_build_jobs_lineage_and_history(graph):
    graph.aspects[(RAW, "schemaMetadata")] = {"fields": [{"fieldPath": "amount"}], "schemaName": "crawled"}
    r = OpenLineageReceiver(graph)
    started = r.handle(event("START", inputs=IN))
    assert started["job"] == JOB and started["inputs"] == 1
    done = r.handle(event("COMPLETE", outputs=OUTPUTS))
    io = graph.aspects[(JOB, "dataJobInputOutput")]
    assert io["inputDatasets"] == [RAW] and io["outputDatasets"] == [OUT]          # accumulated over events
    [fgl] = io["fineGrainedLineages"]
    assert fgl["downstreams"] == [f"urn:li:schemaField:({OUT},total)"] and fgl["transformOperation"] == "AGGREGATION"
    assert fgl["upstreams"] == [f"urn:li:schemaField:({RAW},amount)", f"urn:li:schemaField:({RAW},tax)"]
    assert graph.aspects[(RAW, "schemaMetadata")]["schemaName"] == "crawled"         # crawled metadata untouched
    assert [f["fieldPath"] for f in graph.aspects[(OUT, "schemaMetadata")]["fields"]] == ["id", "total"]
    info = graph.aspects[(JOB, "dataJobInfo")]
    assert info["name"] == "load_orders" and "INSERT INTO" in info["customProperties"]["sql"]
    assert graph.aspects[("urn:li:dataFlow:(airflow,orders_daily,PROD)", "dataFlowInfo")]["name"] == "orders_daily"
    run = done["run"]
    assert graph.aspects[(run, "dataProcessInstanceRelationships")]["parentTemplate"] == JOB
    assert [e["status"] for e in graph.timeseries[(run, "dataProcessInstanceRunEvent")]] == ["COMPLETE", "STARTED"]
    assert graph.timeseries[(run, "dataProcessInstanceRunEvent")][0]["result"]["type"] == "SUCCESS"
    assert graph.aspects[(run, "dataProcessInstanceOutput")]["outputs"] == [OUT]
    failed = r.handle(event("FAIL", run="r-2"))
    assert graph.timeseries[(failed["run"], "dataProcessInstanceRunEvent")][0]["result"]["type"] == "FAILURE"
    assert graph.aspects[(failed["run"], "dataProcessInstanceRelationships")]["parentTemplate"] == JOB
    assert failed["run"] != run
    again = r.handle(event("COMPLETE", outputs=OUTPUTS))
    assert again["mcps"] < done["mcps"]                                               # nothing new to say


def test_dataset_events_and_bad_input(graph):
    r = OpenLineageReceiver(graph)
    r.handle({"eventTime": "2026-10-30T02:00:00Z", "dataset": {"namespace": "kafka://b:9092", "name": "payments",
                                                              "facets": {"schema": {"fields": [{"name": "id"}]}}}})
    assert graph.aspects[("urn:li:dataset:(urn:li:dataPlatform:kafka,payments,PROD)", "schemaMetadata")]
    for bad, msg in (({"eventType": "START"}, "not an OpenLineage"), ({"job": {"name": "x"}}, "namespace and a name"),
                     (event("START", eventTime="yesterday"), "ISO 8601"),
                     (event("START", inputs=[{"name": "t"}]), "namespace and a name")):
        with pytest.raises(OpenLineageError, match=msg):
            r.handle(bad)
    with pytest.raises(OpenLineageError, match="between 1 and 500"):
        r.handle_many([])


def test_endpoint(settings, graph):
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    c = TestClient(create_api_app(s))
    body = event("COMPLETE", inputs=IN, outputs=OUTPUTS)
    assert c.post("/openlineage/api/v1/lineage", json=body, headers={"X-Cairn-Actor": ANA}).status_code == 403
    r = c.post("/openlineage/api/v1/lineage", json=[body, event("START", run="r-9")], headers={"X-Cairn-Actor": ADMIN})
    assert r.status_code == 200 and r.json()["accepted"] == 2, r.text
    assert graph.aspects[(JOB, "dataJobInputOutput")]["outputDatasets"] == [OUT]
    bad = c.post("/openlineage/api/v1/lineage", json={"eventType": "START"}, headers={"X-Cairn-Actor": ADMIN})
    assert bad.status_code == 422
