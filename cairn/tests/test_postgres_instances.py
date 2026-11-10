"""Managed Postgres (L140) on the Docker runtime, end to end against real components: PostgreSQL 16 containers made
by Cairn, the backup sidecar, and a real Cairn Store binary holding WAL and base backups.

- an instance is made through the API (only with CREATE_DATABASE_INSTANCE), answers over TLS, archives its WAL;
- databases and short-lived logins (L88) live on it; an allow-listed parameter is set (one that needs a restart
  restarts it), a parameter outside the list is refused;
- a base backup lands in the store, and its restore check passes;
- a point-in-time restore into a new instance has the rows written before the time and not after, its database
  renamed and registered, and none of the source's logins;
- a branch of a database from a backup has the data as of that backup, on an instance of its own;
- health, metrics and slow queries are served; resize and a recreate (a new image) keep the data;
- retention deletes an expired base backup from the store and keeps the newest;
- the server Cairn was given before (CAIRN_DATABASES_ADMIN_URL) is the instance "default", read-only.

Needs Docker (with postgres:16-alpine and python:3.12-slim) and CAIRN_STORE_BIN."""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid

import pytest
from fastapi.testclient import TestClient

BIN = os.environ.get("CAIRN_STORE_BIN", "")
DOCKER = shutil.which("docker")


def _images_present() -> bool:
    if not DOCKER:
        return False
    r = subprocess.run(["docker", "image", "inspect", "postgres:16-alpine", "python:3.12-slim"], capture_output=True)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not (BIN and _images_present()),
                                reason="needs CAIRN_STORE_BIN and Docker with postgres:16-alpine and python:3.12-slim")
ANA, BOB, CAL = "urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:cal"
KEY = ("cairn-pg-backups", "pg-backups-secret-" + "b" * 24)


def bridge_ip() -> str:
    out = subprocess.run(["docker", "network", "inspect", "bridge", "-f", "{{range .IPAM.Config}}{{.Gateway}}{{end}}"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return out or "172.17.0.1"


def free_port(host: str) -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def store():
    """Cairn Store on the Docker bridge's address: this host and the instances' sidecars both reach it there."""
    ip = bridge_ip()
    d, p = tempfile.mkdtemp(), free_port(ip)
    proc = subprocess.Popen([BIN], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **os.environ, "CAIRN_STORE_ACCESS_KEY": KEY[0], "CAIRN_STORE_SECRET_KEY": KEY[1], "CAIRN_STORE_DATA": f"{d}/s",
        "CAIRN_STORE_ADDR": f"{ip}:{p}", "CAIRN_STORE_FSYNC": "false", "CAIRN_STORE_META": ""})
    end = time.time() + 30
    while True:
        try:
            socket.create_connection((ip, p), 1).close()
            break
        except OSError:
            assert time.time() < end, "the store did not start"
            time.sleep(0.2)
    yield {"url": f"http://{ip}:{p}", "ip": ip}
    proc.terminate()
    proc.wait(15)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module")
def default_server():
    """A plain PostgreSQL the operator runs: the instance "default"."""
    name = "cairn-test-default-" + uuid.uuid4().hex[:6]
    pw = "default-" + uuid.uuid4().hex
    with tempfile.NamedTemporaryFile("w", delete=False) as f:      # the password through a file, not a command line
        f.write(f"POSTGRES_PASSWORD={pw}\n")
    subprocess.run(["docker", "run", "-d", "--name", name, "--env-file", f.name, "-p", "127.0.0.1::5432",
                    "postgres:16-alpine", "postgres", "-c", "shared_preload_libraries=pg_stat_statements"],
                   check=True, capture_output=True)
    os.unlink(f.name)
    port = int(subprocess.run(["docker", "port", name, "5432"], capture_output=True, text=True,
                              check=True).stdout.strip().split(":")[-1])
    import psycopg

    end = time.time() + 60
    while True:
        try:
            psycopg.connect(host="127.0.0.1", port=port, user="postgres", password=pw, dbname="postgres",
                            connect_timeout=2).close()
            break
        except psycopg.OperationalError:
            assert time.time() < end, "the default server did not start"
            time.sleep(0.5)
    time.sleep(2)                                                  # the image restarts once after initdb
    yield {"url": f"postgresql://postgres:{pw}@127.0.0.1:{port}/postgres?sslmode=disable", "port": port}
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture(scope="module")
def cairn(store, default_server, tmp_path_factory):
    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.services import build_services

    tmp = tmp_path_factory.mktemp("pg")
    files = {"CAIRN_PG_INSTANCE_KEY_FILE": base64.b64encode(os.urandom(32)).decode(),
             "CAIRN_PG_BACKUP_ACCESS_KEY_FILE": KEY[0], "CAIRN_PG_BACKUP_SECRET_KEY_FILE": KEY[1],
             "CAIRN_DATABASES_ADMIN_URL_FILE": default_server["url"]}
    env = {"CAIRN_PG_RUNTIME": "docker", "CAIRN_PG_DOCKER_BIND": "127.0.0.1", "CAIRN_PG_DOCKER_HOST": "127.0.0.1",
           "CAIRN_PG_BACKUP_STORE": json.dumps({"endpoint": store["url"], "bucket": "pg-backups"}),
           "CAIRN_DATABASES_HOST": "127.0.0.1", "CAIRN_DATABASES_PORT": str(default_server["port"]),
           "CAIRN_DATABASES_SSLMODE": "disable"}
    for k, v in files.items():                                      # secrets as files, as a Kubernetes Secret mounts them
        (tmp / k).write_text(v)
        env[k] = str(tmp / k)
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        s = build_services(Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-secret",
                                    dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")}),
                           graph=InMemoryGraph(), with_ai=False, with_gateway=False)
    finally:
        for k, v in old.items():
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
    assert s.pg_instances is not None and s.databases is not None
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="ana", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ANA}),
        Policy(id="bob", effect="allow", actions=["VIEW_DATABASE_INSTANCE", "VIEW_DATABASE", "CREATE_DATABASE"],
               condition={"attr": "subject.urn", "op": "eq", "value": BOB})]))
    app = create_api_app(s)
    made: list[str] = []
    yield {"s": s, "ana": TestClient(app, headers={"X-Cairn-Actor": ANA}),
           "bob": TestClient(app, headers={"X-Cairn-Actor": BOB}), "made": made, "store": store}
    for t in list(s.pg_instances._threads.values()):       # scheduled backups and checks still running
        t.join(600)
    for db in [d["name"] for d in s.databases.list(ANA)]:
        try:
            s.databases.drop(ANA, db)
        except Exception:  # noqa: BLE001 - its instance is gone already
            pass
    for name in reversed(made):
        try:
            s.pg_instances.delete(ANA, name)
        except Exception:  # noqa: BLE001
            subprocess.run(f"docker rm -f cairn-pg-{name} cairn-pg-{name}-agent", shell=True, capture_output=True)
            subprocess.run(f"docker volume rm -f cairn-pg-{name}-data cairn-pg-{name}-spool", shell=True,
                           capture_output=True)


def connect(creds, **kw):
    import psycopg

    return psycopg.connect(host=creds["host"], port=creds["port"], dbname=kw.get("dbname", creds["database"]),
                           user=creds["user"], password=creds["password"], sslmode=creds["sslmode"], connect_timeout=5,
                           autocommit=True)


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def test_an_instance_backed_up_restored_to_a_point_in_time_and_branched(cairn):
    import psycopg

    ana, bob, s = cairn["ana"], cairn["bob"], cairn["s"]
    # Only someone with CREATE_DATABASE_INSTANCE makes one; sizes and versions are checked.
    assert bob.post("/v1/databases/instances", json={"name": "orders-pg"}).status_code == 403
    assert ana.post("/v1/databases/instances", json={"name": "orders-pg", "replicas": 1}).status_code == 400   # HA: k8s
    assert ana.post("/v1/databases/instances", json={"name": "orders-pg", "version": "9"}).status_code == 400
    assert ana.post("/v1/databases/instances", json={"name": "Bad_Name"}).status_code == 400
    r = ana.post("/v1/databases/instances", json={"name": "orders-pg", "cpu": 1, "memory_mb": 512, "storage_gb": 5})
    assert r.status_code == 201, r.text
    cairn["made"].append("orders-pg")
    inst = r.json()
    assert inst["state"] == "ready" and inst["runtime"] == "docker" and inst["version"] == "16" and inst["port"]
    names = [i["name"] for i in ana.get("/v1/databases/instances").json()["instances"]]
    assert names == ["default", "orders-pg"]
    # The container runs with its limits; the superuser's password is nowhere in its configuration.
    info = json.loads(subprocess.run(["docker", "inspect", "cairn-pg-orders-pg"], capture_output=True, text=True,
                                     check=True).stdout)[0]
    assert info["HostConfig"]["Memory"] == 512 * 1024 * 1024
    pw = s.pg_instances.password("orders-pg", s.pg_instances._row("orders-pg").uid)
    assert pw not in json.dumps(info)
    agent = json.loads(subprocess.run(["docker", "inspect", "cairn-pg-orders-pg-agent"], capture_output=True, text=True,
                                      check=True).stdout)[0]
    assert KEY[1] not in json.dumps(agent)                          # the store's key is a file in it, not config

    st = bob.get("/v1/databases/instances/orders-pg/status").json()
    assert st["health"] == "healthy", st
    assert st["wal_archiving"]["enabled"] and st["connections"]["max"] == 100

    # A database on it, a write login over TLS.
    r = ana.post("/v1/databases", json={"name": "shop", "instance": "orders-pg"})
    assert r.status_code == 201, r.text
    assert r.json()["instance"] == "orders-pg" and r.json()["port"] == inst["port"]
    w = ana.post("/v1/databases/shop/credentials", json={"access": "write", "ttl_minutes": 30}).json()
    assert w["sslmode"] == "require"
    with connect(w) as c:
        assert c.execute("SHOW ssl").fetchone()[0] == "on"
        assert c.info.pgconn.ssl_in_use
        c.execute("CREATE TABLE orders (id int PRIMARY KEY, note text)")
        c.execute("INSERT INTO orders VALUES (1, 'first'), (2, 'second')")

    # Parameters: allow-listed and checked; one that needs a restart restarts it (no maintenance window).
    assert ana.put("/v1/databases/instances/orders-pg/parameters/fsync", json={"value": "off"}).status_code == 400
    assert ana.put("/v1/databases/instances/orders-pg/parameters/work_mem", json={"value": "lots"}).status_code == 400
    assert bob.put("/v1/databases/instances/orders-pg/parameters/work_mem", json={"value": "8MB"}).status_code == 403
    r = ana.put("/v1/databases/instances/orders-pg/parameters/work_mem", json={"value": "8MB"})
    assert r.status_code == 200 and r.json()["pending_restart"] is False, r.text
    r = ana.put("/v1/databases/instances/orders-pg/parameters/max_connections", json={"value": 120})
    assert r.status_code == 200 and r.json()["pending_restart"] is True, r.text
    with connect(w) as c:
        assert c.execute("SHOW work_mem").fetchone()[0] == "8MB"
        assert c.execute("SHOW max_connections").fetchone()[0] == "120"
    got = ana.get("/v1/databases/instances/orders-pg").json()
    assert got["parameters"] == [{"name": "max_connections", "value": "120"}, {"name": "work_mem", "value": "8MB"}]
    assert got["pending"] == []

    # A schedule, and a base backup now: in the store, and its restore check passes.
    r = ana.put("/v1/databases/instances/orders-pg/backup-schedules/daily",
                json={"every_hours": 24, "retention_days": 7, "verify": True})
    assert r.status_code == 200, r.text
    assert bob.post("/v1/databases/instances/orders-pg/backups").status_code == 403
    r = ana.post("/v1/databases/instances/orders-pg/backups")
    assert r.status_code == 201, r.text
    b1 = r.json()
    assert b1["state"] == "completed" and b1["bytes"] > 0 and b1["start_lsn"] and b1["timeline"] == 1, b1
    keys = s.pg_instances.store.list("orders-pg/")
    assert any(k.endswith(f"/base/{b1['id']}/base.tar.gz") for k in keys), keys
    assert any("/wal/" in k for k in keys), keys                     # continuous archiving
    r = ana.post(f"/v1/databases/instances/orders-pg/backups/{b1['id']}/verify")
    assert r.status_code == 200 and r.json()["verified"] == "passed", r.text
    assert subprocess.run(["docker", "ps", "-aq", "--filter", "name=cairn-pg-verify-"], capture_output=True,
                          text=True).stdout.strip() == ""              # the throwaway instance is gone

    # Rows after the backup; a point in time; rows after it.
    with connect(w) as c:
        c.execute("INSERT INTO orders VALUES (3, 'before the point')")
    time.sleep(1.5)
    point = iso_now()
    time.sleep(1.5)
    with connect(w) as c:
        c.execute("INSERT INTO orders VALUES (4, 'after the point')")
        c.execute("UPDATE orders SET note = 'changed after' WHERE id = 1")

    # Point-in-time restore into a new instance: the database renamed and registered; data as of the point.
    assert ana.post("/v1/databases/instances/orders-pg/restore",
                    json={"name": "orders-pitr", "target_time": "2999-01-01T00:00:00Z"}).status_code == 400
    r = ana.post("/v1/databases/instances/orders-pg/restore",
                 json={"name": "orders-pitr", "target_time": point, "databases": {"shop": "shop_pitr"}})
    assert r.status_code == 201, r.text
    cairn["made"].append("orders-pitr")
    pitr = r.json()
    assert pitr["state"] == "ready" and pitr["restored_from"]["instance"] == "orders-pg" and pitr["port"] != inst["port"]
    rw = ana.post("/v1/databases/shop_pitr/credentials", json={"access": "read", "ttl_minutes": 30}).json()
    assert rw["instance"] == "orders-pitr"
    with connect(rw) as c:
        rows = c.execute("SELECT id, note FROM orders ORDER BY id").fetchall()
    assert rows == [(1, "first"), (2, "second"), (3, "before the point")], rows
    # The source's logins do not open the restored copy; its superuser password is its own.
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(host="127.0.0.1", port=pitr["port"], dbname="shop_pitr", user=w["user"], password=w["password"],
                        sslmode="require", connect_timeout=5).close()
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(host="127.0.0.1", port=pitr["port"], dbname="postgres", user="postgres", password=pw,
                        sslmode="require", connect_timeout=5).close()
    # The source is untouched.
    with connect(w) as c:
        assert c.execute("SELECT count(*) FROM orders").fetchone()[0] == 4
    # The restored instance archives on its own (a new timeline, its own place in the store).
    st = s.pg_instances.status(ANA, "orders-pitr")
    assert st["health"] == "healthy" and st["last_backup"]["timeline"] == 2, st

    # A branch of the database from the backup: the data as of the backup, on an instance of its own.
    r = ana.post("/v1/databases/shop/branches", json={"name": "shop_then", "backup": b1["id"]})
    assert r.status_code == 201, r.text
    cairn["made"].append("shop-then")
    assert r.json()["instance"] == "shop-then" and r.json()["branch_of"] == "shop"
    bw = ana.post("/v1/databases/shop_then/credentials", json={"access": "write", "ttl_minutes": 30}).json()
    with connect(bw) as c:
        assert c.execute("SELECT id, note FROM orders ORDER BY id").fetchall() == [(1, "first"), (2, "second")]
        c.execute("INSERT INTO orders VALUES (9, 'only in the branch')")
    with connect(w) as c:
        assert c.execute("SELECT count(*) FROM orders WHERE id = 9").fetchone()[0] == 0

    # Metrics and slow queries for dashboards and the SRE agent; statement text only for managers.
    m = bob.get("/v1/databases/instances/orders-pg/metrics")
    assert m.status_code == 200, m.text
    shop = [d for d in m.json()["databases"] if d["database"] == "shop"][0]
    assert shop["commits"] > 0 and shop["size_bytes"] > 0 and m.json()["wal_archiving"]["archived_count"] > 0
    assert bob.get("/v1/databases/instances/orders-pg/slow-queries").status_code == 403
    sq = ana.get("/v1/databases/instances/orders-pg/slow-queries", params={"limit": 50})
    assert sq.status_code == 200, sq.text
    assert any("INSERT INTO orders" in q["query"] for q in sq.json()["queries"]), sq.json()

    # Resize in place; a new image (a minor upgrade) restarts it on the same data and port.
    r = ana.patch("/v1/databases/instances/orders-pg", json={"memory_mb": 768})
    assert r.status_code == 200, r.text
    info = json.loads(subprocess.run(["docker", "inspect", "cairn-pg-orders-pg"], capture_output=True, text=True,
                                     check=True).stdout)[0]
    assert info["HostConfig"]["Memory"] == 768 * 1024 * 1024
    assert ana.patch("/v1/databases/instances/orders-pg", json={"version": "17"}).status_code == 400
    started = info["State"]["StartedAt"]
    r = ana.patch("/v1/databases/instances/orders-pg", json={"image": "postgres:16-alpine"})
    assert r.status_code == 200 and r.json()["pending"] == [], r.text
    info = json.loads(subprocess.run(["docker", "inspect", "cairn-pg-orders-pg"], capture_output=True, text=True,
                                     check=True).stdout)[0]
    assert info["State"]["StartedAt"] != started and r.json()["port"] == inst["port"]
    with connect(w) as c:
        assert c.execute("SELECT count(*) FROM orders").fetchone()[0] == 4

    # An instance holding databases is not deleted from under them.
    assert ana.delete("/v1/databases/instances/orders-pitr").status_code == 409
    assert ana.delete("/v1/databases/shop_pitr").status_code == 204
    assert ana.delete("/v1/databases/instances/orders-pitr").status_code == 204
    cairn["made"].remove("orders-pitr")
    assert subprocess.run(["docker", "ps", "-aq", "--filter", "name=cairn-pg-orders-pitr"], capture_output=True,
                          text=True).stdout.strip() == ""


def test_retention_deletes_expired_backups_and_keeps_the_newest(cairn):
    s, ana = cairn["s"], cairn["ana"]
    inst = s.pg_instances
    r = ana.post("/v1/databases/instances", json={"name": "keep-pg", "memory_mb": 512})
    assert r.status_code == 201, r.text
    cairn["made"].append("keep-pg")
    ana.put("/v1/databases/instances/keep-pg/backup-schedules/hourly", json={"every_hours": 1, "retention_days": 1})
    # The scheduler takes the first backup (it is due: none yet).
    assert inst.run_due() >= 1
    inst._threads["keep-pg"].join(300)
    first = inst.backups(ANA, "keep-pg")
    assert len(first) == 1 and first[0]["schedule"] == "hourly" and first[0]["state"] == "completed", first
    real = inst.clock
    try:
        inst.clock = lambda: real() + 2 * 3600 * 1000            # two hours on: due again
        assert inst.run_due() >= 1
        inst._threads["keep-pg"].join(300)
        assert len(inst.backups(ANA, "keep-pg")) == 2
        inst.clock = lambda: real() + 3 * 86400 * 1000           # three days on: the older one expires
        inst.run_due()
        t = inst._threads.get("keep-pg")
        if t is not None:
            t.join(300)
        inst.run_due()                                           # retention runs once nothing else is due
        kept = inst.backups(ANA, "keep-pg")
    finally:
        inst.clock = real
    ids = [b["id"] for b in kept]
    assert first[0]["id"] not in ids and len(ids) >= 1, kept
    keys = inst.store.list("keep-pg/")
    assert not any(f"/base/{first[0]['id']}/" in k for k in keys)
    assert any(f"/base/{ids[-1]}/" in k for k in keys)


def test_the_server_given_before_is_the_default_instance(cairn):
    ana, bob = cairn["ana"], cairn["bob"]
    d = ana.get("/v1/databases/instances/default").json()
    assert d["runtime"] == "external"
    # L88 keeps working: a database without an instance goes on it.
    r = ana.post("/v1/databases", json={"name": "legacy_app"})
    assert r.status_code == 201 and r.json()["instance"] == "default", r.text
    st = bob.get("/v1/databases/instances/default/status").json()
    assert st["health"] == "healthy" and st["wal_archiving"]["enabled"] is False, st
    assert bob.get("/v1/databases/instances/default/metrics").status_code == 200
    # Its lifecycle is not Cairn's.
    for call in (lambda: ana.post("/v1/databases/instances/default/backups"),
                 lambda: ana.patch("/v1/databases/instances/default", json={"cpu": 2}),
                 lambda: ana.delete("/v1/databases/instances/default")):
        assert call().status_code == 409
    assert ana.delete("/v1/databases/legacy_app").status_code == 204
