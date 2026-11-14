"""Governed volumes (L58) on a real Cairn Store with STS: a volume shared with a group is readable by its members
alone, through Cairn's API and with the narrowed credentials Cairn hands an engine; read-only stays read-only, the
credentials reach that one bucket, and a revoked grant ends API access at once.

Needs CAIRN_STORE_BIN."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time

import pytest
from fastapi import Request

from cairn.common.db import get_engine, init_db
from cairn.dataaccess.volumes import StoreClient, VolumeError, Volumes, build_volumes_router

BIN = os.environ.get("CAIRN_STORE_BIN", "")
pytestmark = pytest.mark.skipif(not BIN, reason="needs CAIRN_STORE_BIN (a built cairn-store)")
KEY = ("cairn-volumes-admin", "volumes-secret-" + "v" * 24)
OWNER, ANA, BOB, OPS = (f"urn:li:corpuser:{u}" for u in ("owner", "ana", "bob", "ops"))
ANALYSTS = "urn:li:corpGroup:analysts"
GROUPS = {ANA: [ANALYSTS], BOB: ["urn:li:corpGroup:sales"]}


def port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def store():
    d, p = tempfile.mkdtemp(), port()
    proc = subprocess.Popen([BIN], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **os.environ, "CAIRN_STORE_ACCESS_KEY": KEY[0], "CAIRN_STORE_SECRET_KEY": KEY[1], "CAIRN_STORE_DATA": f"{d}/s",
        "CAIRN_STORE_ADDR": f"127.0.0.1:{p}", "CAIRN_STORE_FSYNC": "false", "CAIRN_STORE_META": "",
        "CAIRN_STORE_STS_SECRET": "volumes-sts-" + "x" * 32})
    end = time.time() + 30
    while True:
        try:
            socket.create_connection(("127.0.0.1", p), 1).close()
            break
        except OSError:
            assert time.time() < end, "the store did not start"
            time.sleep(0.2)
    yield f"http://127.0.0.1:{p}"
    proc.terminate()
    proc.wait(15)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def vs(store, tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/v.db")
    init_db(engine)
    audit = []

    class Audit:
        def record(self, *a):
            audit.append(a)
    v = Volumes(engine, StoreClient(store, *KEY), lambda a: GROUPS.get(a, []), lambda a: a in (OWNER, OPS),
                lambda a: a == OPS, Audit())
    v.audited = audit
    return v


def s3_with(creds):
    import boto3

    return boto3.client("s3", endpoint_url=creds["endpoint"], region_name="us-east-1",
                        aws_access_key_id=creds["access_key_id"], aws_secret_access_key=creds["secret_access_key"],
                        aws_session_token=creds["session_token"])


def test_a_volume_shared_with_a_group_is_theirs_alone(vs):
    vs.create(OWNER, "raw-files", "landing files")
    vs.write(OWNER, "raw-files", "2026/11/orders.csv", b"id,amount\n1,10\n")
    vs.grant(OWNER, "raw-files", ANALYSTS, "read")
    # Ana is in the group: she lists and reads it through Cairn.
    assert [f["path"] for f in vs.files(ANA, "raw-files")["files"]] == ["2026/11/orders.csv"]
    meta, body = vs.read(ANA, "raw-files", "2026/11/orders.csv")
    assert b"".join(body) == b"id,amount\n1,10\n" and meta["size"] == 15
    assert [v["name"] for v in vs.list(ANA)] == ["raw-files"] and vs.list(ANA)[0]["access"] == "read"
    assert "grants" not in vs.get(ANA, "raw-files")                      # only managers see who else has it
    # Read-only stays read-only.
    with pytest.raises(VolumeError) as e:
        vs.write(ANA, "raw-files", "x.csv", b"x")
    assert e.value.status == 403
    # Bob is not in the group: to him it does not exist.
    for call in (lambda: vs.files(BOB, "raw-files"), lambda: vs.read(BOB, "raw-files", "2026/11/orders.csv"),
                 lambda: vs.get(BOB, "raw-files"), lambda: vs.credentials(BOB, "raw-files")):
        with pytest.raises(VolumeError) as e:
            call()
        assert e.value.status == 404
    assert vs.list(BOB) == []
    assert [a[1] for a in vs.audited][:4] == ["volume.created", "volume.file_written", "volume.granted", "volume.file_read"]


def test_engine_credentials_reach_one_bucket_and_no_more(vs):
    from botocore.exceptions import ClientError

    vs.create(OWNER, "models", "")
    vs.create(OWNER, "secret-stuff", "")
    vs.write(OWNER, "models", "m.bin", b"weights")
    vs.write(OWNER, "secret-stuff", "s.txt", b"hidden")
    vs.grant(OWNER, "models", ANALYSTS, "read")
    c = vs.credentials(ANA, "models")
    assert c["bucket"] == "cairn-vol-models" and c["access"] == "read" and c["path"] == "s3a://cairn-vol-models/"
    s3 = s3_with(c)
    assert s3.get_object(Bucket="cairn-vol-models", Key="m.bin")["Body"].read() == b"weights"
    assert [o["Key"] for o in s3.list_objects_v2(Bucket="cairn-vol-models")["Contents"]] == ["m.bin"]
    for denied in (lambda: s3.put_object(Bucket="cairn-vol-models", Key="new", Body=b"x"),       # read-only
                   lambda: s3.get_object(Bucket="cairn-vol-secret-stuff", Key="s.txt"),        # another volume
                   lambda: s3.list_objects_v2(Bucket="cairn-vol-secret-stuff"),
                   lambda: s3.delete_object(Bucket="cairn-vol-models", Key="m.bin")):
        with pytest.raises(ClientError) as e:
            denied()
        assert e.value.response["Error"]["Code"] == "AccessDenied", e.value.response
    with pytest.raises(VolumeError) as e:                                  # write credentials need a write grant
        vs.credentials(ANA, "models", "write")
    assert e.value.status == 403
    # Writers get write credentials: Spark-style multipart upload included.
    vs.grant(OWNER, "models", ANALYSTS, "write")
    w = s3_with(vs.credentials(ANA, "models", "write"))
    w.upload_fileobj(__import__("io").BytesIO(b"p" * (6 << 20)), "cairn-vol-models", "big/part-0000.parquet",
                     Config=__import__("boto3").s3.transfer.TransferConfig(multipart_threshold=5 << 20))
    assert vs.files(ANA, "models", "big/")["files"][0]["size"] == 6 << 20
    with pytest.raises(ClientError):
        w.get_object(Bucket="cairn-vol-secret-stuff", Key="s.txt")
    assert vs.credentials(ANA, "models")["expiration"]


def test_revoking_ends_api_access_and_new_credentials(vs):
    vs.create(OWNER, "shared", "")
    vs.grant(OWNER, "shared", ANA, "read")                                 # to the person, not a group
    vs.files(ANA, "shared")
    with pytest.raises(VolumeError) as e:                                  # only the owner or an operator manages
        vs.grant(ANA, "shared", BOB, "read")
    assert e.value.status == 403
    vs.revoke(OWNER, "shared", ANA)
    for call in (lambda: vs.files(ANA, "shared"), lambda: vs.credentials(ANA, "shared")):
        with pytest.raises(VolumeError) as e:
            call()
        assert e.value.status == 404
    assert vs.get(OPS, "shared")["grants"] == []                           # operators see and manage every volume


def test_creating_deleting_and_bad_input(vs, store):
    with pytest.raises(VolumeError) as e:
        vs.create(ANA, "mine")                                             # needs MANAGE_VOLUMES
    assert e.value.status == 403
    for bad in ("A", "x", "has_underscore", "-dash", "a" * 41):
        with pytest.raises(VolumeError, match="name"):
            vs.create(OWNER, bad)
    vs.create(OWNER, "tmp-vol")
    with pytest.raises(VolumeError) as e:
        vs.create(OWNER, "tmp-vol")
    assert e.value.status == 409
    for path in ("../x", "a//b", "a/../b", "with space", "", "/abs"):
        with pytest.raises(VolumeError, match="path"):
            vs.write(OWNER, "tmp-vol", path, b"x")
    with pytest.raises(VolumeError, match="principal"):
        vs.grant(OWNER, "tmp-vol", "ana", "read")
    with pytest.raises(VolumeError, match="access"):
        vs.grant(OWNER, "tmp-vol", ANA, "admin")
    vs.write(OWNER, "tmp-vol", "f.txt", b"x")
    with pytest.raises(VolumeError) as e:                                  # only empty volumes are deleted
        vs.drop(OWNER, "tmp-vol")
    assert e.value.status == 409
    vs.remove(OWNER, "tmp-vol", "f.txt")
    vs.drop(OWNER, "tmp-vol")
    with pytest.raises(VolumeError) as e:
        vs.get(OWNER, "tmp-vol")
    assert e.value.status == 404
    assert "cairn-vol-tmp-vol" not in StoreClient(store, *KEY)._s3("GET", "/").text


def test_no_store_key_means_503(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path}/n.db")
    init_db(engine)
    with pytest.raises(VolumeError) as e:
        Volumes(engine, None, lambda a: [], lambda a: True, lambda a: False).create(OWNER, "nowhere")
    assert e.value.status == 503


def test_the_api(vs):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    class P:
        def __init__(self, urn):
            self.urn = urn

    def principal(request: Request):
        return P(request.headers["x-user"])
    app = FastAPI()
    app.include_router(build_volumes_router(vs, principal))
    c = TestClient(app)
    o, a, b = ({"x-user": u} for u in (OWNER, ANA, BOB))
    assert c.post("/v1/volumes", json={"name": "api-vol"}, headers=o).status_code == 201
    assert c.put("/v1/volumes/api-vol/files/docs/readme.md", content=b"# hello", headers=o).status_code == 200
    assert c.put("/v1/volumes/api-vol/grants", json={"principal": ANALYSTS, "access": "read"}, headers=o).status_code == 200
    r = c.get("/v1/volumes/api-vol/files/docs/readme.md", headers=a)
    assert r.status_code == 200 and r.content == b"# hello"
    assert c.get("/v1/volumes/api-vol/files", params={"prefix": "docs/"}, headers=a).json()["files"][0]["path"] == "docs/readme.md"
    assert c.get("/v1/volumes/api-vol/files/docs/readme.md", headers=b).status_code == 404
    assert c.put("/v1/volumes/api-vol/files/x", content=b"x", headers=a).status_code == 403
    cr = c.post("/v1/volumes/api-vol/credentials", json={}, headers=a)
    assert cr.status_code == 200 and cr.json()["access"] == "read"
    assert c.post("/v1/volumes/api-vol/credentials", json={"access": "write"}, headers=a).status_code == 403
    assert c.delete("/v1/volumes/api-vol/grants", params={"principal": ANALYSTS}, headers=o).status_code == 200
    assert c.get("/v1/volumes/api-vol/files", headers=a).status_code == 404
    vs.max_upload = 10
    assert c.put("/v1/volumes/api-vol/files/big", content=b"x" * 11, headers=o).status_code == 413
    assert c.delete("/v1/volumes/api-vol/files/docs/readme.md", headers=o).status_code == 204
    assert c.delete("/v1/volumes/api-vol", headers=o).status_code == 204
