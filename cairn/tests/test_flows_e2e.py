"""Cairn Flows end to end (L122) on a real Trino, Cairn Catalog and Cairn Store, with Cairn's API in front: a flow
refreshes a derived table on Trino as its owner, a check stops the report on bad data and the owner is told, a
branch picks its side from a row count, a write to a table (Trino's lineage, the change feed) starts a flow, a
Python step runs in a real job container reading Trino with the owner's token, an approval holds the run until
someone approves over HTTP, and an owner without the grant is refused by Trino.

Needs docker, CAIRN_STORE_BIN and CAIRN_CATALOG_BIN (the Trino fixtures of tests/test_trino_e2e.py) and the
image CAIRN_FLOWS_TEST_JOB_IMAGE (default python:3.12-slim) for the job step."""

from __future__ import annotations

import os
import time

import httpx
import pytest

from test_trino_e2e import _ready, cairn, lake, trino  # noqa: F401  (fixtures)

JOB_IMAGE = os.environ.get("CAIRN_FLOWS_TEST_JOB_IMAGE", "python:3.12-slim")
ADMIN, ANA, BOB = "urn:li:corpuser:admin", "urn:li:corpuser:ana", "urn:li:corpuser:bob"

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, CAIRN_STORE_BIN and CAIRN_CATALOG_BIN")

JOB = r'''
import json, os, ssl, urllib.request
ctx = ssl.create_default_context(cafile=os.environ["CAIRN_TRINO_CA_FILE"])
head = {"Authorization": "Bearer " + os.environ["CAIRN_TRINO_TOKEN"], "X-Trino-User": os.environ["CAIRN_USER"]}
doc = json.load(urllib.request.urlopen(urllib.request.Request(
    os.environ["CAIRN_TRINO_URL"] + "/v1/statement", data=b"SELECT count(*) FROM lake.flows_e2e.orders", headers=head),
    context=ctx, timeout=60))
rows = []
while True:
    rows += doc.get("data") or []
    if doc.get("error"):
        raise SystemExit(doc["error"]["message"])
    if not doc.get("nextUri"):
        break
    doc = json.load(urllib.request.urlopen(urllib.request.Request(doc["nextUri"], headers=head), context=ctx, timeout=60))
params = json.loads(os.environ["CAIRN_JOB_PARAMS"])
print(json.dumps({"user": os.environ["CAIRN_USER"], "orders": rows[0][0], "region": params["region"]}))
'''


def ds(table: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{table},PROD)"


@pytest.fixture(scope="module")
def flows(trino, cairn):  # noqa: F811
    """The API's own Flows (s.flows), its steps on Trino, derived tables, the change feed and Docker jobs."""
    from cairn.common.security import Principal
    from cairn.pipelines.derived import DerivedTables
    from cairn.pipelines.jobs import DockerRunner, Jobs
    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    definition = s.data_access.registry.definitions()["lakehouse"]

    def run(owner, sql):
        return s.data_access.run_on_trino(definition, owner, sql, 100_000)[1]
    for stmt in ["CREATE SCHEMA IF NOT EXISTS lake.flows_e2e",
                 "CREATE TABLE lake.flows_e2e.orders (id integer, region varchar, amount double)",
                 "INSERT INTO lake.flows_e2e.orders VALUES (1, 'EU', 10.0), (2, 'US', 5.0), (3, 'EU', 2.0)"]:
        run(ADMIN, stmt)
    for t in ("flows_e2e.orders", "flows_e2e.daily", "flows_e2e.anas"):
        s.graph.aspects[(ds(t), "schemaMetadata")] = {"fields": [{"fieldPath": c} for c in ("id", "region", "amount", "total")]}
    derived = DerivedTables(s.engine, run, "lake", s.audit, s.dataset_changes)
    authz = lambda privilege: lambda actor: s.authorize(Principal(actor, "token"), privilege, None,  # noqa: E731
                                                         raise_on_deny=False)
    jobs = Jobs(s.engine, DockerRunner(JOB_IMAGE, network="host", memory="512m", cpus="1"), authz("RUN_JOBS"),
                authz("MANAGE_SYSTEM_OPERATIONS"), lambda owner, ttl: s.trino_tokens.mint(owner, ttl)["token"],
                f"https://127.0.0.1:{trino.tls}", None, s.audit, f"{trino.dir}/ca.pem")
    f = s.flows
    f.b.workspace, f.b.derived, f.b.jobs = s.sql_workspace, derived, jobs
    f.changes = s.dataset_changes
    if getattr(s, "lakehouse", None) is not None:       # this Trino calls the lakehouse catalog "lake"
        s.lakehouse.trino_catalog = "lake"
    f.run_sql = run
    f.url, f.hub = url, s.notify
    return f


def steps_of(run):
    return {x["key"]: x for x in run["steps"]}


def test_a_flow_refreshes_checks_branches_and_reports_on_trino_as_its_owner(flows, trino):  # noqa: F811
    s_derived = flows.b.derived
    s_derived.create({"name": "daily", "target": "flows_e2e.daily", "mode": "full", "every_minutes": 1440,
                      "sql": "SELECT region, sum(amount) AS total FROM lake.flows_e2e.orders GROUP BY region"}, ADMIN, ADMIN)
    flows.create(ADMIN, {"yaml": """
kind: flow
name: nightly
notify: {on: [failure], to: [urn:li:corpuser:bob]}
steps:
  - {name: refresh, type: derived, table: daily, reads: [flows_e2e.orders]}
  - name: no-negatives
    type: check
    after: [refresh]
    sql: SELECT count(*) AS bad FROM flows_e2e.daily WHERE total < 0
    connection: lakehouse
    measure: bad
    op: "=="
    value: 0
  - {name: count, type: sql, after: [no-negatives], sql: "SELECT count(*) AS n FROM flows_e2e.daily", connection: lakehouse}
  - {name: size, type: branch, after: [count], condition: "steps.count.preview[0][0] >= 2", then: [report], else: [quiet]}
  - {name: report, type: sql, after: [size], sql: "SELECT region, total FROM flows_e2e.daily ORDER BY region", connection: lakehouse}
  - {name: quiet, type: notify, after: [size], message: "one region only"}
"""}, publish=True)
    run = flows.run(ADMIN, "nightly", wait=True, timeout_s=300)
    by = steps_of(run)
    assert run["status"] == "succeeded", run
    assert by["refresh"]["output"]["table"] == "daily" and by["refresh"]["output"]["kind"] in ("create", "full")
    assert {r["region"]: r["total"] for r in trino("SELECT region, total FROM lake.flows_e2e.daily")} == {"EU": 12.0, "US": 5.0}
    assert by["size"]["output"]["chosen"] == "then" and by["quiet"]["status"] == "skipped"
    assert by["report"]["output"]["preview"] == [["EU", 12.0], ["US", 5.0]]
    # The refresh ran as the flow's owner on Trino: its lineage names the flow's step.
    assert flows.b.derived.runs(flows.b.derived.list()[0]["id"])[0]["trigger"] == "flow"
    # Bad data: the check fails, the report does not run, the owner and the recipient are told.
    flows.run_sql(ADMIN, "INSERT INTO lake.flows_e2e.orders VALUES (4, 'APAC', -50.0)")
    try:
        run = flows.run(ADMIN, "nightly", wait=True, timeout_s=300)
        by = steps_of(run)
        assert run["status"] == "failed" and by["no-negatives"]["status"] == "failed"
        assert "check failed: bad is 1, not == 0" in by["no-negatives"]["error"]
        assert by["count"]["status"] == "skipped" and by["report"]["status"] == "skipped"
        # The run is recorded as failed, then its owner and recipients are told (the same worker, a moment later).
        for who in (ADMIN, BOB):
            end = time.time() + 30
            told = lambda who=who: any(n["type"] == "flow.failed" and "no-negatives: failed" in n["body"]  # noqa: E731
                                       for n in flows.hub.inbox(who))
            while not told() and time.time() < end:
                time.sleep(0.5)
            assert told(), (who, flows.hub.inbox(who)[:5])
    finally:                                     # the next tests count the orders: never leave the bad row behind
        flows.run_sql(ADMIN, "DELETE FROM lake.flows_e2e.orders WHERE id = 4")


def test_an_owner_without_the_grant_is_refused(flows):
    flows.b.derived.create({"name": "anas", "target": "flows_e2e.anas", "mode": "full", "every_minutes": 1440,
                            "sql": "SELECT region, sum(amount) AS total FROM lake.flows_e2e.orders GROUP BY region"},
                           ANA, ADMIN)
    flows.create(ANA, {"name": "anas", "steps": [
        {"name": "refresh", "type": "derived", "table": "anas"},
        {"name": "pay", "type": "sql", "sql": "SELECT salary FROM hr.salaries", "connection": "lakehouse"}]}, publish=True)
    run = flows.run(ANA, "anas", wait=True, timeout_s=300)
    by = steps_of(run)
    assert run["status"] == "failed" and run["run_as"] == ANA
    assert "Access Denied" in by["refresh"]["error"], by["refresh"]       # Trino, asking Cairn, as ana: no WRITE
    assert "may not query hr.salaries" in by["pay"]["error"]
    # An admin may run it, but it still runs as ana.
    run = flows.run(ADMIN, "anas", wait=True, timeout_s=300)
    assert run["status"] == "failed" and run["run_as"] == ANA and run["triggered_by"] == ADMIN


def test_a_write_starts_a_flow_that_runs_a_job_and_waits_for_approval(flows, cairn, trino):  # noqa: F811
    url, s = cairn
    flows.create(ADMIN, {"name": "on-orders", "params": [{"name": "region", "type": "string", "default": "EU"}],
                         "triggers": [{"type": "dataset", "tables": ["flows_e2e.orders"]}],
                         "steps": [{"name": "py", "type": "python", "code": JOB, "params": {"region": "{{ params.region }}"},
                                    "timeout_s": 300},
                                   {"name": "gate", "type": "approval", "after": ["py"], "approvers": [BOB],
                                    "message": "{{ steps.py.result.orders }} orders: go on?"},
                                   {"name": "tell", "type": "notify", "after": ["gate"],
                                    "message": "{{ steps.py.result.orders }} orders, approved by {{ steps.gate.by }}"}]},
                 publish=True)
    assert flows.run_due() == 0
    trino("INSERT INTO lake.flows_e2e.orders VALUES (5, 'EU', 1.0)")      # through Trino: its lineage is the change
    end = time.time() + 60
    started = 0
    while not started and time.time() < end:
        started = flows.run_due()
        time.sleep(1)
    runs = flows.runs(ADMIN, "on-orders")
    recent = s.dataset_changes.since([flows.table_urn("flows_e2e.orders")], 0, 50)
    assert len(runs) == 1 and runs[0]["trigger"] == "dataset", (runs, flows.table_urn("flows_e2e.orders"), recent)
    rid = runs[0]["id"]
    end = time.time() + 300
    while flows._run_public(rid)["status"] != "waiting" and time.time() < end:
        time.sleep(1)
    run = flows._run_public(rid)
    py = steps_of(run)["py"]
    assert py["status"] == "ok", py
    assert py["output"]["result"] == {"user": "admin", "orders": 4, "region": "EU"}   # read on Trino as the owner
    assert steps_of(run)["gate"]["status"] == "waiting"
    bob = {"X-Cairn-Actor": BOB}
    assert [a["step"] for a in httpx.get(f"{url}/v1/flows/approvals", headers=bob, timeout=30).json()] == ["gate"]
    r = httpx.post(f"{url}/v1/flows/runs/{rid}/steps/gate/approve", json={"comment": "fine"},
                   headers={"X-Cairn-Actor": ANA}, timeout=30)
    assert r.status_code == 403
    r = httpx.post(f"{url}/v1/flows/runs/{rid}/steps/gate/approve", json={"comment": "fine"}, headers=bob, timeout=30)
    assert r.status_code == 200, r.text
    done = flows.wait_for(rid, 120)
    assert done["status"] == "succeeded", done
    assert any(n["type"] == "flow.notice" and n["body"] == "4 orders, approved by urn:li:corpuser:bob"
               for n in flows.hub.inbox(ADMIN))
    # Its job run is kept with the run, and nothing secret reached its log.
    log = flows.step_log(ADMIN, rid, "py")["log"]
    assert s.trino_tokens is not None and "Bearer" not in log
