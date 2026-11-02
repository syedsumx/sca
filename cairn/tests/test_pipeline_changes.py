"""Dataset changes and failed pipelines (cairn.pipelines.changes), fed by the real OpenLineage receiver."""

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from cairn.common.core import InMemoryGraph
from cairn.common.db import init_db
from cairn.common.security import Principal
from cairn.pipelines.changes import DatasetChanges, PipelineHealth, _incident_urn, build_changes_router
from cairn.sources.openlineage import OpenLineageReceiver

OUT = {"namespace": "trino://", "name": "lakehouse.analytics.orders"}
IN = {"namespace": "trino://", "name": "lakehouse.raw.orders"}


class Hub:
    def __init__(self):
        self.events = []

    def publish(self, e):
        self.events.append(e)
        return []


def event(state, run="r1", outputs=(OUT,), error=None):
    facets = {"errorMessage": {"message": error, "programmingLanguage": "python"}} if error else {}
    return {"eventType": state, "eventTime": "2026-11-02T10:00:00Z", "run": {"runId": run, "facets": facets},
            "job": {"namespace": "airflow", "name": "shop_daily.build_orders"},
            "inputs": [IN], "outputs": list(outputs)}


def world(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/c.db")
    init_db(engine)
    graph, hub = InMemoryGraph(), Hub()
    changes = DatasetChanges(engine)
    ol = OpenLineageReceiver(graph)
    health = PipelineHealth(graph, hub, changes)
    ol.on_run = health.on_run
    return ol, graph, hub, changes


def test_completed_runs_are_changes_and_failures_are_incidents(tmp_path):
    ol, graph, hub, changes = world(tmp_path)
    start = changes.head()
    ol.handle_many([event("START"), event("COMPLETE")])
    rows = changes.since(["urn:li:dataset:(urn:li:dataPlatform:trino,lakehouse.analytics.orders,PROD)"], start)
    assert len(rows) == 1 and rows[0]["source"] == "openlineage" and "shop_daily" in rows[0]["job"]
    out_urn = rows[0]["urn"]
    assert not changes.since(["urn:li:dataset:(urn:li:dataPlatform:trino,lakehouse.raw.orders,PROD)"], start)  # inputs are not changes

    # A failure: an incident on what the job writes, owners told once, however often it fails.
    ol.handle(event("FAIL", run="r2", outputs=(), error="Trino: Access Denied"))
    job = rows[0]["job"]
    inc = graph.get_aspect(_incident_urn(job, out_urn), "incidentInfo")
    assert inc["status"]["state"] == "ACTIVE" and inc["entities"] == [out_urn, job]
    assert "Access Denied" in inc["description"] and inc["title"].startswith("[Pipeline]")
    told = [(e.type, e.entity_urn) for e in hub.events]
    assert ("pipeline.run_failed", job) in told and ("pipeline.run_failed", out_urn) in told
    ol.handle(event("FAIL", run="r3", outputs=()))
    assert len(hub.events) == len(told)  # not again
    assert len(changes.since([out_urn], start)) == 1  # a failed run changed nothing
    # The next successful run resolves it.
    ol.handle(event("COMPLETE", run="r4"))
    assert graph.get_aspect(_incident_urn(job, out_urn), "incidentInfo")["status"]["state"] == "RESOLVED"
    assert len(changes.since([out_urn], start)) == 2


def test_dbt_runs_record_what_they_built(tmp_path):
    from cairn.pipelines.dbt import DbtRuns

    engine = create_engine(f"sqlite:///{tmp_path}/c.db")
    init_db(engine)
    changes = DatasetChanges(engine)

    class External:
        def ingest(self, *a, **k):
            return {"checks": 0}
    runs = DbtRuns(InMemoryGraph(), External(), None)
    runs.changes = changes
    node = {"name": "orders", "database": "lakehouse", "schema": "analytics", "resource_type": "model", "columns": {}}
    manifest = {"metadata": {"adapter_type": "trino", "project_name": "shop", "invocation_id": "inv-1"},
                "nodes": {"model.shop.orders": node, "model.shop.broken": {**node, "name": "broken"}}}
    runs.ingest({"manifest": manifest, "run_results": {"results": [
        {"unique_id": "model.shop.orders", "status": "success"}, {"unique_id": "model.shop.broken", "status": "error"}]}},
        "urn:li:corpuser:svc-dbt")
    rows = changes.since(["urn:li:dataset:(urn:li:dataPlatform:trino,lakehouse.analytics.orders,PROD)",
                          "urn:li:dataset:(urn:li:dataPlatform:trino,lakehouse.analytics.broken,PROD)"], 0)
    assert [(r["source"], r["job"], r["run"]) for r in rows] == [("dbt", "dbt:shop", "inv-1")]  # only what succeeded


def test_the_feed_shows_only_datasets_the_caller_may_see(tmp_path):
    ol, _, _, changes = world(tmp_path)
    ol.handle(event("COMPLETE"))
    urn = "urn:li:dataset:(urn:li:dataPlatform:trino,lakehouse.analytics.orders,PROD)"
    secret = "urn:li:dataset:(urn:li:dataPlatform:trino,lakehouse.hr.salaries,PROD)"

    def authorize(p, action, resource):
        if resource == secret:
            raise HTTPException(403, "no")
    app = FastAPI()
    app.include_router(build_changes_router(changes, lambda: Principal("urn:li:corpuser:ana", "token"), authorize))
    c = TestClient(app)
    head = c.get("/v1/datasets/changes", params={"urn": urn}).json()
    assert head["changes"] == [] and head["cursor"] >= 1  # no cursor yet: where to watch from
    got = c.get("/v1/datasets/changes", params={"urn": urn, "after": 0}).json()
    assert [x["urn"] for x in got["changes"]] == [urn] and got["cursor"] == got["changes"][-1]["seq"]
    assert c.get("/v1/datasets/changes", params={"urn": urn, "after": got["cursor"]}).json()["changes"] == []
    assert c.get("/v1/datasets/changes", params={"urn": [urn, secret], "after": 0}).status_code == 403
    assert c.get("/v1/datasets/changes", params={"urn": [f"u{i}" for i in range(101)]}).status_code == 422
