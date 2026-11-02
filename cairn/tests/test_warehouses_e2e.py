"""Warehouses (cairn.dataaccess.warehouses) with a real Trino: the proxy starts a stopped warehouse on the
first query (once, however many arrive together), Trino's follow-up URLs point back at the proxy, an
idle warehouse is stopped, and the next query starts it again. Docker scaler (Compose); the Kubernetes
scaler against a real API server is in tests/test_warehouses_k8s.py. Needs docker and the Trino image."""

import asyncio
import os
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from test_trino_e2e import TRINO, port, wait

pytestmark = pytest.mark.skipif(not shutil.which("docker") or subprocess.run(
    ["docker", "image", "inspect", TRINO], capture_output=True).returncode != 0, reason="needs docker and the Trino image")


def running(name):
    out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name], capture_output=True, text=True).stdout
    return out.strip() == "true"


@pytest.fixture(scope="module")
def warehouse():
    from cairn.dataaccess.warehouses import DockerScaler, Warehouse, serve

    d = tempfile.mkdtemp(prefix="wh-")
    tp, pp = port(), port()
    os.makedirs(f"{d}/catalog")
    open(f"{d}/catalog/tpch.properties", "w").write("connector.name=tpch\n")
    open(f"{d}/config.properties", "w").write("\n".join([
        "coordinator=true", "node-scheduler.include-coordinator=true", f"http-server.http.port={tp}",
        f"discovery.uri=http://127.0.0.1:{tp}", "http-server.process-forwarded=true", ""]))
    os.chmod(d, 0o755)
    name = f"cairn-wh-sales-{os.getpid()}"
    subprocess.run(["docker", "create", "--name", name, "--network", "host",
                    "-v", f"{d}/catalog:/etc/trino/catalog:ro", "-v", f"{d}/config.properties:/etc/trino/config.properties:ro",
                    TRINO], check=True, capture_output=True)          # created, not started: the proxy starts it
    w = Warehouse("sales", pp, f"http://127.0.0.1:{tp}", DockerScaler(name), idle_minutes=5 / 60, start_timeout=240)
    loop = asyncio.new_event_loop()
    threading.Thread(target=lambda: loop.run_until_complete(serve([w], host="127.0.0.1", reap_every=1)), daemon=True).start()
    wait(pp)
    yield {"w": w, "name": name, "port": pp, "trino_port": tp}
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    shutil.rmtree(d, ignore_errors=True)


def query(port_, sql="SELECT count(*) FROM tpch.tiny.nation"):
    import trino

    conn = trino.dbapi.connect(host="127.0.0.1", port=port_, user="ana", http_scheme="http", request_timeout=300)
    cur = conn.cursor()
    cur.execute(sql)
    return cur.fetchall()


def test_a_stopped_warehouse_starts_on_the_first_query_and_stops_when_idle(warehouse):
    import httpx

    w, name, pp = warehouse["w"], warehouse["name"], warehouse["port"]
    assert not running(name)
    # Three queries arrive while it is stopped: one start, all answered.
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(lambda _: query(pp), range(3)))
    assert results == [[[25]]] * 3 and w.starts == 1 and running(name)
    # Trino's follow-up URLs point back at the proxy, not at the warehouse.
    r = httpx.post(f"http://127.0.0.1:{pp}/v1/statement", content="SELECT 1", headers={"X-Trino-User": "ana"}, timeout=60)
    assert r.status_code == 200 and r.json()["nextUri"].startswith(f"http://127.0.0.1:{pp}/"), r.json()
    rows = []
    while "nextUri" in r.json():                  # follow it to the end, through the proxy
        assert r.json()["nextUri"].startswith(f"http://127.0.0.1:{pp}/")
        r = httpx.get(r.json()["nextUri"], timeout=60)
        rows += r.json().get("data") or []
    assert rows == [[1]]
    # A long query keeps it awake: its client polls through the proxy all along.
    assert query(pp, "SELECT count(*) FROM tpch.sf1.lineitem WHERE comment LIKE '%a%' AND quantity > 1") != []
    # Idle for longer than idle_minutes (5 s here): stopped.
    end = time.time() + 60
    while running(name) and time.time() < end:
        time.sleep(1)
    assert not running(name) and w.stops >= 1
    # The next query starts it again.
    assert query(pp) == [[25]] and w.starts == 2
    h = httpx.get(f"http://127.0.0.1:{pp}/cairn/warehouse", timeout=10).json()
    assert h == {"warehouse": "sales", "ready": True, "starts": 2, "stops": w.stops}


def test_configs_are_checked():
    from cairn.dataaccess.warehouses import WarehouseError, load

    ok = {"name": "sales", "port": 18081, "upstream": "http://trino-sales:8080", "docker": {"container": "trino-sales"}}
    assert [w.name for w in load({"warehouses": [ok]})] == ["sales"]
    for bad, msg in [({**ok, "name": "Sales"}, "lowercase"), ({**ok, "upstream": "trino-sales"}, "upstream"),
                     ({k: v for k, v in ok.items() if k != "docker"}, "kubernetes or docker"),
                     ({**ok, "tls": {"cert": "/x"}}, "cert and key")]:
        with pytest.raises(WarehouseError, match=msg):
            load({"warehouses": [bad]})
    with pytest.raises(WarehouseError, match="a port of its own"):
        load({"warehouses": [ok, {**ok, "name": "finance"}]})
    with pytest.raises(WarehouseError, match="no warehouses"):
        load({"warehouses": []})
