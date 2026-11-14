"""Remote agent operations: cancellation, streamed run logs and spooling through gateway outages."""

import logging
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from cairn.agent.client import AgentState, GatewayClient
from cairn.agent.gateway import EnqueueRequest, GatewayError, PoolSpec
from cairn.agent.sink import get_channel
from cairn.agent.worker import AgentConfig, AgentWorker
from cairn.app import create_api_app, create_gateway_app
from cairn.common.proposals import make_mcp
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services

ADMIN = "urn:li:corpuser:admin"
ALICE = {"X-Cairn-Actor": "urn:li:corpuser:alice"}


def ds(i: int) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.t{i},PROD)"


class Flaky:
    """An HTTP client whose batch uploads fail with a transport error while ``down`` is set."""

    def __init__(self, inner):
        self.inner = inner
        self.down = False
        self.refused = 0

    def post(self, url, **kw):
        if self.down and "/batches" in url:
            self.refused += 1
            raise httpx.ConnectError("gateway unreachable")
        return self.inner.post(url, **kw)


@pytest.fixture
def stack(settings, graph, tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)      # backoff and drain waits
    monkeypatch.setenv("PG_PASSWORD", "s3cret-value")
    root = logging.getLogger()
    monkeypatch.setattr(root, "level", logging.INFO)          # as the agent's main() configures it
    services = build_services(settings, graph=graph, with_ai=False)
    services.pdp.set_bundle(PolicyBundle(policies=[Policy(
        id="alice-all", effect="allow", actions=["*"],
        condition={"attr": "subject.urn", "op": "eq", "value": "urn:li:corpuser:alice"})]))
    gw = services.gateway
    gw.create_pool(PoolSpec(name="finance", allowed_platforms=["postgres"], max_concurrency=2), ADMIN)
    http = Flaky(TestClient(create_gateway_app(services)))
    token = gw.create_enrollment_token("finance", ADMIN)["token"]
    client = GatewayClient("http://testserver", AgentState(str(tmp_path / "agent")), http=http)
    client.enroll(token, hostname="vpc-host-1")
    admin = TestClient(create_api_app(services))
    return services, gw, http, client, admin, tmp_path


def _worker(client, tmp, runner, **cfg):
    return AgentWorker(AgentConfig(gateway_url="http://testserver", state_dir=str(tmp / "agent"), batch_max_items=2,
                                   log_interval_seconds=3600, **cfg), runner=runner, client=client)


def _run_one(worker):
    worker.tick()
    worker.pool.shutdown(wait=True)


def _task(gw, er=None, max_attempts=3):
    return gw.enqueue(EnqueueRequest(pool="finance", execution_request_urn=er, max_attempts=max_attempts, recipe={
        "source": {"type": "postgres", "config": {"host_port": "db:5432", "password": "${PG_PASSWORD}"}}}),
        ADMIN)["task_id"]


# --------------------------------------------------------------------------- cancellation
def test_cancel_queued_task(stack):
    services, gw, http, client, admin, tmp = stack
    er = "urn:li:dataHubExecutionRequest:q1"
    tid = _task(gw, er)
    r = admin.post(f"/agents/tasks/{tid}/cancel", headers=ALICE)
    assert r.status_code == 200 and r.json() == {"task_id": tid, "status": "CANCELLED", "was": "QUEUED"}
    assert client.poll() is None                                       # never starts
    assert admin.post(f"/agents/tasks/{tid}/cancel", headers=ALICE).status_code == 409
    assert admin.post("/agents/tasks/task_nope/cancel", headers=ALICE).status_code == 404
    assert services.graph.get_aspect(er, "dataHubExecutionRequestResult")["status"] == "CANCELLED"
    assert admin.post(f"/agents/tasks/{tid}/cancel", headers={"X-Cairn-Actor": "urn:li:corpuser:mallory"}
                      ).status_code == 403


def test_cancel_reaches_a_running_task_at_the_next_heartbeat(stack):
    services, gw, http, client, admin, tmp = stack
    tid = _task(gw)
    seen = {}

    def runner(recipe):
        ch = get_channel(recipe["sink"]["config"]["run_id"])
        for i in range(4):
            ch.add(make_mcp(ds(i), "status", {"removed": False}))
        gw.cancel(tid, ADMIN)                                   # someone presses Cancel mid-run
        worker.handle_heartbeat(client.heartbeat([tid]))         # the agent hears about it
        try:
            for i in range(4, 100):
                ch.add(make_mcp(ds(i), "status", {"removed": False}))
        finally:
            seen["stopped_at"] = i
        return {}

    worker = _worker(client, tmp, runner)
    _run_one(worker)
    assert seen["stopped_at"] == 4                              # stopped at the next record
    run = next(r for r in gw.list_runs() if r["task_id"] == tid)
    assert run["status"] == "CANCELLED" and run["mcps_forwarded"] == 2      # buffered records are dropped
    logs = admin.get(f"/agents/tasks/{tid}/logs", headers=ALICE).json()
    assert logs["done"] and any("cancelled by" in line for line in logs["lines"])
    assert any("finished: CANCELLED" in line for line in logs["lines"])


def test_cancel_reaches_a_running_task_through_a_refused_batch(stack):
    services, gw, http, client, admin, tmp = stack
    tid = _task(gw)

    def runner(recipe):
        ch = get_channel(recipe["sink"]["config"]["run_id"])
        for i in range(3):                                      # one batch of two sent, one record buffered
            ch.add(make_mcp(ds(i), "status", {"removed": False}))
        gw.cancel(tid, ADMIN)                                   # no heartbeat in between
        for i in range(3, 10):
            ch.add(make_mcp(ds(i), "status", {"removed": False}))
        return {}

    _run_one(_worker(client, tmp, runner))
    run = next(r for r in gw.list_runs() if r["task_id"] == tid)
    assert run["status"] == "CANCELLED" and run["mcps_forwarded"] == 2


# --------------------------------------------------------------------------- logs
def test_run_logs_stream_scrubbed_and_paginated(stack):
    services, gw, http, client, admin, tmp = stack
    tid = _task(gw)
    log = logging.getLogger("ingestion.source.postgres")

    def runner(recipe):
        log.info("connecting to db:5432 with password %s", recipe["source"]["config"]["password"])
        log.debug("debug lines are not shipped")
        ch = get_channel(recipe["sink"]["config"]["run_id"])
        ch.add(make_mcp(ds(0), "status", {"removed": False}))
        raise RuntimeError("auth failed for password s3cret-value")

    worker = _worker(client, tmp, runner)
    _run_one(worker)
    body = admin.get(f"/agents/tasks/{tid}/logs", headers=ALICE).json()
    text = "\n".join(body["lines"])
    assert "--- attempt 1 on vpc-host-1" in body["lines"][0]
    assert "connecting to db:5432 with password ***" in text and "s3cret" not in text
    assert "debug lines" not in text
    report = [r for r in gw.list_runs() if r["task_id"] == tid][0]
    assert report["status"] == "QUEUED"                       # retried after the failure
    # The failure report reaching Cairn has the secret removed too.
    from sqlalchemy import select

    from cairn.agent.store import tasks
    with services.engine.connect() as conn:
        stored = conn.execute(select(tasks.c.report).where(tasks.c.task_id == tid)).scalar()
    assert "s3cret" not in str(stored) and "auth failed for password ***" in stored["error"]
    # Pagination: ask for what came after.
    later = admin.get(f"/agents/tasks/{tid}/logs", params={"after": body["next"]}, headers=ALICE).json()
    assert later["lines"] == [] and later["next"] == body["next"]
    assert admin.get("/agents/tasks/nope/logs", headers=ALICE).status_code == 404


def test_log_uploads_are_stored_once(stack):
    services, gw, http, client, admin, tmp = stack
    tid = _task(gw)
    run = client.poll()["run_id"]
    client.upload_logs(run, 0, ["one"])
    with pytest.raises(Exception) as e:
        client.upload_logs(run, 0, ["one"])                   # a retried upload
    assert getattr(e.value, "status", None) == 409
    client.upload_logs(run, 1, ["two"])
    assert gw.task_logs(tid)["lines"][-2:] == ["one", "two"]
    with pytest.raises(GatewayError):
        gw.task_logs("missing")


# --------------------------------------------------------------------------- spool
def test_batches_spool_through_an_outage_and_arrive_in_order(stack):
    services, gw, http, client, admin, tmp = stack
    tid = _task(gw)
    spool = tmp / "agent" / "spool"

    def runner(recipe):
        run_id = recipe["sink"]["config"]["run_id"]
        ch = get_channel(run_id)
        ch.add(make_mcp(ds(0), "status", {"removed": False}))
        ch.add(make_mcp(ds(1), "status", {"removed": False}))
        http.down = True                                       # the VPN drops
        for i in range(2, 8):
            ch.add(make_mcp(ds(i), "status", {"removed": False}))
        assert ch.pending >= 2 and len(os.listdir(spool / run_id)) == ch.pending
        # What is on disk is sealed for the gateway, not readable metadata.
        assert all("db.t" not in (spool / run_id / f).read_text() for f in os.listdir(spool / run_id))
        http.down = False                                      # back before the lease runs out
        return {}

    _run_one(_worker(client, tmp, runner))
    run = next(r for r in gw.list_runs() if r["task_id"] == tid)
    assert run["status"] == "SUCCESS" and run["mcps_forwarded"] == 8, run
    order = [m["entityUrn"] for m in services.graph.emitted if m["aspectName"] == "status"]
    assert order == [ds(i) for i in range(8)]
    assert not os.path.exists(spool) or not os.listdir(spool)
    assert http.refused >= 1


def test_a_full_spool_fails_the_run(stack):
    services, gw, http, client, admin, tmp = stack
    tid = _task(gw, max_attempts=1)

    def runner(recipe):
        ch = get_channel(recipe["sink"]["config"]["run_id"])
        http.down = True
        for i in range(50):
            ch.add(make_mcp(ds(i), "status", {"removed": False}))
        return {}

    _run_one(_worker(client, tmp, runner, spool_max_bytes=4096))
    http.down = False
    run = next(r for r in gw.list_runs() if r["task_id"] == tid)
    assert run["status"] == "FAILURE"


def test_an_outage_longer_than_the_lease_gives_the_task_back(stack):
    services, gw, http, client, admin, tmp = stack
    gw.lease_ms = 1000
    tid = _task(gw)

    def runner(recipe):
        ch = get_channel(recipe["sink"]["config"]["run_id"])
        http.down = True
        for i in range(3):
            ch.add(make_mcp(ds(i), "status", {"removed": False}))
        return {}

    worker = _worker(client, tmp, runner)
    worker.tick()
    worker.pool.shutdown(wait=True)
    result = list(worker.running.values())[0].result()
    assert result == "ABANDONED"
    http.down = False
    # Nothing was completed; the gateway re-queues the task when the lease expires.
    assert next(r for r in gw.list_runs() if r["task_id"] == tid)["status"] in ("LEASED", "RUNNING")
