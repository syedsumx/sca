"""Remote buckets mounted into local buckets (here the remote is another Cairn Store): sync of objects,
changes and deletions; reads (ranges too) fetched from the remote; caching and uncaching; write-back of
local writes and deletes; periodic sync; and the refusals."""

import base64
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

from conftest import ADMIN, REGION

MASTER = base64.b64encode(b"m" * 32).decode()


def admin(st, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = AWSRequest(method=method, url=f"{st.endpoint}/-/admin/v1/{path}", data=data)
    S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
    r = requests.request(method, req.url, data=data, headers=dict(req.headers), timeout=60)
    return r.status_code, (r.json() if r.content else None)


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
def env(custom_store, tmp_path):
    remote = custom_store({})
    up = remote.client()
    up.create_bucket(Bucket="upstream")
    remotes = tmp_path / "remotes.json"
    keys = {"access_key": ADMIN[0], "secret_key": ADMIN[1], "endpoint": remote.endpoint, "bucket": "upstream"}
    remotes.write_text(json.dumps([{"id": "up", "prefix": "data/", **keys}, {"id": "other", "prefix": "other/", **keys}]))

    def local(**extra):
        return custom_store({"CAIRN_STORE_REMOTES_FILE": str(remotes), "CAIRN_STORE_MASTER_KEY": MASTER,
                             "CAIRN_STORE_REMOTE_SYNC_INTERVAL": "3600", **extra})

    return remote, up, local


def test_mount_sync_read_and_follow_changes(env):
    remote, up, local = env
    bodies = {f"f{i}.csv": os.urandom(3000 + i) for i in range(8)}
    for k, b in bodies.items():
        up.put_object(Bucket="upstream", Key=f"data/{k}", Body=b, ContentType="text/csv", Metadata={"source": "erp"})
    up.put_object(Bucket="upstream", Key="elsewhere.txt", Body=b"outside the prefix")
    st = local()
    s3 = st.client()
    s3.create_bucket(Bucket="mirror")
    assert admin(st, "PUT", "buckets/mirror/remote", {"remote": "up"})[0] == 200
    code, rep = admin(st, "POST", "buckets/mirror/remote/sync")
    assert code == 200 and rep["added"] == 8 and rep["remote_objects"] == 8, rep
    assert sorted(o["Key"] for o in s3.list_objects_v2(Bucket="mirror")["Contents"]) == sorted(bodies)
    h = s3.head_object(Bucket="mirror", Key="f3.csv")
    assert h["ContentType"] == "text/csv" and h["Metadata"] == {"source": "erp"} and h["ContentLength"] == 3003
    assert s3.get_object(Bucket="mirror", Key="f3.csv")["Body"].read() == bodies["f3.csv"]
    assert s3.get_object(Bucket="mirror", Key="f5.csv", Range="bytes=100-199")["Body"].read() == bodies["f5.csv"][100:200]
    # The data stayed on the remote: nothing was written to this store's disk for it.
    assert not any(files for _, _, files in os.walk(os.path.join(st.data, "blobs")))
    status = admin(st, "GET", "buckets/mirror/remote")[1]
    assert status["remote_only"] == 8 and status["cached"] == 0 and status["write_back"] is False
    # Changes on the remote follow at the next pass.
    up.put_object(Bucket="upstream", Key="data/f0.csv", Body=b"changed upstream")
    up.delete_object(Bucket="upstream", Key="data/f1.csv")
    up.put_object(Bucket="upstream", Key="data/new/g.json", Body=b"{}")
    rep = admin(st, "POST", "buckets/mirror/remote/sync")[1]
    assert (rep["added"], rep["updated"], rep["removed"]) == (1, 1, 1), rep
    assert s3.get_object(Bucket="mirror", Key="f0.csv")["Body"].read() == b"changed upstream"
    with pytest.raises(ClientError):
        s3.head_object(Bucket="mirror", Key="f1.csv")
    assert s3.get_object(Bucket="mirror", Key="new/g.json")["Body"].read() == b"{}"
    # A second pass with nothing changed does nothing.
    rep = admin(st, "POST", "buckets/mirror/remote/sync")[1]
    assert (rep["added"], rep["updated"], rep["removed"]) == (0, 0, 0), rep
    # Without write-back, local writes stay local and are not touched by syncs.
    s3.put_object(Bucket="mirror", Key="local-only.txt", Body=b"mine")
    admin(st, "POST", "buckets/mirror/remote/sync")
    assert s3.get_object(Bucket="mirror", Key="local-only.txt")["Body"].read() == b"mine"
    with pytest.raises(ClientError):
        up.head_object(Bucket="upstream", Key="data/local-only.txt")
    # Deleting a mirror here leaves the remote alone.
    s3.delete_object(Bucket="mirror", Key="f2.csv")
    time.sleep(0.5)
    assert up.head_object(Bucket="upstream", Key="data/f2.csv")["ContentLength"] == 3002


def test_cache_and_uncache(env):
    remote, up, local = env
    for i in range(4):
        up.put_object(Bucket="upstream", Key=f"data/c{i}", Body=f"content {i}".encode() * 1000)
    st = local()
    s3 = st.client()
    s3.create_bucket(Bucket="cached")
    admin(st, "PUT", "buckets/cached/remote", {"remote": "up"})
    admin(st, "POST", "buckets/cached/remote/sync")
    code, rep = admin(st, "POST", "buckets/cached/remote/cache", {"prefix": "c"})
    assert code == 200 and rep["cached"] == 4, rep
    # Cached objects are encrypted at rest like any new object.
    assert all(b"content 1" not in open(os.path.join(d, f), "rb").read()
               for d, _, fs in os.walk(os.path.join(st.data, "blobs")) for f in fs)
    up.put_object(Bucket="upstream", Key="data/late", Body=b"not cached")
    admin(st, "POST", "buckets/cached/remote/sync")
    remote.stop()
    # With the remote down, cached objects still read; remote-only ones cannot.
    assert s3.get_object(Bucket="cached", Key="c2")["Body"].read() == b"content 2" * 1000
    with pytest.raises(ClientError):
        s3.get_object(Bucket="cached", Key="late")["Body"].read()
    remote.start()
    code, rep = admin(st, "POST", "buckets/cached/remote/uncache", {"prefix": "c"})
    assert code == 200 and rep["uncached"] == 4
    assert s3.get_object(Bucket="cached", Key="c3")["Body"].read() == b"content 3" * 1000
    assert admin(st, "GET", "buckets/cached/remote")[1]["cached"] == 0


def test_write_back(env):
    remote, up, local = env
    up.put_object(Bucket="upstream", Key="data/existing", Body=b"from upstream")
    st = local()
    s3 = st.client()
    s3.create_bucket(Bucket="wb-bucket")
    assert admin(st, "PUT", "buckets/wb-bucket/remote", {"remote": "up", "write_back": True})[0] == 200
    admin(st, "POST", "buckets/wb-bucket/remote/sync")
    s3.put_object(Bucket="wb-bucket", Key="written/here.txt", Body=b"local write", ContentType="text/plain", Metadata={"by": "cairn"})
    wait_for(lambda: up.get_object(Bucket="upstream", Key="data/written/here.txt")["Body"].read() == b"local write", what="write-back")
    h = up.head_object(Bucket="upstream", Key="data/written/here.txt")
    assert h["ContentType"] == "text/plain" and h["Metadata"] == {"by": "cairn"}
    # Large objects stream through (encrypted here, plaintext on the remote).
    big = os.urandom(3 << 20)
    s3.put_object(Bucket="wb-bucket", Key="big.bin", Body=big)
    wait_for(lambda: up.head_object(Bucket="upstream", Key="data/big.bin")["ContentLength"] == len(big), what="big write-back")
    assert up.get_object(Bucket="upstream", Key="data/big.bin")["Body"].read() == big
    # A sync after write-back changes nothing (the written objects are known to the remote).
    rep = admin(st, "POST", "buckets/wb-bucket/remote/sync")[1]
    assert (rep["added"], rep["updated"], rep["removed"]) == (0, 0, 0), rep
    # Deletes are written back too: of local writes and of mirrors.
    s3.delete_object(Bucket="wb-bucket", Key="written/here.txt")
    s3.delete_object(Bucket="wb-bucket", Key="existing")
    wait_for(lambda: "Contents" not in up.list_objects_v2(Bucket="upstream", Prefix="data/written/"), what="delete write-back")
    wait_for(lambda: "Contents" not in up.list_objects_v2(Bucket="upstream", Prefix="data/existing"), what="mirror delete write-back")
    # Overwriting a mirror here writes the new version back.
    up.put_object(Bucket="upstream", Key="data/doc", Body=b"v1")
    admin(st, "POST", "buckets/wb-bucket/remote/sync")
    s3.put_object(Bucket="wb-bucket", Key="doc", Body=b"v2 from here")
    wait_for(lambda: up.get_object(Bucket="upstream", Key="data/doc")["Body"].read() == b"v2 from here", what="overwrite write-back")


def test_periodic_sync_and_refusals(env, binary):
    remote, up, local = env
    st = local(CAIRN_STORE_REMOTE_SYNC_INTERVAL="1")
    s3 = st.client()
    s3.create_bucket(Bucket="auto")
    s3.create_bucket(Bucket="second")
    assert admin(st, "PUT", "buckets/auto/remote", {"remote": "nope"})[0] == 404
    assert admin(st, "PUT", "buckets/auto/remote", {"remote": "up"})[0] == 200
    assert admin(st, "PUT", "buckets/second/remote", {"remote": "up"})[0] == 409  # mounted in one bucket only
    assert admin(st, "POST", "buckets/second/remote/sync")[0] == 404
    assert admin(st, "PUT", "buckets/no-bucket/remote", {"remote": "other"})[0] == 404
    up.put_object(Bucket="upstream", Key="data/arrives", Body=b"by itself")
    wait_for(lambda: s3.get_object(Bucket="auto", Key="arrives")["Body"].read() == b"by itself", what="periodic sync")
    env_ = {**os.environ, "CAIRN_STORE_ENDPOINT": st.endpoint, "CAIRN_STORE_ACCESS_KEY": ADMIN[0], "CAIRN_STORE_SECRET_KEY": ADMIN[1]}

    def cli(*args):
        p = subprocess.run([binary, "admin", *args], env=env_, capture_output=True, text=True, timeout=60)
        return p.returncode, json.loads(p.stdout) if p.stdout.strip() else None

    assert cli("remote", "auto")[1]["remote"] == "up"
    assert cli("remote-sync", "auto")[0] == 0
    assert cli("remote-mount", "second", "--remote", "other", "--write-back")[1]["write_back"] is True
    assert cli("remote-unmount", "second")[0] == 0
    assert cli("remote", "second")[0] != 0
    actions = [e["action"] for e in admin(st, "GET", "audit?after=0")[1]["entries"] if e["action"].startswith("remote.")]
    assert actions == ["remote.mount", "remote.mount", "remote.unmount"]
    # A store without remotes refuses mounts.
    plain = local(CAIRN_STORE_REMOTES_FILE="")
    plain.client().create_bucket(Bucket="x-bucket")
    assert admin(plain, "PUT", "buckets/x-bucket/remote", {"remote": "up"})[0] == 404
