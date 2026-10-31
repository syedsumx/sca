"""Backups to an S3-compatible sink (here another Cairn Store): consistent while writes go on, incremental,
encrypted objects stay encrypted, restorable into a new data directory, deletable with pruning of blobs no
other backup needs, and on a schedule with retention."""

import base64
import json
import os
import subprocess
import threading
import time

import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from conftest import ADMIN, REGION, Store, _free_port

MASTER = base64.b64encode(b"s" * 32).decode()


def admin(st, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = AWSRequest(method=method, url=f"{st.endpoint}/-/admin/v1/{path}", data=data)
    S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
    r = requests.request(method, req.url, data=data, headers=dict(req.headers), timeout=60)
    return r.status_code, (r.json() if r.content else None)


@pytest.fixture
def setup(custom_store, tmp_path):
    sink = custom_store({})
    sink.client().create_bucket(Bucket="offsite")
    sinks = tmp_path / "sinks.json"
    sinks.write_text(json.dumps([{"id": "offsite", "endpoint": sink.endpoint, "bucket": "offsite", "prefix": "cairn",
                                  "access_key": ADMIN[0], "secret_key": ADMIN[1]}]))

    def source(**env):
        return custom_store({"CAIRN_STORE_BACKUP_SINKS_FILE": str(sinks), "CAIRN_STORE_MASTER_KEY": MASTER, **env})

    return sink, sinks, source


def sink_keys(sink, prefix):
    s3 = sink.client()
    return [o["Key"] for p in s3.get_paginator("list_objects_v2").paginate(Bucket="offsite", Prefix=prefix) for o in p.get("Contents", [])]


def test_backup_restore_and_incremental(setup, tmp_path, binary):
    sink, sinks, source = setup
    src = source()
    s3 = src.client()
    s3.create_bucket(Bucket="data")
    s3.put_bucket_versioning(Bucket="data", VersioningConfiguration={"Status": "Enabled"})
    big = {f"big{i}": os.urandom(200_000) for i in range(3)}  # blob files
    small = {f"small{i}": f"small object {i} ".encode() * 20 for i in range(30)}  # packed
    for k, b in {**big, **small}.items():
        s3.put_object(Bucket="data", Key=k, Body=b)
    s3.put_object(Bucket="data", Key="small0", Body=b"second version")
    stop = threading.Event()

    def writer():  # keeps writing while the backup runs
        i = 0
        while not stop.is_set():
            s3.put_object(Bucket="data", Key=f"live{i % 3}", Body=os.urandom(1000))
            i += 1

    t = threading.Thread(target=writer)
    t.start()
    code, first = admin(src, "POST", "backups", {"name": "first", "sink": "offsite"})
    stop.set()
    t.join()
    assert code == 201 and first["objects"] >= 33 and first["blobs_uploaded"] > 0, first
    # Encrypted objects are uploaded exactly as stored: the sink holds no plaintext.
    for k in sink_keys(sink, "cairn/blobs/"):
        raw = sink.client().get_object(Bucket="offsite", Key=k)["Body"].read()
        assert all(b[:5000] not in raw for b in big.values())
    # The second backup reuses what the sink already has.
    s3.put_object(Bucket="data", Key="after-first", Body=os.urandom(150_000))
    code, second = admin(src, "POST", "backups", {"name": "second", "sink": "offsite"})
    assert code == 201 and second["blobs_uploaded"] < first["blobs_uploaded"] + 2, (first, second)
    assert [b["name"] for b in admin(src, "GET", "backups?sink=offsite")[1]["backups"]] == ["first", "second"]
    assert admin(src, "POST", "backups", {"name": "second", "sink": "offsite"})[0] == 409
    # Restore into a new data directory; a store started on it has the data as of the snapshot.
    restored = tmp_path / "restored"
    env = {**os.environ, "CAIRN_STORE_BACKUP_SINKS_FILE": str(sinks)}
    restore = [binary, "restore-backup", "offsite"]
    out = subprocess.run([*restore, "second", str(restored)], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["objects"] == second["objects"]
    plain_env = {**os.environ, "CAIRN_STORE_META": ""}
    fsck = subprocess.run([binary, "fsck", "-data", str(restored)], capture_output=True, timeout=60, env=plain_env)
    assert fsck.returncode == 0
    rst = Store(binary, str(restored), _free_port(), env={"CAIRN_STORE_META": "", "CAIRN_STORE_MASTER_KEY": MASTER}).start()
    try:
        r3 = rst.client()
        for k, b in {**big, **{k: v for k, v in small.items() if k != "small0"}}.items():
            assert r3.get_object(Bucket="data", Key=k)["Body"].read() == b
        assert r3.get_object(Bucket="data", Key="small0")["Body"].read() == b"second version"
        assert len(r3.list_object_versions(Bucket="data", Prefix="small0")["Versions"]) == 2
        assert r3.head_object(Bucket="data", Key="after-first")["ContentLength"] == 150_000
    finally:
        rst.stop()
    # Restoring into a directory that is not empty is refused.
    out = subprocess.run([*restore, "first", str(restored)], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode != 0 and "not empty" in out.stderr


def test_delete_prunes_what_no_backup_needs(setup):
    sink, _, source = setup
    src = source()
    s3 = src.client()
    s3.create_bucket(Bucket="data")
    s3.put_object(Bucket="data", Key="kept", Body=os.urandom(100_000))
    s3.put_object(Bucket="data", Key="dropped", Body=os.urandom(100_000))
    assert admin(src, "POST", "backups", {"name": "one", "sink": "offsite"})[0] == 201
    s3.delete_object(Bucket="data", Key="dropped")
    assert admin(src, "POST", "backups", {"name": "two", "sink": "offsite"})[0] == 201
    blobs_before = len(sink_keys(sink, "cairn/blobs/"))
    code, out = admin(src, "DELETE", "backups/one?sink=offsite")
    assert code == 200 and out["blobs_deleted"] >= 1, out
    assert len(sink_keys(sink, "cairn/blobs/")) == blobs_before - 1  # only "dropped"'s blob went
    assert not [k for k in sink_keys(sink, "cairn/backups/one/")]
    assert admin(src, "DELETE", "backups/one?sink=offsite")[0] == 404
    assert admin(src, "DELETE", "backups/two?sink=offsite")[0] == 200
    assert sink_keys(sink, "cairn/blobs/") == [] and sink_keys(sink, "cairn/packs/") == []
    actions = [e["action"] for e in admin(src, "GET", "audit?after=0")[1]["entries"] if e["action"].startswith("backup.")]
    assert actions == ["backup.create", "backup.create", "backup.delete", "backup.delete"]


def test_scheduled_backups_keep_the_newest(setup):
    sink, _, source = setup
    src = source(CAIRN_STORE_BACKUP_SINK_SCHEDULE="offsite", CAIRN_STORE_BACKUP_SINK_INTERVAL="1", CAIRN_STORE_BACKUP_SINK_KEEP="2")
    src.client().create_bucket(Bucket="data")
    src.client().put_object(Bucket="data", Key="k", Body=b"v" * 70_000)
    created = set()
    end = time.time() + 30
    while time.time() < end and len(created) < 4:
        for b in admin(src, "GET", "backups?sink=offsite")[1]["backups"]:
            created.add(b["name"])
        time.sleep(0.3)
    assert len(created) >= 4 and all(n.startswith("auto-") for n in created)
    time.sleep(1.5)
    assert len(admin(src, "GET", "backups?sink=offsite")[1]["backups"]) <= 3  # the newest two (and one in flight)


def test_errors_and_cli(setup, custom_store):
    sink, _, source = setup
    src = source()
    assert admin(src, "POST", "backups", {"name": "x", "sink": "nope"})[0] == 404
    assert admin(src, "POST", "backups", {"name": "../x", "sink": "offsite"})[0] == 400
    assert admin(src, "GET", "backups?sink=nope")[0] == 404
    plain = custom_store({})
    assert admin(plain, "POST", "backups", {"name": "x", "sink": "offsite"})[0] == 501  # no sinks configured
    env = {**os.environ, "CAIRN_STORE_ENDPOINT": src.endpoint, "CAIRN_STORE_ACCESS_KEY": ADMIN[0], "CAIRN_STORE_SECRET_KEY": ADMIN[1]}

    def cli(*args):
        p = subprocess.run([src.binary, "admin", *args], env=env, capture_output=True, text=True, timeout=60)
        return p.returncode, json.loads(p.stdout) if p.stdout.strip() else None

    src.client().create_bucket(Bucket="data")
    rc, out = cli("backup", "from-cli", "--sink", "offsite")
    assert rc == 0 and out["name"] == "from-cli"
    assert [b["name"] for b in cli("backups", "--sink", "offsite")[1]["backups"]] == ["from-cli"]
    assert cli("backup-delete", "from-cli", "--sink", "offsite")[0] == 0
    # A sink that cannot be reached: 502, nothing written.
    sink.stop(kill=True)
    assert admin(src, "POST", "backups", {"name": "unreachable", "sink": "offsite"})[0] == 502
