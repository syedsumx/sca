"""L46, chaos for remote agents: the gateway process is stopped and started while an agent is sending a run's
metadata, and Cairn Core (the metadata store behind the gateway) refuses writes for a while. The agent spools what it
cannot send and sends it again; the run ends with every change delivered once, in order. A real gateway (uvicorn)
and a real agent worker over HTTP."""

from __future__ import annotations

import socket
import threading
import time

from cairn.agent.client import AgentState, GatewayClient
from cairn.agent.gateway import EnqueueRequest, PoolSpec
from cairn.agent.sink import get_channel
from cairn.agent.worker import AgentConfig, AgentWorker
from cairn.app import create_gateway_app
from cairn.common.proposals import make_mcp
from cairn.services import build_services

ADMIN = "urn:li:corpuser:admin"
N = 60


def ds(i: int) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.chaos_{i},PROD)"


def test_a_gateway_restart_and_a_core_outage_mid_run_lose_nothing(settings, graph, tmp_path):
    import uvicorn

    services = build_services(settings, graph=graph, with_ai=False)
    gw = services.gateway
    gw.create_pool(PoolSpec(name="finance", allowed_platforms=["postgres"]), ADMIN)
    app = create_gateway_app(services)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    running: dict = {}

    def start():
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", timeout_keep_alive=75))
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        running.update(server=server, thread=t)
        for _ in range(200):
            if server.started:
                return
            time.sleep(0.05)

    def stop():
        running["server"].should_exit = True
        running["thread"].join(30)

    start()
    url = f"http://127.0.0.1:{port}"
    client = GatewayClient(url, AgentState(str(tmp_path / "agent")), timeout=10)
    client.enroll(gw.create_enrollment_token("finance", ADMIN)["token"], hostname="vpc-1")
    task_id = gw.enqueue(EnqueueRequest(pool="finance", recipe={"source": {"type": "postgres", "config": {}}}), ADMIN)["task_id"]
    events: list[str] = []

    real_emit = graph.emit
    core_down = threading.Event()
    refused = []

    def emit(mcps):
        if core_down.is_set():
            refused.append(len(mcps))
            raise ConnectionError("Cairn Core is unavailable")
        return real_emit(mcps)

    graph.emit = emit

    def runner(recipe):
        ch = get_channel(recipe["sink"]["config"]["run_id"])
        for i in range(N):
            if i == 15:
                stop()                                 # the gateway's process goes away mid-run
                events.append("gateway down")
            if i == 30:
                start()
                events.append("gateway up")
            if i == 35:
                core_down.set()                        # then the metadata store behind it
                events.append("core down")
            if i == 50:
                core_down.clear()
                events.append("core up")
            ch.add(make_mcp(ds(i), "status", {"removed": False}))
            time.sleep(0.05)
        return {}

    worker = AgentWorker(AgentConfig(gateway_url=url, state_dir=str(tmp_path / "agent"), batch_max_items=3,
                                     log_interval_seconds=3600), runner=runner, client=client)
    try:
        worker.tick()
        worker.pool.shutdown(wait=True)
    finally:
        graph.emit = real_emit
        stop()
    assert events == ["gateway down", "gateway up", "core down", "core up"]
    assert refused, "Core's outage refused nothing: the test did not test it"
    run = next(r for r in gw.list_runs() if r["task_id"] == task_id)
    assert run["status"] == "SUCCESS" and run["mcps_forwarded"] == N, run
    order = [m["entityUrn"] for m in graph.emitted if m["aspectName"] == "status" and "chaos_" in m["entityUrn"]]
    assert order == [ds(i) for i in range(N)], "changes were lost, repeated or reordered"
    assert not any((tmp_path / "agent" / "spool").glob("*/*"))      # nothing left behind
