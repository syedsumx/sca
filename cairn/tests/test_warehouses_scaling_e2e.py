"""A warehouse with several nodes, scaled with its load, with a real Trino: a coordinator that runs no
queries itself and two worker containers. The proxy starts a worker when a query arrives, a second when
two run at once, removes them after the load drops, and stops the coordinator when idle. "Start now"
starts the warehouse without a query, and only with the control key. Node-seconds reach the cost report.

Needs docker and the Trino image."""

import asyncio
import os
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from test_trino_e2e import TRINO, port, wait
from test_warehouses_e2e import query, running

pytestmark = pytest.mark.skipif(not shutil.which("docker") or subprocess.run(
    ["docker", "image", "inspect", TRINO], capture_output=True).returncode != 0, reason="needs docker and the Trino image")

KEY = "warehouse-control-key-" + "k" * 20


def node(d, name, http_port, coordinator_port, coordinator):
    os.makedirs(f"{d}/{name}/catalog")
    open(f"{d}/{name}/catalog/tpch.properties", "w").write("connector.name=tpch\n")
    lines = [f"coordinator={'true' if coordinator else 'false'}", f"http-server.http.port={http_port}",
             f"discovery.uri=http://127.0.0.1:{coordinator_port}"]
    if coordinator:
        # Queries run on workers only, and wait for one rather than fail.
        lines += ["node-scheduler.include-coordinator=false", "http-server.process-forwarded=true",
                  "query-manager.required-workers=1", "query-manager.required-workers-max-wait=5m"]
    open(f"{d}/{name}/config.properties", "w").write("\n".join(lines) + "\n")
    open(f"{d}/{name}/node.properties", "w").write(f"node.environment=docker\nnode.data-dir=/data/trino\nnode.id={name}\n")
    os.chmod(f"{d}/{name}", 0o755)
    subprocess.run(["docker", "create", "--name", name, "--network", "host", "--memory", "2g",
                    "-v", f"{d}/{name}/catalog:/etc/trino/catalog:ro",
                    "-v", f"{d}/{name}/config.properties:/etc/trino/config.properties:ro",
                    "-v", f"{d}/{name}/node.properties:/etc/trino/node.properties:ro", TRINO], check=True, capture_output=True)


@pytest.fixture(scope="module")
def cluster():
    from cairn.dataaccess.warehouses import DockerPool, DockerScaler, Warehouse, Workers, serve

    d = tempfile.mkdtemp(prefix="whc-")
    tag = os.getpid()
    cp, w1p, w2p, pp = port(), port(), port(), port()
    coord, w1, w2 = f"cairn-whc-coord-{tag}", f"cairn-whc-w1-{tag}", f"cairn-whc-w2-{tag}"
    node(d, coord, cp, cp, True)
    node(d, w1, w1p, cp, False)
    node(d, w2, w2p, cp, False)
    workers = Workers(DockerPool([w1, w2]), min=0, max=2, queries_per_worker=1, scale_down_minutes=5 / 60)
    w = Warehouse("finance", pp, f"http://127.0.0.1:{cp}", DockerScaler(coord), idle_minutes=45 / 60, start_timeout=240,
                  workers=workers, control_key=KEY)
    loop = asyncio.new_event_loop()
    threading.Thread(target=lambda: loop.run_until_complete(serve([w], host="127.0.0.1", reap_every=1)), daemon=True).start()
    wait(pp)
    yield {"w": w, "port": pp, "coord": coord, "w1": w1, "w2": w2, "loop": loop}
    subprocess.run(["docker", "rm", "-f", coord, w1, w2], capture_output=True)
    shutil.rmtree(d, ignore_errors=True)


def until(cond, seconds=120):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(1)
    return False


SLOW = "SELECT count(*) FROM tpch.sf1.lineitem l JOIN tpch.sf1.orders o ON l.orderkey = o.orderkey WHERE l.comment LIKE '%a%'"


def test_workers_follow_the_load_and_the_warehouse_stops_when_idle(cluster):
    w, pp = cluster["w"], cluster["port"]
    assert not running(cluster["coord"]) and not running(cluster["w1"])
    # The first query starts the coordinator, then one worker for it (the query waits for it in Trino).
    assert query(pp) == [[25]]
    assert running(cluster["coord"]) and running(cluster["w1"]) and not running(cluster["w2"])
    # Two queries at once: a second worker.
    seen_two = []
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(query, pp, SLOW) for _ in range(2)]
        seen_two.append(until(lambda: running(cluster["w2"]), 180))
        assert all(f.result() for f in futures)
    assert seen_two == [True] and w.workers.scale_ups >= 2
    h = httpx.get(f"http://127.0.0.1:{pp}/cairn/warehouse", timeout=10).json()
    assert h["workers"]["max"] == 2 and h["workers"]["scale_ups"] >= 2
    # The load is gone: the workers go after scale_down_minutes (5 s), before the coordinator's idle time.
    assert until(lambda: not running(cluster["w1"]) and not running(cluster["w2"]), 60)
    assert running(cluster["coord"]) and w.workers.scale_downs >= 1 and w.stops == 0
    # Then the coordinator, when idle (45 s).
    assert until(lambda: not running(cluster["coord"]), 120)
    assert w.stops == 1
    # Node-seconds were counted: the coordinator and its workers, for the time they ran.
    assert w.node_seconds > 30


def test_start_now_needs_the_control_key_and_starts_without_a_query(cluster):
    pp = cluster["port"]
    assert until(lambda: not running(cluster["coord"]), 120)
    r = httpx.post(f"http://127.0.0.1:{pp}/cairn/warehouse/start", headers={"X-Cairn-Warehouse-Key": "wrong"}, timeout=10)
    assert r.status_code == 403 and not running(cluster["coord"])
    from cairn.dataaccess.warehouse_status import WarehouseStatus

    status = WarehouseStatus("http://127.0.0.1", {"finance": pp}, control_key=KEY)
    assert asyncio.run(status.start("finance")) == {"name": "finance", "starting": True}
    assert until(lambda: asyncio.run(status.all(["finance"]))[0]["state"] == "running", 180)
    assert not running(cluster["w1"])                    # no queries: no workers (min 0)


def test_node_seconds_reach_the_cost_report_once(cluster, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.dataaccess.warehouse_status import WarehouseStatus, build_warehouses_router
    from cairn.reports.costs import CostService

    engine = create_engine(f"sqlite:///{tmp_path}/c.db")
    init_db(engine)
    costs = CostService(engine, None, lambda urn: False)
    app = FastAPI()
    app.include_router(build_warehouses_router(WarehouseStatus("http://127.0.0.1", {"finance": cluster["port"]}),
                                               lambda: None, lambda *a: None, None, costs, "trino-key"))
    c = TestClient(app)
    w = cluster["w"]
    body = {"id": w.usage_id, "warehouse": "finance", "start_ms": int(w.usage_since * 1000),
            "end_ms": int(w.usage_since * 1000) + 3_600_000, "node_seconds": 3 * 3600.0}
    assert c.post("/v1/warehouses/usage", json=body).status_code == 404               # no key
    os.environ["CAIRN_COST_WAREHOUSE_USD_PER_NODE_HOUR"] = "0.5"
    try:
        assert c.post("/v1/warehouses/usage", json=body, headers={"X-Cairn-Trino-Key": "trino-key"}).json() == {"recorded": True}
        assert c.post("/v1/warehouses/usage", json=body, headers={"X-Cairn-Trino-Key": "trino-key"}).json() == {"recorded": False}
    finally:
        del os.environ["CAIRN_COST_WAREHOUSE_USD_PER_NODE_HOUR"]
    rows = costs.jobs(days=3650, now_ms=body["end_ms"])
    assert rows == [{"job": "urn:li:warehouse:finance", "engine": "warehouse", "runs": 1, "seconds": 3600.0, "usd": 1.5}]
    # What the proxy counted is reported the same way, through its own report call.
    import uvicorn
    ap = port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=ap, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait(ap)
    try:
        counted = w.node_seconds
        assert counted > 0
        ok = asyncio.run_coroutine_threadsafe(w.report_usage(f"http://127.0.0.1:{ap}/v1/warehouses/usage", "trino-key"),
                                              cluster["loop"]).result(30)
        assert ok and w.node_seconds < counted
        assert {r["engine"] for r in costs.jobs(days=3650)} == {"warehouse"}
        assert costs.jobs(days=3650)[0]["runs"] >= 1
    finally:
        server.should_exit = True
