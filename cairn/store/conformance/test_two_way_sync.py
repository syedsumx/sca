"""Two-way (active-active) sync between two stores: each bucket replicates to the other. Replicas are not
replicated again (no ping-pong), the newer write wins on both sides, deletes travel both ways, and objects
written before replication was configured are sent with a resync."""

import json
import os
import subprocess
import time

import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

from conftest import ADMIN, READER, REGION, Store, _free_port


def rules(target):
    return {"Role": f"arn:cairn:store:::{target}", "Rules": [
        {"ID": "all", "Priority": 1, "Status": "Enabled", "Filter": {"Prefix": ""},
         "DeleteMarkerReplication": {"Status": "Enabled"}, "Destination": {"Bucket": "arn:aws:s3:::shared"}}]}


def wait_for(cond, timeout=30, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return
        except ClientError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def sites(binary, tmp_path):
    pa, pb = _free_port(), _free_port()
    for name, port in (("a", pa), ("b", pb)):
        target = {"id": name, "endpoint": f"http://127.0.0.1:{port}", "access_key": ADMIN[0], "secret_key": ADMIN[1]}
        (tmp_path / f"to-{name}.json").write_text(json.dumps([target]))
    a = Store(binary, str(tmp_path / "a"), pa, env={"CAIRN_STORE_REPLICATION_TARGETS_FILE": str(tmp_path / "to-b.json")})
    b = Store(binary, str(tmp_path / "b"), pb, env={"CAIRN_STORE_REPLICATION_TARGETS_FILE": str(tmp_path / "to-a.json")})
    a.start(), b.start()
    for st in (a, b):
        s3 = st.client()
        s3.create_bucket(Bucket="shared")
        s3.put_bucket_versioning(Bucket="shared", VersioningConfiguration={"Status": "Enabled"})
    yield a, b
    a.stop(kill=True)
    b.stop(kill=True)


def connect(a, b):
    a.client().put_bucket_replication(Bucket="shared", ReplicationConfiguration=rules("b"))
    b.client().put_bucket_replication(Bucket="shared", ReplicationConfiguration=rules("a"))


def versions(st, key):
    return [v for v in st.client().list_object_versions(Bucket="shared", Prefix=key).get("Versions", []) if v["Key"] == key]


def test_writes_go_both_ways_without_ping_pong(sites):
    a, b = sites
    connect(a, b)
    sa, sb = a.client(), b.client()
    sa.put_object(Bucket="shared", Key="from-a", Body=b"written on a")
    sb.put_object(Bucket="shared", Key="from-b", Body=b"written on b")
    wait_for(lambda: sb.get_object(Bucket="shared", Key="from-a")["Body"].read() == b"written on a", what="a -> b")
    wait_for(lambda: sa.get_object(Bucket="shared", Key="from-b")["Body"].read() == b"written on b", what="b -> a")
    assert sb.head_object(Bucket="shared", Key="from-a")["ReplicationStatus"] == "REPLICA"
    wait_for(lambda: sa.head_object(Bucket="shared", Key="from-a").get("ReplicationStatus") == "COMPLETED", what="status")
    time.sleep(2)  # had the replica been replicated back, a would now have a second version
    assert len(versions(a, "from-a")) == 1 and len(versions(b, "from-a")) == 1
    assert len(versions(a, "from-b")) == 1 and len(versions(b, "from-b")) == 1
    # Same modification time on both sides: the replica keeps the source's.
    assert sa.head_object(Bucket="shared", Key="from-a")["LastModified"] == sb.head_object(Bucket="shared", Key="from-a")["LastModified"]
    # Deletes travel too (as delete markers), and are not sent back.
    sb.delete_object(Bucket="shared", Key="from-a")
    wait_for(lambda: _gone(sa, "from-a"), what="delete b -> a")
    time.sleep(1)
    markers = sa.list_object_versions(Bucket="shared", Prefix="from-a").get("DeleteMarkers", [])
    assert len(markers) == 1
    assert len(b.client().list_object_versions(Bucket="shared", Prefix="from-a").get("DeleteMarkers", [])) == 1


def _gone(s3, key):
    try:
        s3.head_object(Bucket="shared", Key=key)
        return False
    except ClientError as e:
        return e.response["Error"]["Code"] in ("404", "NoSuchKey")


def replica_put(st, key, body, modified_ms, cred=ADMIN):
    url = f"{st.endpoint}/shared/{key}"
    req = AWSRequest(method="PUT", url=url, data=body, headers={"x-cairn-replica": str(modified_ms)})
    S3SigV4Auth(Credentials(*cred), "s3", REGION).add_auth(req)
    return requests.put(url, data=body, headers=dict(req.headers), timeout=10)


def test_the_newer_write_wins_on_both_sides(sites):
    a, b = sites
    sa = a.client()
    sa.put_object(Bucket="shared", Key="doc", Body=b"local, now")
    now = int(time.time() * 1000)
    # A replica older than the current version is acknowledged and dropped: the newer write stays current.
    r = replica_put(a, "doc", b"older, from the other site", now - 60_000)
    assert r.status_code == 200 and r.headers.get("x-cairn-replica") == "skipped"
    assert sa.get_object(Bucket="shared", Key="doc")["Body"].read() == b"local, now"
    # A newer one replaces it, keeping the source's modification time, and is marked as a replica.
    r = replica_put(a, "doc", b"newer, from the other site", now + 1000)
    assert r.status_code == 200 and r.headers.get("x-cairn-replica") is None
    h = sa.head_object(Bucket="shared", Key="doc")
    assert sa.get_object(Bucket="shared", Key="doc")["Body"].read() == b"newer, from the other site"
    assert h["ReplicationStatus"] == "REPLICA" and int(h["LastModified"].timestamp()) == (now + 1000) // 1000
    # Writing replicas needs write access (s3:ReplicateObject): a read-only key is refused.
    assert replica_put(a, "doc", b"x", now + 5000, cred=READER).status_code == 403
    # A replica timestamp from the far future is refused rather than winning every conflict.
    assert replica_put(a, "doc", b"x", now + 3_600_000).status_code == 400


def test_concurrent_writes_converge(sites):
    a, b = sites
    sa, sb = a.client(), b.client()
    sa.put_object(Bucket="shared", Key="race", Body=b"a wrote first")
    time.sleep(0.05)
    sb.put_object(Bucket="shared", Key="race", Body=b"b wrote last")
    connect(a, b)  # both writes now cross: each side must end with b's (newer) write
    for st in (a, b):
        env = {**os.environ, "CAIRN_STORE_ENDPOINT": st.endpoint, "CAIRN_STORE_ACCESS_KEY": ADMIN[0], "CAIRN_STORE_SECRET_KEY": ADMIN[1]}
        subprocess.run([st.binary, "admin", "replication-resync", "shared"], check=True, capture_output=True, timeout=30, env=env)
    for s3 in (sa, sb):
        wait_for(lambda s3=s3: s3.get_object(Bucket="shared", Key="race")["Body"].read() == b"b wrote last", what="convergence")
    time.sleep(1.5)
    assert sa.get_object(Bucket="shared", Key="race")["Body"].read() == sb.get_object(Bucket="shared", Key="race")["Body"].read()


def test_resync_sends_existing_objects(sites):
    a, b = sites
    sa = a.client()
    for i in range(25):
        sa.put_object(Bucket="shared", Key=f"old/{i:02d}", Body=f"existing {i}".encode())
    connect(a, b)
    assert "Contents" not in b.client().list_objects_v2(Bucket="shared", Prefix="old/")
    data = b""
    req = AWSRequest(method="POST", url=f"{a.endpoint}/-/admin/v1/buckets/shared/replication/resync", data=data)
    S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
    r = requests.post(req.url, data=data, headers=dict(req.headers), timeout=10)
    assert r.status_code == 200 and r.json()["queued"] == 25
    wait_for(lambda: b.client().list_objects_v2(Bucket="shared", Prefix="old/").get("KeyCount") == 25, what="resync")
    # Resyncing again sends nothing new back and forth: replicas are skipped on b's side.
    req = AWSRequest(method="POST", url=f"{b.endpoint}/-/admin/v1/buckets/shared/replication/resync", data=data)
    S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
    assert requests.post(req.url, data=data, headers=dict(req.headers), timeout=10).json()["queued"] == 0
    # Without a replication configuration, a resync is refused.
    req = AWSRequest(method="POST", url=f"{a.endpoint}/-/admin/v1/buckets/nope/replication/resync", data=data)
    S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
    assert requests.post(req.url, data=data, headers=dict(req.headers), timeout=10).status_code == 404
