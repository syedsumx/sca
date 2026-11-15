"""Alert evaluation across processes, on a real PostgreSQL and a real Prometheus: the notifier's pass and an operator's
"Evaluate now" in an API replica (two processes, two connection pools, one database) take turns, so a new alert fires,
notifies and opens its incident once, not once per pass.

Needs docker and the postgres:16-alpine and prom/prometheus images (CI: the trino job's observability step)."""

import os
import shutil
import socket
import subprocess
import threading
import time
import uuid

import httpx
import pytest

PG, PROM = "postgres:16-alpine", "prom/prometheus:v3.15.1"
ADMIN = "urn:li:corpuser:admin"


def _ready() -> bool:
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode:
        return False
    return all(subprocess.run(["docker", "image", "inspect", i], capture_output=True, timeout=30).returncode == 0
               for i in (PG, PROM))


pytestmark = pytest.mark.skipif(not _ready(), reason=f"needs docker and the {PG} and {PROM} images")


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    d = tmp_path_factory.mktemp("obs-lock")
    password = "obs-" + uuid.uuid4().hex
    (d / "pg.env").write_text(f"POSTGRES_PASSWORD={password}\nPOSTGRES_DB=cairn\n")
    tag = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
    pg_name, prom_name, pg_port, prom_port = f"cairn-obslock-pg-{tag}", f"cairn-obslock-prom-{tag}", _port(), _port()
    try:
        subprocess.run(["docker", "run", "-d", "--rm", "--name", pg_name, "--env-file", str(d / "pg.env"),
                        "-p", f"127.0.0.1:{pg_port}:5432", PG], check=True, capture_output=True, timeout=300)
        subprocess.run(["docker", "run", "-d", "--rm", "--name", prom_name, "-p", f"127.0.0.1:{prom_port}:9090", PROM],
                       check=True, capture_output=True, timeout=300)
        end = time.time() + 90
        while subprocess.run(["docker", "exec", pg_name, "pg_isready", "-U", "postgres", "-h", "127.0.0.1"],
                             capture_output=True).returncode:
            assert time.time() < end, "PostgreSQL did not start"
            time.sleep(1)
        prom = f"http://127.0.0.1:{prom_port}"
        while True:
            try:
                if httpx.get(prom + "/-/ready", timeout=5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.time() < end, "Prometheus did not start"
            time.sleep(1)
        time.sleep(1)
        yield f"postgresql+psycopg://postgres:{password}@127.0.0.1:{pg_port}/cairn", prom
    finally:
        subprocess.run(["docker", "rm", "-f", pg_name, prom_name], capture_output=True, timeout=60)


def test_two_processes_evaluating_at_once_fire_a_new_alert_once(stack):
    from sqlalchemy import create_engine

    from cairn.common.audit import AuditLog
    from cairn.common.db import init_db
    from cairn.observability.backends import Backends
    from cairn.observability.service import ObservabilityService

    url, prom = stack
    engines = [create_engine(url, future=True, pool_pre_ping=True) for _ in range(2)]   # two processes' pools
    init_db(engines[0])
    env = {"CAIRN_OBS_PROMETHEUS_URL": prom}
    notifier, api = (ObservabilityService(e, Backends(env), None, AuditLog(e)) for e in engines)
    notifier.create("alert_rule", ADMIN, {"id": "always", "name": "Always", "expr": "vector(1)", "severity": "critical"})
    assert notifier.test_rule({"expr": "vector(1)"})["count"] == 1          # the real Prometheus answers it

    api_done = threading.Event()
    fire = notifier._fire

    def slow_fire(a, spec, now):              # the notifier has the new alert and is about to fire it...
        api_done.wait(5)                      # ...while the API replica evaluates too, if it may
        return fire(a, spec, now)

    notifier._fire = slow_fire
    now = int(time.time() * 1000)
    results = {}

    def run(name, svc):
        try:
            results[name] = svc.evaluate(now)
        finally:
            if name == "api":
                api_done.set()

    t1 = threading.Thread(target=run, args=("notifier", notifier))
    t1.start()
    time.sleep(1.0)
    t2 = threading.Thread(target=run, args=("api", api))
    t2.start()
    t1.join(60)
    t2.join(60)
    assert results["notifier"]["fired"] + results["api"]["fired"] == 1
    fired = [e for e in AuditLog(engines[0]).entries(category="observability")
             if e["action"] == "observability.alert_fired"]
    assert len(api.alerts()) == 1 and len(fired) == 1 and len(api.incidents()) == 1
    for e in engines:
        e.dispose()
