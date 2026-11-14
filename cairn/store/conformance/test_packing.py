"""Small objects are packed into shared pack files (CAIRN_STORE_PACK_BELOW) and compacted after deletes; reads,
ranges, encryption, versions and backups work the same."""

import base64
import json
import os
import random
from pathlib import Path

import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from conftest import ADMIN, REGION


def admin(store, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = AWSRequest(method=method, url=f"{store.endpoint}/-/admin/v1/{path}", data=data)
    S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
    r = requests.request(method, req.url, data=data, headers=dict(req.headers), timeout=60)
    assert r.status_code < 300, r.text
    return r.json()


@pytest.fixture
def packed(custom_store):
    st = custom_store({"CAIRN_STORE_MASTER_KEY": base64.b64encode(os.urandom(32)).decode(), "CAIRN_STORE_PACK_FILE_BYTES": str(2 << 20)})
    st.client().create_bucket(Bucket="small")
    return st


def files_under(path):
    return [p for p in Path(path).rglob("*") if p.is_file()]


def test_small_objects_share_pack_files(packed):
    s3 = packed.client()
    bodies = {f"obj/{i:05d}": os.urandom(random.randint(100, 4000)) for i in range(1500)}
    for k, b in bodies.items():
        s3.put_object(Bucket="small", Key=k, Body=b)
    s3.put_object(Bucket="small", Key="large.bin", Body=os.urandom(200_000))  # above 64 KiB: a file of its own
    blobs = files_under(os.path.join(packed.data, "blobs"))
    packs = files_under(os.path.join(packed.data, "packs"))
    assert len(blobs) == 1 and 1 <= len(packs) <= 4  # 1500 objects in a few files, rotated at 2 MiB
    for k in random.sample(sorted(bodies), 200):
        got = s3.get_object(Bucket="small", Key=k)
        assert got["Body"].read() == bodies[k] and got["ServerSideEncryption"] == "AES256"
    k = "obj/00042"
    assert s3.get_object(Bucket="small", Key=k, Range="bytes=10-49")["Body"].read() == bodies[k][10:50]
    # Packed bytes are encrypted on disk like any blob.
    raw = b"".join(p.read_bytes() for p in packs)
    assert bodies["obj/00007"][:64] not in raw
    st = admin(packed, "GET", "stats")["packs"]
    assert st["packs"] == len(packs) and st["live_bytes"] > 0


def test_compaction_reclaims_space_after_deletes(packed):
    s3 = packed.client()
    bodies = {f"c/{i:05d}": os.urandom(3000) for i in range(1200)}  # ~3.6 MB: a full 2 MiB pack and more
    for k, b in bodies.items():
        s3.put_object(Bucket="small", Key=k, Body=b)
    doomed = sorted(bodies)[:1000]
    for i in range(0, len(doomed), 500):
        s3.delete_objects(Bucket="small", Delete={"Objects": [{"Key": k} for k in doomed[i:i + 500]]})
    before = sum(p.stat().st_size for p in files_under(os.path.join(packed.data, "packs")))
    rep = admin(packed, "POST", "compact", {"min_dead": 0.5})
    after = sum(p.stat().st_size for p in files_under(os.path.join(packed.data, "packs")))
    # The full pack held only deleted objects (removed outright); live ones would have been moved out.
    assert rep["packs_compacted"] + rep["packs_deleted"] >= 1 and rep["bytes_reclaimed"] > 0 and after < before
    for k in sorted(bodies)[1000:]:
        assert s3.get_object(Bucket="small", Key=k)["Body"].read() == bodies[k]
    entries = admin(packed, "GET", "audit?after=0&limit=10000")["entries"]
    assert any(e["action"] == "packs.compact" for e in entries)


def test_versions_and_restart(packed):
    s3 = packed.client()
    s3.put_bucket_versioning(Bucket="small", VersioningConfiguration={"Status": "Enabled"})
    v1 = s3.put_object(Bucket="small", Key="doc", Body=b"v1")["VersionId"]
    s3.put_object(Bucket="small", Key="doc", Body=b"v2")
    packed.stop()
    packed.start()
    admin(packed, "POST", "compact", {"min_dead": 0.0})
    s3 = packed.client()
    assert s3.get_object(Bucket="small", Key="doc")["Body"].read() == b"v2"
    assert s3.get_object(Bucket="small", Key="doc", VersionId=v1)["Body"].read() == b"v1"


def test_backups_of_packed_objects(custom_store, tmp_path):
    import shutil
    import subprocess

    from conftest import Store, _free_port

    root = tmp_path / "backups"
    st = custom_store({"CAIRN_STORE_BACKUP_DIR": str(root), "CAIRN_STORE_PACK_FILE_BYTES": str(1 << 20)})
    s3 = st.client()
    s3.create_bucket(Bucket="packed-bk")
    first = {f"a/{i:04d}": os.urandom(2000) for i in range(800)}  # ~1.6 MB: one closed pack and an active one
    for k, b in first.items():
        s3.put_object(Bucket="packed-bk", Key=k, Body=b)
    b1 = admin(st, "POST", "backups", {"name": "one"})
    second = {f"b/{i:04d}": os.urandom(2000) for i in range(100)}
    for k, b in second.items():
        s3.put_object(Bucket="packed-bk", Key=k, Body=b)
    b2 = admin(st, "POST", "backups", {"name": "two"})
    assert b1["blobs"] >= 2 and b2["blobs_linked"] >= 1  # the closed pack is linked; the growing one is copied
    restored = tmp_path / "restored"
    shutil.copytree(root / "two", restored)
    assert subprocess.run([st.binary, "fsck", "-data", str(restored)], capture_output=True, timeout=60).returncode == 0
    rst = Store(st.binary, str(restored), _free_port(), env={"CAIRN_STORE_META": ""}).start()
    try:
        r3 = rst.client()
        for k, b in list(first.items())[::40] + list(second.items())[::10]:
            assert r3.get_object(Bucket="packed-bk", Key=k)["Body"].read() == b
    finally:
        rst.stop()
