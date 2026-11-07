"""Adaptive warehouses (Snowflake gap J), with real Trinos: one port in front of a small and a large warehouse,
choosing one for each query. Cairn's hint endpoint (served here over HTTP, with its key) reads query history. A
query whose earlier runs (same shape, other literals) were heavy goes to the large warehouse, which starts for it.
Others go to the small one, or overflow to the large one while the small one is busy. A query's follow-up polls
reach the warehouse it started on, so its results come back whole. Without a hint, queries are routed by load
alone.

Needs docker and the Trino image."""

import asyncio
import os
import shutil
import subprocess
import tempfile
import threading
import time

import httpx
import pytest

from test_trino_e2e import TRINO, port, wait
from test_warehouses_e2e import query, running

pytestmark = pytest.mark.skipif(not shutil.which("docker") or subprocess.run(
    ["docker", "image", "inspect", TRINO], capture_output=True).returncode != 0, reason="needs docker and the Trino image")

KEY = "trino-access-key-" + "k" * 24
HEAVY = "SELECT count(*) AS n FROM tpch.tiny.orders WHERE orderkey > {}"
LIGHT = "SELECT count(*) AS n FROM tpch.tiny.nation WHERE nationkey >= {}"
SLOW = "SELECT count(*) AS n FROM tpch.sf1.lineitem a CROSS JOIN tpch.tiny.region b WHERE a.quantity > {}"


def trino_container(d, label):
    tp = port()
    os.makedirs(f"{d}/{label}/catalog")
    open(f"{d}/{label}/catalog/tpch.properties", "w").write("connector.name=tpch\n")
    open(f"{d}/{label}/config.properties", "w").write("\n".join([
        "coordinator=true", "node-scheduler.include-coordinator=true", f"http-server.http.port={tp}",
        f"discovery.uri=http://127.0.0.1:{tp}", "http-server.process-forwarded=true", ""]))
    name = f"cairn-wha-{label}-{os.getpid()}"
    subprocess.run(["docker", "create", "--name", name, "--network", "host",
                    "-v", f"{d}/{label}/catalog:/etc/trino/catalog:ro",
                    "-v", f"{d}/{label}/config.properties:/etc/trino/config.properties:ro", TRINO],
                   check=True, capture_output=True)
    return name, tp


def completed(qid, sql, cpu_ms):
    """A Trino QueryCompletedEvent, as Trino's HTTP event listener posts it to Cairn."""
    return {"metadata": {"queryId": qid, "query": sql, "queryState": "FINISHED"},
            "context": {"user": "ana", "source": "test", "serverAddress": "127.0.0.1"},
            "statistics": {"cpuTime": cpu_ms / 1000, "wallTime": cpu_ms / 1000, "queuedTime": 0,
                           "physicalInputBytes": 50_000_000_000 if cpu_ms > 60_000 else 1000,
                           "peakUserMemoryBytes": 0, "outputRows": 1, "completedSplits": 1},
            "createTime": "2026-11-01T10:00:00Z", "endTime": "2026-11-01T10:02:00Z", "ioMetadata": {"inputs": []}}


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    import uvicorn
    from fastapi import FastAPI
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.dataaccess.query_history import QueryHistory
    from cairn.dataaccess.warehouse_status import WarehouseStatus, build_warehouses_router
    from cairn.dataaccess.warehouses import Adaptive, DockerScaler, Warehouse, http_hint, serve

    db = tmp_path_factory.mktemp("adaptive")
    engine = create_engine(f"sqlite:///{db}/cairn.db")
    init_db(engine)
    history = QueryHistory(engine)
    for i in range(3):                                    # the heavy report ran three times, two minutes each
        history.record(completed(f"q_heavy_{i}", HEAVY.format(1000 + i), 120_000))
    history.record(completed("q_light_0", LIGHT.format(3), 40))
    # Cairn's hint endpoint, over HTTP, as the proxy reaches it.
    api = FastAPI()
    api.include_router(build_warehouses_router(WarehouseStatus("http://127.0.0.1", {}), lambda: None, lambda *a: None,
                                               key=KEY, history=history))
    hp = port()
    hint_server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=hp, log_level="warning"))
    threading.Thread(target=hint_server.run, daemon=True).start()
    wait(hp)

    d = tempfile.mkdtemp(prefix="wha-")
    os.chmod(d, 0o755)
    small_c, small_tp = trino_container(d, "small")
    large_c, large_tp = trino_container(d, "large")
    small = Warehouse("small", port(), f"http://127.0.0.1:{small_tp}", DockerScaler(small_c), idle_minutes=30,
                      start_timeout=240)
    large = Warehouse("large", port(), f"http://127.0.0.1:{large_tp}", DockerScaler(large_c), idle_minutes=30,
                      start_timeout=240)
    auto = Adaptive("auto", port(), [small, large], {"cpu_ms": 60_000}, overflow_at=1,
                    hint=http_hint(f"http://127.0.0.1:{hp}/v1/warehouses/hint", KEY, timeout=5))
    blind = Adaptive("blind", port(), [small, large], {"cpu_ms": 60_000}, overflow_at=1,
                     hint=http_hint(f"http://127.0.0.1:{port()}/v1/warehouses/hint", KEY, timeout=1))   # nobody there
    loop = asyncio.new_event_loop()
    threading.Thread(target=lambda: loop.run_until_complete(
        serve([small, large], host="127.0.0.1", reap_every=2, adaptive=[auto, blind])), daemon=True).start()
    wait(auto.port)
    wait(blind.port)
    yield {"auto": auto, "blind": blind, "small": small_c, "large": large_c, "hint": f"http://127.0.0.1:{hp}"}
    hint_server.should_exit = True
    subprocess.run(["docker", "rm", "-f", small_c, large_c], capture_output=True)
    shutil.rmtree(d, ignore_errors=True)


def test_queries_go_where_their_history_says(stack):
    auto = stack["auto"]
    # A light query goes to the small warehouse, which starts for it; the large one stays stopped.
    assert query(auto.port, LIGHT.format(10)) == [[15]]
    assert running(stack["small"]) and not running(stack["large"])
    assert auto.routed == {"small:history": 1}
    # The heavy report, with literals it never ran with: the large warehouse starts and answers it. Its follow-up
    # polls are routed by query id; were they sent to the small warehouse, the client would get an error.
    assert query(auto.port, HEAVY.format(7)) == [[14993]]
    assert running(stack["large"]) and auto.routed.get("large:history") == 1
    # A shape never seen goes to the small one.
    assert query(auto.port, "SELECT count(*) AS n FROM tpch.tiny.region") == [[5]]
    assert auto.routed.get("small:default") == 1
    status = httpx.get(f"http://127.0.0.1:{auto.port}/cairn/warehouse").json()
    assert [t["warehouse"] for t in status["tiers"]] == ["small", "large"] and status["routed"] == auto.routed


def test_a_busy_small_warehouse_overflows_to_the_large_one(stack):
    auto = stack["auto"]
    before = auto.routed.get("large:overflow", 0)
    end = time.time() + 30
    while time.time() < end and (auto.busy(auto.tiers[0]) or auto.busy(auto.tiers[1])):   # earlier queries are over
        time.sleep(0.5)
    results = {}
    slow = threading.Thread(target=lambda: results.update(slow=query(auto.port, SLOW.format(0))))
    slow.start()
    end = time.time() + 60
    while time.time() < end and auto.busy(auto.tiers[0]) < 1:
        time.sleep(0.2)
    assert auto.busy(auto.tiers[0]) >= 1, "the slow query never reached the small warehouse"
    assert query(auto.port, LIGHT.format(20)) == [[5]]        # while the small one is busy
    assert auto.routed.get("large:overflow", 0) == before + 1
    slow.join(300)
    assert results["slow"][0][0] > 0


def test_without_a_hint_queries_are_routed_by_load_alone(stack):
    blind = stack["blind"]
    assert query(blind.port, HEAVY.format(9)) == [[14993]]     # no history to go on: the small warehouse
    assert blind.routed == {"small:default": 1}


def test_the_hint_endpoint_needs_the_key_and_tells_nothing_but_figures(stack):
    url = stack["hint"] + "/v1/warehouses/hint"
    assert httpx.post(url, json={"sql": HEAVY.format(1)}).status_code == 404
    assert httpx.post(url, json={"sql": HEAVY.format(1)}, headers={"X-Cairn-Trino-Key": "wrong"}).status_code == 404
    got = httpx.post(url, json={"sql": HEAVY.format(1)}, headers={"X-Cairn-Trino-Key": KEY}).json()
    assert got["runs"] == 3 and got["cpu_ms"] == 120_000 and set(got) == {
        "fingerprint", "runs", "cpu_ms", "input_bytes", "peak_memory_bytes", "wall_ms"}
    assert httpx.post(url, json={"sql": "SELECT 42"}, headers={"X-Cairn-Trino-Key": KEY}).json() == {"runs": 0}


def test_configs_are_checked():
    from cairn.dataaccess.warehouses import WarehouseError, load, load_adaptive

    raw = {"warehouses": [{"name": "s", "port": 9001, "upstream": "http://x", "docker": {"container": "a"}},
                          {"name": "l", "port": 9002, "upstream": "http://y", "docker": {"container": "b"}}]}
    whs = load(raw)
    for bad in [{"name": "a", "port": 9001, "tiers": ["s", "l"], "large_when": {"cpu_ms": 1}},      # port taken
                {"name": "s", "port": 9003, "tiers": ["s", "l"], "large_when": {"cpu_ms": 1}},      # a warehouse's name
                {"name": "a", "port": 9003, "tiers": ["s"], "large_when": {"cpu_ms": 1}},           # one tier
                {"name": "a", "port": 9003, "tiers": ["s", "z"], "large_when": {"cpu_ms": 1}},      # unknown tier
                {"name": "a", "port": 9003, "tiers": ["s", "l"], "large_when": {}},                 # no threshold
                {"name": "a", "port": 9003, "tiers": ["s", "l"], "large_when": {"rows": 5}},        # unknown figure
                {"name": "a", "port": 9003, "tiers": ["s", "l"], "large_when": {"cpu_ms": True}},   # not a number
                {"name": "a", "port": 9003, "tiers": ["s", "l"], "large_when": {"cpu_ms": 1}, "overflow_at": 0}]:
        with pytest.raises(WarehouseError):
            load_adaptive({**raw, "adaptive": [bad]}, whs)
    a = load_adaptive({**raw, "adaptive": [{"name": "a", "port": 9003, "tiers": ["s", "l"],
                                            "large_when": {"cpu_ms": 60000}}]}, whs)[0]
    assert [w.name for w in a.tiers] == ["s", "l"] and a.overflow_at == 8 and a.hint is None


def test_thresholds_are_compared_as_given():
    """Review finding: thresholds and figures were cut to whole numbers, so a fractional threshold under 1 sent
    every query with history to the large warehouse, and 100.5 counted as reaching 100.7."""
    from cairn.dataaccess.warehouses import Adaptive

    a = Adaptive("a", 1, [None, None], {"cpu_ms": 0.5})
    assert not a.big({"runs": 2, "cpu_ms": 0.2}) and a.big({"runs": 2, "cpu_ms": 0.5})
    b = Adaptive("b", 1, [None, None], {"wall_ms": 100.7})
    assert not b.big({"runs": 1, "wall_ms": 100.5}) and b.big({"runs": 1, "wall_ms": 101})
    assert not b.big({"runs": 0, "wall_ms": 10_000}) and not b.big(None)
