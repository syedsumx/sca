"""Disaster recovery drill: a region is lost, the standby serves the lakehouse, with measured RPO and RTO.

Region A: Cairn Store (replicating its bucket to region B's store), PostgreSQL (streaming to region B's
standby) and Cairn Catalog on them. Writers commit to A. Region B's store is frozen for a moment (its
replication falls behind, as over a slow link), then region A is killed outright. Failover: B's PostgreSQL
is promoted, a catalog is started on it and B's store, and ``cairn dr reconcile`` moves any table whose
newest files never reached B back to its newest commit whose files did. The drill records:

* RPO: the acknowledged commits the standby does not have, and how old the newest one it has was;
* RTO: from the loss of region A to every table readable on B.

Needs docker (postgres:16-alpine) and the store and catalog binaries."""

import json
import os
import shutil
import signal
import socket
import subprocess
import time
import uuid

import pytest

pa = pytest.importorskip("pyarrow")
pytest.importorskip("pyiceberg")

STORE_KEY = ("dr-key", "dr-secret-" + "s" * 24)
TOKEN = "dr-catalog-token-" + "t" * 20
SCHEMA = pa.schema([("batch", pa.string()), ("n", pa.int64())])


def _ready():
    if not (os.environ.get("CAIRN_STORE_BIN") and os.environ.get("CAIRN_CATALOG_BIN") and shutil.which("docker")):
        return False
    return subprocess.run(["docker", "image", "inspect", "postgres:16-alpine"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, postgres:16-alpine and the store and catalog binaries")


def port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait(p, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", p), 0.3).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"nothing on {p}")


def pg_ready(name, p, timeout=90):
    end = time.time() + timeout
    while subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-p", str(p)], capture_output=True).returncode:
        assert time.time() < end, f"{name} did not start"
        time.sleep(0.5)


@pytest.fixture
def regions(tmp_path):
    procs, containers = [], []
    tag = uuid.uuid4().hex[:6]
    try:
        # PostgreSQL: A, and B streaming from it.
        pa_port, pb_port = port(), port()
        a_db, b_db = f"dr-pg-a-{tag}", f"dr-pg-b-{tag}"
        subprocess.run(["docker", "run", "-d", "--rm", "--name", a_db, "--network", "host", "-e", "POSTGRES_PASSWORD=pgpass",
                        "postgres:16-alpine", "-p", str(pa_port), "-c", "wal_level=replica", "-c", "max_wal_senders=5",
                        "-c", "listen_addresses=127.0.0.1"], check=True, capture_output=True)
        containers.append(a_db)
        pg_ready(a_db, pa_port)
        time.sleep(1)
        subprocess.run(["docker", "exec", a_db, "psql", "-h", "127.0.0.1", "-p", str(pa_port), "-U", "postgres", "-c",
                        "CREATE DATABASE lake"], check=True, capture_output=True)
        subprocess.run(["docker", "run", "-d", "--rm", "--name", b_db, "--network", "host", "--user", "postgres",
                        "--entrypoint", "sh", "postgres:16-alpine", "-c",
                        f"pg_basebackup -h 127.0.0.1 -p {pa_port} -U postgres -D /tmp/pgb -R -X stream --checkpoint=fast && "
                        "chmod 700 /tmp/pgb && "
                        f"exec postgres -D /tmp/pgb -p {pb_port} -c listen_addresses=127.0.0.1"], check=True, capture_output=True)
        containers.append(b_db)
        pg_ready(b_db, pb_port)
        # Stores: B, then A replicating its bucket to B.
        sa, sb = port(), port()

        def store(name, p, extra):
            pr = subprocess.Popen([os.environ["CAIRN_STORE_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
                **os.environ, "CAIRN_STORE_ACCESS_KEY": STORE_KEY[0], "CAIRN_STORE_SECRET_KEY": STORE_KEY[1],
                "CAIRN_STORE_DATA": str(tmp_path / name), "CAIRN_STORE_ADDR": f"127.0.0.1:{p}", "CAIRN_STORE_BUCKETS": "lakehouse",
                "CAIRN_STORE_LOG_LEVEL": "warn", **extra})
            procs.append(pr)
            wait(p)
            return pr
        store_b = store("store-b", sb, {})
        (tmp_path / "b-secret").write_text(STORE_KEY[1])
        (tmp_path / "targets.json").write_text(json.dumps([{"id": "dr", "endpoint": f"http://127.0.0.1:{sb}",
                                                             "access_key": STORE_KEY[0], "secret_key_file": str(tmp_path / "b-secret"),
                                                             "region": "us-east-1"}]))
        store_a = store("store-a", sa, {"CAIRN_STORE_REPLICATION_TARGETS_FILE": str(tmp_path / "targets.json")})
        import boto3

        def s3(p):
            return boto3.client("s3", endpoint_url=f"http://127.0.0.1:{p}", aws_access_key_id=STORE_KEY[0],
                                aws_secret_access_key=STORE_KEY[1], region_name="us-east-1")
        for p in (sb, sa):
            s3(p).put_bucket_versioning(Bucket="lakehouse", VersioningConfiguration={"Status": "Enabled"})
        s3(sa).put_bucket_replication(Bucket="lakehouse", ReplicationConfiguration={"Role": "arn:cairn:store:::dr", "Rules": [{
            "ID": "all", "Priority": 1, "Status": "Enabled", "Filter": {"Prefix": ""},
            "DeleteMarkerReplication": {"Status": "Enabled"}, "Destination": {"Bucket": "arn:aws:s3:::lakehouse"}}]})

        def catalog(name, p, pg_port, store_port):
            pr = subprocess.Popen([os.environ["CAIRN_CATALOG_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
                **os.environ, "CAIRN_CATALOG_ADDR": f"127.0.0.1:{p}", "CAIRN_CATALOG_DATA": str(tmp_path / name),
                "CAIRN_CATALOG_TOKEN": TOKEN, "CAIRN_CATALOG_S3_ENDPOINT": f"http://127.0.0.1:{store_port}",
                "CAIRN_CATALOG_S3_ACCESS_KEY": STORE_KEY[0], "CAIRN_CATALOG_S3_SECRET_KEY": STORE_KEY[1],
                "CAIRN_CATALOG_WAREHOUSE": "s3://lakehouse/warehouse", "CAIRN_CATALOG_LOG_LEVEL": "warn",
                "CAIRN_CATALOG_DATABASE_URL": f"postgresql://postgres:pgpass@127.0.0.1:{pg_port}/lake?sslmode=disable"})
            procs.append(pr)
            return pr
        ca = port()
        cat_a = catalog("catalog-a", ca, pa_port, sa)
        wait(ca)
        yield {"a": {"store": store_a, "store_port": sa, "catalog": cat_a, "catalog_port": ca, "pg": a_db, "pg_port": pa_port},
               "b": {"store": store_b, "store_port": sb, "pg": b_db, "pg_port": pb_port}, "catalog": catalog, "s3": s3}
    finally:
        for pr in procs:
            if pr.poll() is None:
                try:
                    pr.send_signal(signal.SIGCONT)
                except ProcessLookupError:
                    pass
                pr.kill()
        for c in containers:
            subprocess.run(["docker", "rm", "-f", c], capture_output=True)


def rest(p, store_port):
    from pyiceberg.catalog.rest import RestCatalog

    return RestCatalog(f"dr{p}", uri=f"http://127.0.0.1:{p}", token=TOKEN, **{
        "s3.endpoint": f"http://127.0.0.1:{store_port}", "s3.access-key-id": STORE_KEY[0],
        "s3.secret-access-key": STORE_KEY[1], "s3.region": "us-east-1", "s3.path-style-access": "true"})


def test_a_region_is_lost_and_the_standby_serves_the_lakehouse(regions):
    from cairn.pipelines.dr import Reconciler

    a, b = regions["a"], regions["b"]
    cat = rest(a["catalog_port"], a["store_port"])
    cat.create_namespace("ops")
    events = cat.create_table("ops.events", schema=SCHEMA)
    steady = cat.create_table("ops.steady", schema=SCHEMA)
    steady.append(pa.table({"batch": ["s"], "n": [1]}, schema=SCHEMA))
    acked: list[tuple[str, float]] = []

    def commit(i):
        events.append(pa.table({"batch": [f"b{i}"] * 2, "n": [i, i]}, schema=SCHEMA))
        acked.append((f"b{i}", time.time()))
    for i in range(6):                                 # replicated as they go
        commit(i)
    time.sleep(3)
    # The link to B slows down: B's store stops taking replicas (the database still streams).
    os.kill(b["store"].pid, signal.SIGSTOP)
    for i in range(6, 10):
        commit(i)
    time.sleep(1)
    # Region A is lost.
    lost_at = time.time()
    a["store"].kill()
    a["catalog"].kill()
    subprocess.run(["docker", "kill", a["pg"]], capture_output=True)
    os.kill(b["store"].pid, signal.SIGCONT)
    # Failover: promote B's database, start a catalog on B, reconcile.
    subprocess.run(["docker", "exec", b["pg"], "pg_ctl", "promote", "-D", "/tmp/pgb"], check=True, capture_output=True)
    end = time.time() + 60
    while subprocess.run(["docker", "exec", b["pg"], "psql", "-h", "127.0.0.1", "-p", str(b["pg_port"]), "-U", "postgres", "-tAc",
                          "SELECT pg_is_in_recovery()"], capture_output=True, text=True).stdout.strip() != "f":
        assert time.time() < end, "the standby was not promoted"
        time.sleep(0.3)
    cb = port()
    regions["catalog"]("catalog-b", cb, b["pg_port"], b["store_port"])
    wait(cb)
    standby = rest(cb, b["store_port"])
    # Report first (what `cairn dr reconcile` prints without --apply): something to repair, nothing changed.
    import argparse

    from cairn.pipelines import dr
    args = argparse.Namespace(catalog=f"http://127.0.0.1:{cb}", token=TOKEN, store=f"http://127.0.0.1:{b['store_port']}",
                              access_key=STORE_KEY[0], secret_key=STORE_KEY[1], secret_key_file="", region="us-east-1",
                              apply=False)
    assert dr.main(args) == 1
    reports = {r.table: r for r in Reconciler(standby, regions["s3"](b["store_port"])).reconcile(apply=True)}
    assert reports["ops.steady"].action == "none" and reports["ops.steady"].readable
    ev = reports["ops.events"]
    assert ev.readable and ev.action in ("none", "rolled-back"), ev
    got = standby.load_table("ops.events").scan().to_arrow().column("batch").to_pylist()
    recovered_at = time.time()
    have = sorted(set(got), key=lambda x: int(x[1:]))
    # What the standby serves is a prefix of what was acknowledged, each commit once, all of it readable.
    assert have == [x for x, _ in acked][:len(have)] and all(got.count(x) == 2 for x in have)
    assert len(have) >= 6                              # everything replicated before the link slowed is there
    missing = [x for x, _ in acked][len(have):]
    newest = dict(acked)[have[-1]]
    rpo_commits, rpo_seconds, rto = len(missing), round(lost_at - newest, 1), round(recovered_at - lost_at, 1)
    assert ev.lost_commits == (rpo_commits if ev.action == "rolled-back" else 0) or ev.action == "none"
    print(f"\nDR drill: RPO {rpo_commits} of {len(acked)} commits ({rpo_seconds} s), RTO {rto} s; events {ev.action}"
          f" ({ev.lost_commits} commits rolled back), steady untouched")
    assert rto < 120
    # The standby takes writes.
    standby.load_table("ops.events").append(pa.table({"batch": ["after"], "n": [0]}, schema=SCHEMA))
    assert "after" in standby.load_table("ops.events").scan().to_arrow().column("batch").to_pylist()
