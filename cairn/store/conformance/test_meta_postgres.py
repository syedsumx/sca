"""Metadata in PostgreSQL (CAIRN_STORE_META): moving a store's embedded index there with `migrate-meta`, and the
binding between a data directory and its metadata database. Needs CAIRN_STORE_TEST_META (an admin URL of a
PostgreSQL server); the whole suite also runs on PostgreSQL metadata when it is set."""

import json
import os
import subprocess
import uuid

import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from conftest import ADMIN, REGION, Store, _free_port

ADMIN_URL = os.environ.get("CAIRN_STORE_TEST_META")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="needs CAIRN_STORE_TEST_META (PostgreSQL)")


def new_database() -> str:
    import psycopg

    name = "store_" + uuid.uuid4().hex[:12]
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    base, _, query = ADMIN_URL.partition("?")
    return base.rsplit("/", 1)[0] + "/" + name + (f"?{query}" if query else "")


def admin(store, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = AWSRequest(method=method, url=f"{store.endpoint}/-/admin/v1/{path}", data=data)
    S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
    r = requests.request(method, req.url, data=data, headers=dict(req.headers), timeout=10)
    return r.status_code, (r.json() if r.content else None)


def test_migrate_embedded_index_to_postgres(binary, tmp_path):
    data = str(tmp_path / "data")
    st = Store(binary, data, _free_port(), env={"CAIRN_STORE_META": ""}).start()
    s3 = st.client()
    s3.create_bucket(Bucket="migrated")
    s3.put_bucket_versioning(Bucket="migrated", VersioningConfiguration={"Status": "Enabled"})
    v1 = s3.put_object(Bucket="migrated", Key="doc", Body=b"one")["VersionId"]
    s3.put_object(Bucket="migrated", Key="doc", Body=b"two", Tagging="k=v")
    for i in range(1200):  # more than one page of a range
        s3.put_object(Bucket="migrated", Key=f"many/{i:05d}", Body=b"x")
    _, key = admin(st, "POST", "keys", {"access_key": "kept-key", "buckets": ["migrated"]})
    st.stop()

    url = new_database()
    env = {**os.environ, "CAIRN_STORE_DATA": data, "CAIRN_STORE_META": url,
           "CAIRN_STORE_ACCESS_KEY": ADMIN[0], "CAIRN_STORE_SECRET_KEY": ADMIN[1]}
    out = subprocess.run([binary, "migrate-meta"], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout)
    assert report["rows"] > 1200 and not os.path.exists(os.path.join(data, "index.redb"))
    assert os.path.exists(report["embedded_index_kept_as"])

    st = Store(binary, data, st.port, env={"CAIRN_STORE_META": url}).start()
    s3 = st.client()
    assert s3.get_object(Bucket="migrated", Key="doc")["Body"].read() == b"two"
    assert s3.get_object(Bucket="migrated", Key="doc", VersionId=v1)["Body"].read() == b"one"
    assert s3.get_object_tagging(Bucket="migrated", Key="doc")["TagSet"] == [{"Key": "k", "Value": "v"}]
    keys = [o["Key"] for p in s3.get_paginator("list_objects_v2").paginate(Bucket="migrated", Prefix="many/") for o in p["Contents"]]
    assert keys == [f"many/{i:05d}" for i in range(1200)]
    st.client(("kept-key", key["secret_key"])).head_object(Bucket="migrated", Key="doc")  # keys came along
    _, audit = admin(st, "GET", "audit?after=0&limit=10000")
    from test_audit import cairn_verify

    assert cairn_verify(audit["entries"])["valid"] is True  # the hash chain survived the move
    st.stop()
    # Migrating again into a database that has data is refused.
    out = subprocess.run([binary, "migrate-meta"], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode != 0


def test_a_metadata_database_belongs_to_one_store(binary, tmp_path):
    url = new_database()
    a = Store(binary, str(tmp_path / "a"), _free_port(), env={"CAIRN_STORE_META": url}).start()
    a.client().create_bucket(Bucket="only-mine")
    a.stop()
    b = Store(binary, str(tmp_path / "b"), _free_port(), env={"CAIRN_STORE_META": url})
    with pytest.raises(RuntimeError):
        b.start()  # another data directory: its blobs are not this database's
    a.start()
    assert [x["Name"] for x in a.client().list_buckets()["Buckets"]] == ["only-mine"]
    a.stop()
