"""Source management: gallery forms build recipes with secret references only; sources are created,
run, tested and watched through Cairn Core's ingestion API; pool sources test on their agents; health
changes tell owners once."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from cairn.agent.client import AgentState, GatewayClient
from cairn.agent.dispatcher import ExecutionRequestDispatcher
from cairn.agent.gateway import PoolSpec
from cairn.agent.worker import AgentConfig, AgentWorker
from cairn.app import create_api_app, create_gateway_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services
from cairn.sources.catalog import SourceError, build_recipe, form_values
from cairn.sources.service import health_of

ADMIN, ANA = "urn:li:corpuser:admin", "urn:li:corpuser:ana"
HOUR = 3_600_000


def test_forms_build_recipes_and_read_back():
    r = build_recipe("snowflake", {"account_id": "xy123", "warehouse": "WH", "username": "cairn",
                                   "password": "SNOWFLAKE_PW", "databases": "SALES, FINANCE"}, {"include_views": False})
    cfg = r["source"]["config"]
    assert r["source"]["type"] == "snowflake" and cfg["password"] == "${SNOWFLAKE_PW}"
    assert cfg["database_pattern"] == {"allow": ["SALES", "FINANCE"]} and cfg["include_table_lineage"] is True
    assert cfg["profiling"] == {"enabled": False} and cfg["include_views"] is False
    connector, values, advanced = form_values(r)
    assert connector == "snowflake" and values["password"] == "SNOWFLAKE_PW" and advanced == {"include_views": False}
    s3 = build_recipe("s3", {"paths": ["s3://b/a/*.parquet", "s3://b/c/*.csv"], "aws_region": "eu-west-1"})
    assert s3["source"]["config"]["path_specs"] == [{"include": "s3://b/a/*.parquet"}, {"include": "s3://b/c/*.csv"}]
    assert form_values(s3)[1]["paths"] == ["s3://b/a/*.parquet", "s3://b/c/*.csv"]
    assert build_recipe("dbt-cloud", {"account_id": "12", "project_id": 3, "job_id": "4", "token": "${DBT}",
                                      "target_platform": "snowflake"})["source"]["config"]["account_id"] == 12
    for connector, values, advanced, msg in (
            ("snowflake", {"account_id": "x", "warehouse": "w", "username": "u"}, None, "Password secret is required"),
            ("postgres", {"host_port": "h:1", "username": "u", "password": "hunter2!"}, None, "name of a secret"),
            ("postgres", {"host_port": "h:1", "username": "u", "password": "PG", "nope": 1}, None, "no field"),
            ("postgres", {"host_port": "h:1", "username": "u", "password": "PG"}, {"options": {"password": "x"}},
             "inline credentials"),
            ("dbt", {"manifest_path": "m", "catalog_path": "c", "target_platform": "oracle"}, None, "is one of"),
            ("nope", {}, None, "unknown connector")):
        with pytest.raises(SourceError, match=msg):
            build_recipe(connector, values, advanced)
    custom = build_recipe("custom", {}, recipe={"source": {"type": "hive", "config": {"password": "${HIVE}"}},
                                                "sink": {"type": "x"}})
    assert custom == {"source": {"type": "hive", "config": {"password": "${HIVE}"}}}    # the executor sets the sink
    assert form_values(custom)[0] == "custom"


def run(status, started, duration=60_000, events=0):
    return {"urn": f"urn:li:dataHubExecutionRequest:{started}", "status": status, "requested_ms": started,
            "started_ms": started, "duration_ms": duration, "events": events}


def test_health():
    now = int(time.time() * 1000)
    daily = {"schedule": {"interval": "0 2 * * *", "timezone": "UTC"}}
    assert health_of(daily, [])["state"] == "never_run"
    assert health_of(daily, [run("RUNNING", now)])["state"] == "running"
    assert health_of(daily, [run("FAILURE", now), run("FAILURE", now - HOUR), run("SUCCESS", now - 2 * HOUR)]) == \
        {"state": "failing", "reason": "the last 2 run(s) failed"}
    assert health_of(daily, [run("SUCCESS", now - 3 * HOUR)])["state"] == "healthy"
    assert health_of(daily, [run("SUCCESS", now - 50 * HOUR)])["state"] == "stale"
    assert health_of({"schedule": None}, [run("SUCCESS", now - 500 * HOUR)])["state"] == "healthy"   # manual only
    assert health_of({"schedule": {"interval": "0 * * * *"}}, [run("SUCCESS", now - 3 * HOUR)])["state"] == "stale"
    assert health_of(daily, [run("CANCELLED", now)])["state"] == "failing"     # nothing has succeeded


class FakeCore:
    """Just the ingestion part of Cairn Core's GraphQL API."""

    def __init__(self):
        self.sources, self.runs, self.n = {}, {}, 0

    def __call__(self, query, v):
        if "createIngestionSource" in query:
            self.n += 1
            urn = f"urn:li:dataHubIngestionSource:s{self.n}"
            self.sources[urn] = {"urn": urn, **v["i"], "platform": None}
            return {"createIngestionSource": urn}
        if "updateIngestionSource" in query:
            self.sources[v["u"]].update(v["i"])
            return {"updateIngestionSource": v["u"]}
        if "deleteIngestionSource" in query:
            self.sources.pop(v["u"], None)
            return {"deleteIngestionSource": v["u"]}
        if "createIngestionExecutionRequest" in query:
            self.n += 1
            urn = f"urn:li:dataHubExecutionRequest:r{self.n}"
            src = self.sources[v["i"]["ingestionSourceUrn"]]
            self.runs.setdefault(src["urn"], []).insert(0, {
                "urn": urn, "id": f"r{self.n}", "input": {"requestedAt": int(time.time() * 1000),
                                                       "executorId": src["config"]["executorId"]},
                "result": {"status": "RUNNING", "startTimeMs": int(time.time() * 1000), "durationMs": None,
                           "report": "", "structuredReport": None}})
            return {"createIngestionExecutionRequest": urn}
        if "createTestConnectionRequest" in query:
            self.tested = json.loads(v["i"]["recipe"])
            return {"createTestConnectionRequest": "urn:li:dataHubExecutionRequest:test1"}
        if "executionRequest(urn" in query:
            if v["u"].endswith("test1"):
                return {"executionRequest": {"result": {"status": "SUCCESS", "report": "", "structuredReport": {
                    "type": "TEST_CONNECTION", "serializedValue": json.dumps(
                        {"basic_connectivity": {"capable": False, "failure_reason": "password authentication failed"}})}}}}
            er = next(e for rs in self.runs.values() for e in rs if e["urn"] == v["u"])
            return {"executionRequest": er}
        if "ingestionSource(urn" in query:
            s = self.sources.get(v["u"])
            return {"ingestionSource": s and {**s, "executions": {"executionRequests": self.runs.get(s["urn"], [])}}}
        if "listIngestionSources" in query:
            return {"listIngestionSources": {"ingestionSources": [
                {**s, "executions": {"executionRequests": self.runs.get(s["urn"], [])[:v["n"]]}}
                for s in self.sources.values()]}}
        raise AssertionError(query)

    def finish(self, src, status, events=0):
        er = self.runs[src][0]
        er["result"].update(status=status, durationMs=1000, report="line 1\nline 2",
                            structuredReport={"type": "CLI_INGEST",
                                              "serializedValue": json.dumps({"source": {"report": {
                                                  "events_produced": events, "failures": {"x": ["bad"]} if status == "FAILURE" else {},
                                                  "warnings": {}}}})})


@pytest.fixture
def stack(settings, graph):
    core = FakeCore()
    graph.graphql_handler = core
    s = build_services(settings, graph=graph, with_ai=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE"], condition={"attr": "subject.urn", "op": "exists"})]))
    s.gateway.create_pool(PoolSpec(name="finance", allowed_platforms=["postgres"], max_concurrency=1), ADMIN)
    return s, core


PG = {"host_port": "finance-db:5432", "database": "finance", "username": "reader", "password": "FINANCE_PG_PASSWORD"}


def test_sources_api_create_run_history_and_owners_told(stack):
    s, core = stack
    c = TestClient(create_api_app(s))
    admin = {"X-Cairn-Actor": ADMIN}
    assert c.get("/sources/connectors", headers={"X-Cairn-Actor": ANA}).status_code == 403
    assert {"snowflake", "postgres", "dbt", "looker"} <= {x["id"] for x in c.get("/sources/connectors", headers=admin).json()}
    assert [e["id"] for e in c.get("/sources/executors", headers=admin).json()] == ["default", "finance"]
    body = {"name": "Finance DB", "connector": "postgres", "values": PG, "executor": "finance",
            "schedule": {"interval": "0 2 * * *", "timezone": "Europe/Berlin"}, "owners": [ANA]}
    assert c.post("/sources", json={**body, "executor": "nowhere"}, headers=admin).status_code == 422
    assert c.post("/sources", json={**body, "schedule": {"interval": "every day!"}}, headers=admin).status_code == 422
    made = c.post("/sources", json=body, headers=admin)
    assert made.status_code == 201, made.text
    src = made.json()
    assert src["connector"] == "postgres" and src["values"]["password"] == "FINANCE_PG_PASSWORD"
    assert src["secrets"] == ["FINANCE_PG_PASSWORD"] and src["executor"] == "finance"
    assert src["health"]["state"] == "never_run" and src["owners"] == [ANA]
    assert json.loads(core.sources[src["urn"]]["config"]["recipe"])["source"]["config"]["password"] == "${FINANCE_PG_PASSWORD}"

    ran = c.post(f"/sources/{src['urn']}/run", headers=admin).json()["run"]
    assert c.get("/sources", headers=admin).json()[0]["health"]["state"] == "running"
    core.finish(src["urn"], "FAILURE")
    assert s.sources.check_health()["failing"] == 1
    told = [n for n in s.notify.inbox(ANA) if n["type"] == "source.failing"]
    assert told and "Finance DB" in told[0]["title"]
    assert s.sources.check_health()["failing"] == 0                      # told once per change
    log = c.get(f"/sources/runs/{ran}", headers=admin).json()
    assert log["status"] == "FAILURE" and log["failures"] == 1 and "line 2" in log["log"]
    c.post(f"/sources/{src['urn']}/run", headers=admin)
    core.finish(src["urn"], "SUCCESS", events=42)
    assert s.sources.check_health()["recovered"] == 1
    assert [n for n in s.notify.inbox(ANA) if n["type"] == "source.recovered"]
    detail = c.get(f"/sources/{src['urn']}/detail", headers=admin).json()
    assert [r["status"] for r in detail["runs"]] == ["SUCCESS", "FAILURE"] and detail["runs"][0]["events"] == 42

    upd = c.put(f"/sources/{src['urn']}/detail", json={**body, "values": {**PG, "schemas": ["public"]}}, headers=admin)
    assert upd.status_code == 200 and upd.json()["values"]["schemas"] == ["public"]
    assert c.delete(f"/sources/{src['urn']}/detail", headers=admin).status_code == 204
    assert c.get("/sources", headers=admin).json() == []
    actions = {e["action"] for e in s.audit.entries(category="sources", limit=50)}
    assert {"source.created", "source.updated", "source.run", "source.deleted"} <= actions


def test_test_connection_on_cairn_and_on_an_agent_pool(stack, tmp_path):
    s, core = stack
    c = TestClient(create_api_app(s))
    admin = {"X-Cairn-Actor": ADMIN}
    t = c.post("/sources/test", json={"connector": "postgres", "values": PG}, headers=admin).json()
    assert core.tested["source"]["config"]["password"] == "${FINANCE_PG_PASSWORD}"
    got = c.get(f"/sources/test/{t['id']}", headers=admin).json()
    assert got["status"] == "SUCCESS" and got["connected"] is False
    assert got["report"]["basic_connectivity"]["failure_reason"] == "password authentication failed"

    pooled = c.post("/sources/test", json={"connector": "postgres", "values": PG, "executor": "finance"},
                    headers=admin).json()
    assert pooled["id"].startswith("task:") and c.get(f"/sources/test/{pooled['id']}", headers=admin).json()["status"] == "PENDING"
    http = TestClient(create_gateway_app(s))
    state = AgentState(str(tmp_path / "a1"))
    client = GatewayClient("http://testserver", state, http=http)
    client.enroll(s.gateway.create_enrollment_token("finance", ADMIN)["token"], hostname="h")
    tested, ingested = [], []

    def tester(recipe):
        tested.append(recipe)
        return {"test_connection": {"basic_connectivity": {"capable": True}, "capability_report": {}}}

    import os
    os.environ["FINANCE_PG_PASSWORD"] = "reader-pw"
    worker = AgentWorker(AgentConfig(gateway_url="http://testserver", state_dir=str(tmp_path / "a1")),
                         runner=lambda r: ingested.append(r) or {}, client=client, tester=tester)
    worker.tick()
    worker.pool.shutdown(wait=True)
    assert tested and not ingested and tested[0]["source"]["config"]["password"] == "reader-pw"
    done = c.get(f"/sources/test/{pooled['id']}", headers=admin).json()
    assert done["status"] == "SUCCESS" and done["connected"] is True, done


def test_core_test_connection_requests_for_a_pool_go_to_its_agents(stack):
    s, _ = stack
    s.graph.graphql_handler = None
    d = ExecutionRequestDispatcher(s.gateway, s.graph)
    urn = "urn:li:dataHubExecutionRequest:t1"
    s.graph.aspects[(urn, "dataHubExecutionRequestInput")] = {
        "task": "TEST_CONNECTION", "executorId": "finance", "requestedAt": 0, "source": {"type": "MANUAL"},
        "args": {"recipe": json.dumps({"source": {"type": "postgres", "config": {"password": "${PG}"}}})}}
    assert d.poll_once() == 1
    task = s.gateway.get_task(s.gateway.list_runs()[0]["task_id"])
    assert task["kind"] == "test" and task["status"] == "QUEUED"


def test_bi_connectors():
    sup = build_recipe("superset", {"connect_uri": "https://bi.acme.io", "username": "cairn", "password": "SUPERSET_PW"})
    assert sup["source"] == {"type": "superset", "config": {"connect_uri": "https://bi.acme.io", "username": "cairn",
                                                            "password": "${SUPERSET_PW}", "provider": "db"}}
    q = build_recipe("qlik", {"tenant_hostname": "acme.eu.qlikcloud.com", "api_key": "QLIK_KEY"})
    assert q["source"]["type"] == "qlik-sense" and q["source"]["config"]["api_key"] == "${QLIK_KEY}"
    assert build_recipe("sigma", {"client_id": "c", "client_secret": "SIGMA"})["source"]["config"]["api_url"]
    assert form_values(build_recipe("metabase", {"connect_uri": "https://m", "username": "u", "password": "MB"}))[0] == "metabase"
