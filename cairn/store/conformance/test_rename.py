"""Renames and moves within a bucket are one transaction: WebDAV MOVE of a file or a whole directory, SFTP
rename of files and directories, without copying data; overwrite rules; versioned buckets fall back to copy
and delete; authorization of every key, events, usage and the audit trail."""

import base64
import json
import os

import pytest
import requests

from conftest import ADMIN, READER, SCOPED
from test_sftp import connect, wait_port
from test_keys import admin


def auth(key):
    return {"Authorization": "Basic " + base64.b64encode(f"{key[0]}:{key[1]}".encode()).decode()}


def move(st, src, dst, key=ADMIN, overwrite=True):
    h = {**auth(key), "Destination": f"/-/dav/{dst}", "Overwrite": "T" if overwrite else "F"}
    return requests.request("MOVE", f"{st.endpoint}/-/dav/{src}", headers=h, timeout=60)


@pytest.fixture
def st(custom_store):
    s = custom_store({"CAIRN_STORE_FILES_BASIC_AUTH": "true", "CAIRN_STORE_FILES_BASIC_ALLOW_HTTP": "true",
                      "CAIRN_STORE_PACK_BELOW": "0"})
    s.client().create_bucket(Bucket="tree")
    return s


def keys(st, prefix=""):
    s3 = st.client()
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket="tree", Prefix=prefix)
    return sorted(o["Key"] for p in pages for o in p.get("Contents", []))


def blob_files(st):
    return sorted(f for _, _, fs in os.walk(os.path.join(st.data, "blobs")) for f in fs)


def test_directory_move_is_one_transaction(st):
    s3 = st.client()
    for i in range(600):
        s3.put_object(Bucket="tree", Key=f"raw/2026/{i // 100}/f{i:04d}.csv", Body=f"row {i}\n".encode() * 50)
    s3.put_object(Bucket="tree", Key="raw-other/keep.txt", Body=b"not under raw/")
    before = blob_files(st)
    usage = admin(st, "GET", "buckets/tree")[1]["usage"]
    r = move(st, "tree/raw/", "tree/curated/")
    assert r.status_code == 201, r.text
    assert keys(st, "raw/") == [] and len(keys(st, "curated/")) == 600
    assert keys(st, "raw-other/") == ["raw-other/keep.txt"]  # a sibling with the same leading letters stays
    assert s3.get_object(Bucket="tree", Key="curated/2026/3/f0342.csv")["Body"].read() == b"row 342\n" * 50
    assert blob_files(st) == before  # data untouched: no copies
    assert admin(st, "GET", "buckets/tree")[1]["usage"] == usage
    # A single file, then a move onto an existing key: refused with Overwrite: F, replaced with T.
    assert move(st, "tree/curated/2026/0/f0001.csv", "tree/top.csv").status_code == 201
    assert move(st, "tree/curated/2026/0/f0002.csv", "tree/top.csv", overwrite=False).status_code == 412
    assert move(st, "tree/curated/2026/0/f0002.csv", "tree/top.csv").status_code == 204
    assert s3.get_object(Bucket="tree", Key="top.csv")["Body"].read() == b"row 2\n" * 50
    assert admin(st, "GET", "buckets/tree")[1]["usage"]["objects"] == usage["objects"] - 1
    assert move(st, "tree/curated/", "tree/curated/inside/").status_code == 409  # into itself
    entries = [e for e in admin(st, "GET", "audit?after=0&limit=10000")[1]["entries"] if e["action"] == "object.rename"]
    assert len(entries) == 3 and entries[0]["details"]["keys"] == 600


def test_every_key_is_authorized_first(st):
    s3 = st.client()
    for k in ("lake-in/a", "lake-in/b"):
        s3.put_object(Bucket="tree", Key=k, Body=b"x")
    assert move(st, "tree/lake-in/", "tree/lake-out/", key=READER).status_code == 403
    assert move(st, "tree/lake-in/", "tree/lake-out/", key=SCOPED).status_code == 403  # lake-* buckets, not this one
    assert keys(st, "lake-in/") == ["lake-in/a", "lake-in/b"]  # nothing moved


def test_versioned_buckets_copy_then_delete(st):
    s3 = st.client()
    s3.create_bucket(Bucket="vtree")
    s3.put_bucket_versioning(Bucket="vtree", VersioningConfiguration={"Status": "Enabled"})
    s3.put_object(Bucket="vtree", Key="d/a", Body=b"a")
    s3.put_object(Bucket="vtree", Key="d/b", Body=b"b")
    assert move(st, "vtree/d/", "vtree/e/").status_code == 201
    got = sorted(o["Key"] for o in s3.list_objects_v2(Bucket="vtree")["Contents"])
    assert got == ["e/a", "e/b"]
    # The old keys keep their history (a delete marker over each version).
    assert len(s3.list_object_versions(Bucket="vtree", Prefix="d/")["DeleteMarkers"]) == 2


def test_sftp_renames_files_and_directories(custom_store, tmp_path):
    from conftest import _free_port

    port = _free_port()
    s = custom_store({"CAIRN_STORE_SFTP_ADDR": f"127.0.0.1:{port}"})
    wait_port(port)
    s3 = s.client()
    s3.create_bucket(Bucket="ftp")
    for i in range(5):
        s3.put_object(Bucket="ftp", Key=f"in/batch/{i}.json", Body=json.dumps({"i": i}).encode())
    t, c = connect(port)
    c.rename("/ftp/in/batch", "/ftp/done/batch-1")
    assert sorted(c.listdir("/ftp/done/batch-1")) == [f"{i}.json" for i in range(5)]
    with pytest.raises(IOError):
        c.stat("/ftp/in/batch")
    c.rename("/ftp/done/batch-1/0.json", "/ftp/done/first.json")
    assert json.loads(c.open("/ftp/done/first.json").read()) == {"i": 0}
    with pytest.raises(IOError):
        c.rename("/ftp/done/first.json", "/ftp/done/batch-1/1.json")  # SFTP renames never overwrite
    t.close()


def test_encrypted_objects_stay_readable_after_a_move(custom_store):
    s = custom_store({"CAIRN_STORE_FILES_BASIC_AUTH": "true", "CAIRN_STORE_FILES_BASIC_ALLOW_HTTP": "true",
                      "CAIRN_STORE_MASTER_KEY": base64.b64encode(b"r" * 32).decode()})
    s3 = s.client()
    s3.create_bucket(Bucket="tree")
    s3.put_object(Bucket="tree", Key="secret/a.txt", Body=b"classified a")
    s3.put_object(Bucket="tree", Key="secret/b.txt", Body=b"classified b", ServerSideEncryption="AES256")
    assert move(s, "tree/secret/", "tree/vault/").status_code == 201
    # Each data key is bound to its object's name: the move re-wraps it for the new one.
    assert s3.get_object(Bucket="tree", Key="vault/a.txt")["Body"].read() == b"classified a"
    assert s3.get_object(Bucket="tree", Key="vault/b.txt")["ServerSideEncryption"] == "AES256"
    s.stop()
    s.start()
    assert s.client().get_object(Bucket="tree", Key="vault/b.txt")["Body"].read() == b"classified b"
