"""End-to-end smoke tests against a running ``docker-compose.cairn.yml`` stack.

Skipped unless ``CAIRN_E2E=1``. Expects the default host ports and a Cairn Core PAT for an
admin (root) user in ``CAIRN_E2E_TOKEN`` (the compose bootstrap mints one into the
``cairn-bootstrap`` volume: ``docker compose exec cairn-api cat /run/cairn/core_token``).

    CAIRN_E2E=1 CAIRN_E2E_TOKEN=$(docker compose -f docker-compose.cairn.yml exec -T cairn-api \
        cat /run/cairn/core_token) pytest tests/e2e -v
"""

from __future__ import annotations

import json
import re
import subprocess
import os
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_E2E") != "1", reason="set CAIRN_E2E=1 to run against a live stack")

CORE = os.environ.get("CAIRN_E2E_CORE", "http://localhost:8080")
API = os.environ.get("CAIRN_E2E_API", "http://localhost:8800")
MCP = os.environ.get("CAIRN_E2E_MCP", "http://localhost:8830/mcp")
EDGE = os.environ.get("CAIRN_E2E_EDGE", "http://localhost:9000")
TOKEN = os.environ.get("CAIRN_E2E_TOKEN", "")
RUN = uuid.uuid4().hex[:8]
DS = f"urn:li:dataset:(urn:li:dataPlatform:postgres,e2e_{RUN}.public.orders,PROD)"
DS_DOWN = f"urn:li:dataset:(urn:li:dataPlatform:snowflake,e2e_{RUN}.analytics.orders_enriched,PROD)"


def _h() -> dict:
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
            "X-RestLi-Protocol-Version": "2.0.0"}


def emit(urn: str, aspect: str, value: dict) -> None:
    proposal = {"entityType": urn.split(":")[2], "entityUrn": urn, "changeType": "UPSERT", "aspectName": aspect,
                "aspect": {"contentType": "application/json", "value": json.dumps(value)}}
    r = httpx.post(f"{CORE}/aspects?action=ingestProposal", headers=_h(),
                   json={"proposal": proposal, "async": "false"}, timeout=30)
    assert r.status_code == 200, r.text


def gql(query: str, variables: dict | None = None) -> dict:
    r = httpx.post(f"{CORE}/api/graphql", headers=_h(), json={"query": query, "variables": variables or {}}, timeout=30)
    body = r.json()
    assert not body.get("errors"), body
    return body["data"]


def eventually(fn, timeout: float = 60, interval: float = 2):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
            if last:
                return last
        except AssertionError as e:
            last = e
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s: {last}")


@pytest.fixture(scope="module")
def api() -> httpx.Client:
    with httpx.Client(base_url=API, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60) as c:
        yield c


@pytest.fixture(scope="module", autouse=True)
def seed():
    fields = [{"fieldPath": p, "nativeDataType": t, "type": {"type": {"com.linkedin.schema.StringType": {}}}}
              for p, t in (("order_id", "INT"), ("customer_email", "VARCHAR"), ("amount", "NUMERIC"))]
    schema = {"schemaName": "orders", "platform": "urn:li:dataPlatform:postgres", "version": 0, "hash": "",
              "platformSchema": {"com.linkedin.schema.MySqlDDL": {"tableSchema": ""}}, "fields": fields}
    emit(DS, "schemaMetadata", schema)
    emit(DS, "datasetProperties", {"name": "orders", "description": "E2E orders table: one row per order."})
    emit(DS, "ownership", {"owners": [{"owner": "urn:li:corpuser:admin", "type": "TECHNICAL_OWNER"}],
                           "lastModified": {"time": 0, "actor": "urn:li:corpuser:admin"}})
    emit(DS, "datasetProfile", {"timestampMillis": int(time.time() * 1000), "rowCount": 1200, "columnCount": 3})
    emit(DS_DOWN, "schemaMetadata", {**schema, "schemaName": "orders_enriched",
                                     "fields": [{**fields[1], "fieldPath": "email"}]})
    emit(DS_DOWN, "upstreamLineage", {"upstreams": [{"dataset": DS, "type": "TRANSFORMED",
                                                     "auditStamp": {"time": 0, "actor": "urn:li:corpuser:admin"}}],
                                      "fineGrainedLineages": [{
                                          "upstreamType": "FIELD_SET",
                                          "upstreams": [f"urn:li:schemaField:({DS},customer_email)"],
                                          "downstreamType": "FIELD",
                                          "downstreams": [f"urn:li:schemaField:({DS_DOWN},email)"],
                                          "confidenceScore": 1.0}]})


def test_health_endpoints():
    for url in (f"{API}/health", f"{EDGE}/cairn/api/health", f"{CORE}/health"):
        assert httpx.get(url, timeout=10).status_code == 200, url


def test_identity_via_catalog_pat(api):
    assert api.get("/me").json() == {"urn": "urn:li:corpuser:admin", "via": "token"}
    assert httpx.get(f"{API}/me", headers={"Authorization": "Bearer not-a-token"}).status_code == 401


def test_quality_monitor_projects_into_catalog(api):
    spec = {"name": f"e2e volume {RUN}", "dataset_urn": DS, "type": "volume",
            "config": {"source": "catalog", "min_rows": 1000}, "severity": "HIGH"}
    m = api.post("/quality/monitors", json=spec)
    assert m.status_code == 201, m.text
    mid = m.json()["id"]
    # Timeseries aspects are indexed asynchronously (MCL → OpenSearch), so the first runs may
    # legitimately report ERROR "no datasetProfile yet".
    ev = eventually(lambda: (lambda e: e if e["metric_value"] == 1200 else None)(
        api.post(f"/quality/monitors/{mid}/run").json()), timeout=30)
    assert ev["status"] == "PASS" and ev["emitted"]

    q = """query($urn: String!) { dataset(urn: $urn) { assertions(start: 0, count: 10) { total assertions {
            urn info { type description source { type } }
            runEvents(limit: 5) { total failed runEvents { status result { type actualAggValue } } } } } } }"""

    def assertion_with_success():
        res = gql(q, {"urn": DS})["dataset"]["assertions"]
        if not res["total"]:
            return None
        events = res["assertions"][0]["runEvents"]["runEvents"]
        return res if any(e["result"]["type"] == "SUCCESS" and e["result"]["actualAggValue"] == 1200
                          for e in events) else None

    res = eventually(assertion_with_success)
    a = res["assertions"][0]
    assert a["info"]["type"] == "VOLUME" and a["info"]["source"]["type"] == "NATIVE"

    # Drop the row count below the floor → FAIL → native incident on the dataset.
    emit(DS, "datasetProfile", {"timestampMillis": int(time.time() * 1000), "rowCount": 12, "columnCount": 3})
    bad = eventually(lambda: (lambda e: e if e["metric_value"] == 12 else None)(
        api.post(f"/quality/monitors/{mid}/run").json()), timeout=30)
    assert bad["status"] == "FAIL"
    iq = """query($urn: String!) { dataset(urn: $urn) { incidents(start: 0, count: 10) { total incidents {
             urn incidentType title incidentStatus { state } } } } }"""
    inc = eventually(lambda: (lambda r: r if r["total"] >= 1 else None)(gql(iq, {"urn": DS})["dataset"]["incidents"]))
    assert inc["incidents"][0]["incidentType"] == "VOLUME"
    assert inc["incidents"][0]["incidentStatus"]["state"] == "ACTIVE"

    # Recovery auto-resolves the incident.
    emit(DS, "datasetProfile", {"timestampMillis": int(time.time() * 1000), "rowCount": 1300, "columnCount": 3})
    eventually(lambda: api.post(f"/quality/monitors/{mid}/run").json()["status"] == "PASS", timeout=30)
    resolved = eventually(lambda: (lambda r: r if r["incidents"][0]["incidentStatus"]["state"] == "RESOLVED" else None)(
        gql(iq, {"urn": DS})["dataset"]["incidents"]))
    assert resolved


def test_pdp_and_core_plugin(api):
    d = httpx.post(f"{API}/governance/simulate", headers={"Authorization": f"Bearer {TOKEN}"}, json={
        "actor": "urn:li:corpuser:nobody", "privilege": "EDIT_ENTITY_TAGS", "resource_urn": DS}).json()
    assert d["decision"] in ("DENY", "NOT_APPLICABLE")
    compiled = api.get("/governance/policies", params={"include_compiled": "true"}).json()["compiled"]
    assert any(p["source"] == "native" for p in compiled)  # native policies imported from Cairn Core


def test_pii_propagates_along_column_lineage():
    esm = {"editableSchemaFieldInfo": [{"fieldPath": "customer_email", "glossaryTerms": {
        "terms": [{"urn": "urn:li:glossaryTerm:Classification.PII.Email"}],
        "auditStamp": {"time": 0, "actor": "urn:li:corpuser:admin"}}}],
        "created": {"time": 0, "actor": "urn:li:corpuser:admin"}}
    emit(DS, "editableSchemaMetadata", esm)
    q = """query($urn: String!) { dataset(urn: $urn) { editableSchemaMetadata { editableSchemaFieldInfo {
             fieldPath glossaryTerms { terms { term { urn } } } } } } }"""

    def propagated():
        infos = ((gql(q, {"urn": DS_DOWN})["dataset"].get("editableSchemaMetadata") or {})
                 .get("editableSchemaFieldInfo") or [])
        return [t["term"]["urn"] for i in infos if i["fieldPath"] == "email"
                for t in (i.get("glossaryTerms") or {}).get("terms", [])]

    terms = eventually(propagated, timeout=90)
    assert "urn:li:glossaryTerm:Classification.PII.Email" in terms


def test_certification_workflow_projects_tag(api):
    emit(DS, "editableDatasetProperties", {"description": "Certified orders table for revenue reporting.",
                                           "created": {"time": 0, "actor": "urn:li:corpuser:admin"}})
    wf = api.post("/governance/workflows", json={"entity_urn": DS})
    assert wf.status_code == 201, wf.text
    # The requester (catalog) cannot approve their own request (separation of duties).
    assert api.post(f"/governance/workflows/{wf.json()['id']}/approve").status_code == 403


def test_mcp_streamable_http():
    h = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream",
         "Content-Type": "application/json"}
    assert httpx.post(MCP, json={}, headers={"Accept": h["Accept"]}).status_code == 401
    with httpx.Client(timeout=60) as c:
        init = c.post(MCP, headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "1"}}})
        assert init.status_code == 200
        headers = dict(h)
        if init.headers.get("mcp-session-id"):
            headers["mcp-session-id"] = init.headers["mcp-session-id"]
        r = c.post(MCP, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "search_assets", "arguments": {"query": f"e2e_{RUN}"}}})
        text = r.text
        payload = json.loads(text[text.index("{"):]) if text.startswith("event:") or text.startswith("data:") else r.json()
        content = json.loads(payload["result"]["content"][0]["text"])
        assert any(item["urn"] in (DS, DS_DOWN) for item in content["results"]), content


def test_remote_agent_ingests_private_source(api):
    """In-VPC agent flow (needs an enrolled agent in pool 'finance', see README quickstart)."""
    if not any(a["pool"] == "finance" and a["status"] == "ONLINE" for a in api.get("/agents").json()):
        pytest.skip("no ONLINE agent in pool 'finance'")
    recipe = {"source": {"type": "postgres", "config": {
        "host_port": "finance-db:5432", "database": "finance", "username": "catalog_reader",
        "password": "${FINANCE_PG_PASSWORD}", "profiling": {"enabled": False}}}}
    src = gql("mutation($i: UpdateIngestionSourceInput!){ createIngestionSource(input:$i) }", {"i": {
        "name": f"e2e finance {RUN}", "type": "postgres",
        "config": {"recipe": json.dumps(recipe), "executorId": "finance"}}})["createIngestionSource"]
    er = gql("mutation($i: CreateIngestionExecutionRequestInput!){ createIngestionExecutionRequest(input:$i) }",
             {"i": {"ingestionSourceUrn": src}})["createIngestionExecutionRequest"]
    q = "query($u: String!){ executionRequest(urn:$u){ result { status } } }"
    status = eventually(lambda: (lambda r: r["status"] if r and r["status"] in ("SUCCESS", "FAILURE") else None)(
        gql(q, {"u": er})["executionRequest"]["result"]), timeout=180, interval=5)
    assert status == "SUCCESS"
    run = next(r for r in api.get("/agents/runs").json() if r["execution_request_urn"] == er)
    assert run["mcps_forwarded"] > 0 and run["mcps_rejected"] == 0
    customers = "urn:li:dataset:(urn:li:dataPlatform:postgres,finance.finance.customers,PROD)"
    fields = gql("query($u: String!){ dataset(urn:$u){ schemaMetadata(version:0){ fields { fieldPath } } } }",
                 {"u": customers})["dataset"]["schemaMetadata"]["fields"]
    assert {"customer_id", "email"} <= {f["fieldPath"] for f in fields}
    recipe_in_core = gql("query($u: String!){ ingestionSource(urn:$u){ config { recipe } } }",
                        {"u": src})["ingestionSource"]["config"]["recipe"]
    assert "${FINANCE_PG_PASSWORD}" in recipe_in_core  # only the reference ever leaves the VPC
    # The run's log streamed back from the agent, with the resolved password scrubbed in the VPC.
    logs = api.get(f"/agents/tasks/{run['task_id']}/logs").json()
    text = "\n".join(logs["lines"])
    assert logs["done"] and "--- attempt 1 on cairn-remote-agent" in text, text[-2000:]
    assert "started on the agent (postgres source)" in text and "finished: SUCCESS" in text
    assert "reader-demo-only" not in text
    assert api.get(f"/agents/tasks/{run['task_id']}/logs", params={"after": logs["next"]}).json()["lines"] == []


def test_remote_agents_in_workspace_settings(api):
    """People who manage ingestion get a Remote agents tab in Workspace settings: pools, runs, live logs."""
    done = [r for r in api.get("/agents/runs").json() if r["status"] == "SUCCESS" and r["pool"] == "finance"]
    if not done or not os.environ.get("CAIRN_E2E_UI_PASSWORD"):
        pytest.skip("needs a finished remote run and CAIRN_E2E_UI_PASSWORD")
    out = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "ui_agents.js")], capture_output=True,
                         text=True, timeout=240, env={
                             **os.environ, "EDGE": EDGE, "TASK_ID": done[0]["task_id"],
                             "SHOTS": os.environ.get("CAIRN_E2E_SCREENSHOT_DIR", ""),
                             "UI_USER": os.environ.get("CAIRN_E2E_UI_USER", "admin"),
                             "UI_PASSWORD": os.environ["CAIRN_E2E_UI_PASSWORD"]})
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["tab"] and result["pool"] and result["run"] and not result["errors"], result
    assert "finished: SUCCESS" in result["log"] and "reader-demo-only" not in result["log"]


def test_remote_agent_tasks_can_be_cancelled(api):
    """A task in a pool with no agent waits in the queue; cancelling it means it never runs."""
    pool = f"e2e-idle-{RUN}"
    assert api.post("/agents/pools", json={"name": pool, "allowed_platforms": ["postgres"]}).status_code == 201
    task = api.post("/agents/tasks", json={"pool": pool, "recipe": {"source": {"type": "postgres", "config": {
        "host_port": "x:5432", "password": "${NOPE}"}}}}).json()
    r = api.post(f"/agents/tasks/{task['task_id']}/cancel")
    assert r.status_code == 200 and r.json()["was"] == "QUEUED", r.text
    assert api.post(f"/agents/tasks/{task['task_id']}/cancel").status_code == 409
    run = next(x for x in api.get("/agents/runs").json() if x["task_id"] == task["task_id"])
    assert run["status"] == "CANCELLED" and run["agent_id"] is None
    assert any("cancelled by" in line for line in api.get(f"/agents/tasks/{task['task_id']}/logs").json()["lines"])


def test_product_docs_served():
    """In-app help links point at /docs: every page and every link between pages resolves."""
    r = httpx.get(f"{EDGE}/docs", follow_redirects=False, timeout=10)
    assert r.status_code == 301 and r.headers["location"].endswith("/docs/") and "8080" not in r.headers["location"]
    index = httpx.get(f"{EDGE}/docs/", timeout=10)
    assert index.status_code == 200 and "Cairn documentation" in index.text
    pages = sorted(set(re.findall(r'href="([a-z0-9-]+\.html)"', index.text)))
    assert len(pages) >= 10, pages
    for page in pages:
        body = httpx.get(f"{EDGE}/docs/{page}", timeout=10)
        assert body.status_code == 200, page
        for href in set(re.findall(r'href="([a-z0-9-]+\.html)(?:#[^"]*)?"', body.text)):
            assert href in pages, f"{page} links to missing {href}"
    assert httpx.get(f"{EDGE}/cairn/api/docs", timeout=10).status_code == 200   # linked from the API page


def test_builtin_maintenance_sources_run_offline():
    """Garbage collection and documents indexing run from the executor's pre-built environments."""
    q = "mutation($u: String!) { createIngestionExecutionRequest(input: { ingestionSourceUrn: $u }) }"
    runs = {src: gql(q, {"u": f"urn:li:dataHubIngestionSource:{src}"})["createIngestionExecutionRequest"]
            for src in ("datahub-gc", "datahub-documents")}
    status_q = "query($u: String!) { executionRequest(urn: $u) { result { status report } } }"

    def done():
        out = {}
        for src, urn in runs.items():
            result = gql(status_q, {"u": urn})["executionRequest"]["result"] or {}
            if result.get("status") in (None, "RUNNING", "PENDING"):
                return None
            out[src] = result
        return out
    results = eventually(done, timeout=300, interval=5)
    for src, result in results.items():
        assert result["status"] == "SUCCESS", f"{src}: {result['report'][-1500:]}"
        assert "bundled" in result["report"], f"{src} installed packages at run time"
