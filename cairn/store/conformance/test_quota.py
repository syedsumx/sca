"""Bucket quotas and usage through Cairn's admin API (/-/admin/v1, SigV4-signed by an admin key)."""

import json

import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

from conftest import ADMIN, REGION, SCOPED


def admin(store, method, path, body=None, key=ADMIN):
    data = json.dumps(body).encode() if body is not None else b""
    req = AWSRequest(method=method, url=f"{store.endpoint}/-/admin/v1/{path}", data=data,
                     headers={"Content-Type": "application/json"})
    S3SigV4Auth(Credentials(*key), "s3", REGION).add_auth(req)
    r = requests.request(method, req.url, data=data, headers=dict(req.headers), timeout=10)
    return r.status_code, r.json()


def code(exc_info) -> str:
    return exc_info.value.response["Error"]["Code"]


def test_admin_api_needs_an_admin_signature(store, bucket):
    assert requests.get(f"{store.endpoint}/-/admin/v1/stats", timeout=5).status_code == 403
    status, body = admin(store, "GET", "stats", key=SCOPED)
    assert status == 403 and body["error"] == "AccessDenied"
    status, body = admin(store, "GET", "stats")
    assert status == 200 and body["buckets"] >= 1
    assert admin(store, "GET", "nope")[0] == 404


def test_usage_is_reported(store, s3, bucket):
    s3.put_object(Bucket=bucket, Key="a", Body=b"x" * 100)
    s3.put_object(Bucket=bucket, Key="b", Body=b"x" * 23)
    status, b = admin(store, "GET", f"buckets/{bucket}")
    assert status == 200 and b["usage"] == {"bytes": 123, "objects": 2} and b["quota"] is None
    names = [x["name"] for x in admin(store, "GET", "buckets")[1]["buckets"]]
    assert bucket in names
    metrics = requests.get(f"{store.endpoint}/-/metrics", timeout=5).text
    assert f'cairn_store_bucket_bytes{{bucket="{bucket}"}} 123' in metrics


def test_byte_quota(store, s3, bucket):
    status, b = admin(store, "PUT", f"buckets/{bucket}/quota", {"max_bytes": 1000})
    assert status == 200 and b["quota"] == {"max_bytes": 1000}
    s3.put_object(Bucket=bucket, Key="a", Body=b"x" * 900)
    with pytest.raises(ClientError) as ei:
        s3.put_object(Bucket=bucket, Key="b", Body=b"x" * 101)
    assert code(ei) == "QuotaExceeded"
    s3.put_object(Bucket=bucket, Key="b", Body=b"x" * 100)  # exactly at the limit
    s3.put_object(Bucket=bucket, Key="a", Body=b"x" * 10)  # shrinking is always allowed
    up = s3.create_multipart_upload(Bucket=bucket, Key="m")
    with pytest.raises(ClientError) as ei:  # parts count while the upload is in progress
        s3.upload_part(Bucket=bucket, Key="m", UploadId=up["UploadId"], PartNumber=1, Body=b"x" * 1000)
    assert code(ei) == "QuotaExceeded"
    assert admin(store, "DELETE", f"buckets/{bucket}/quota")[1]["quota"] is None
    s3.put_object(Bucket=bucket, Key="c", Body=b"x" * 5000)


def test_object_quota(store, s3, bucket):
    admin(store, "PUT", f"buckets/{bucket}/quota", {"max_objects": 2})
    s3.put_object(Bucket=bucket, Key="1", Body=b"x")
    s3.put_object(Bucket=bucket, Key="2", Body=b"x")
    with pytest.raises(ClientError) as ei:
        s3.put_object(Bucket=bucket, Key="3", Body=b"x")
    assert code(ei) == "QuotaExceeded"
    s3.put_object(Bucket=bucket, Key="2", Body=b"y")  # an overwrite adds no object
    s3.delete_object(Bucket=bucket, Key="1")
    s3.put_object(Bucket=bucket, Key="3", Body=b"x")


def test_invalid_quota(store, bucket):
    assert admin(store, "PUT", f"buckets/{bucket}/quota", {})[0] == 400
    assert admin(store, "PUT", f"buckets/{bucket}/quota", {"max_bytes": "lots"})[0] == 400
    assert admin(store, "PUT", "buckets/no-such-bucket/quota", {"max_bytes": 1})[1]["error"] == "NoSuchBucket"


def test_usage_survives_restart(fresh_store):
    st = fresh_store.start()
    s3 = st.client()
    s3.create_bucket(Bucket="usage")
    s3.put_object(Bucket="usage", Key="k", Body=b"x" * 77)
    st.stop(kill=True)
    st.start()
    assert admin(st, "GET", "buckets/usage")[1]["usage"] == {"bytes": 77, "objects": 1}


def test_admin_command_line(store, s3, bucket):
    import os
    import subprocess

    if not getattr(store, "binary", None):
        pytest.skip("external store")
    env = {**os.environ, "CAIRN_STORE_ENDPOINT": store.endpoint, "CAIRN_STORE_ACCESS_KEY": ADMIN[0], "CAIRN_STORE_SECRET_KEY": ADMIN[1]}

    def cli(*args):
        r = subprocess.run([store.binary, "admin", *args], env=env, capture_output=True, text=True, timeout=20)
        return r.returncode, r.stdout

    code, out = cli("quota", bucket, "--max-objects", "7")
    assert code == 0 and json.loads(out)["quota"] == {"max_objects": 7}
    code, out = cli("bucket", bucket)
    assert code == 0 and json.loads(out)["name"] == bucket
    assert bucket in [b["name"] for b in json.loads(cli("buckets")[1])["buckets"]]
    assert json.loads(cli("stats")[1])["buckets"] >= 1
    code, out = cli("quota-remove", bucket)
    assert code == 0 and json.loads(out)["quota"] is None
    code, out = cli("audit", "--jsonl")
    assert code == 0 and any(json.loads(line)["action"] == "quota.delete" for line in out.splitlines() if line.strip())
    env["CAIRN_STORE_ACCESS_KEY"], env["CAIRN_STORE_SECRET_KEY"] = SCOPED
    code, out = cli("stats")
    assert code == 1 and json.loads(out)["error"] == "AccessDenied"
