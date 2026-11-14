"""Times to live: x-cairn-ttl on PutObject, CopyObject and multipart uploads, and a bucket default (with
0 to opt out). Expired objects are gone at once for reads, listings and the file API, then deleted by the
lifecycle worker (their versions too, in versioned buckets). Bad durations, Object Lock buckets and a
store without lifecycle processing are refused."""

import io
import json
import os
import time

import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

from conftest import ADMIN, REGION

FAST = {"CAIRN_STORE_LIFECYCLE_INTERVAL": "1"}


def admin(st, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = AWSRequest(method=method, url=f"{st.endpoint}/-/admin/v1/{path}", data=data)
    S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
    r = requests.request(method, req.url, data=data, headers=dict(req.headers), timeout=30)
    return r.status_code, (r.json() if r.content else None)


def with_ttl(s3, ttl):
    """Adds x-cairn-ttl to this client's requests."""
    def add(request, **_):
        request.headers["x-cairn-ttl"] = ttl
    s3.meta.events.register_first("before-sign.s3.*", add)
    return s3


def missing(fn, **kw):
    with pytest.raises(ClientError) as ei:
        fn(**kw)
    return ei.value.response["Error"]["Code"]


def keys(s3, bucket):
    return [o["Key"] for o in s3.list_objects_v2(Bucket=bucket).get("Contents", [])]


def wait_for(cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.3)
    return False


def test_objects_expire_and_are_swept(custom_store):
    st = custom_store(FAST)
    s3 = st.client()
    s3.create_bucket(Bucket="scratch")
    t3 = with_ttl(st.client(), "2s")
    t3.put_object(Bucket="scratch", Key="short.txt", Body=b"brief")
    s3.put_object(Bucket="scratch", Key="keep.txt", Body=b"forever")
    h = s3.head_object(Bucket="scratch", Key="short.txt")
    assert "x-cairn-expires" in h["ResponseMetadata"]["HTTPHeaders"]
    assert "x-cairn-expires" not in s3.head_object(Bucket="scratch", Key="keep.txt")["ResponseMetadata"]["HTTPHeaders"]
    # Copies and multipart uploads count from when they are made.
    t3.copy_object(Bucket="scratch", Key="copy.txt", CopySource={"Bucket": "scratch", "Key": "keep.txt"})
    t3.upload_fileobj(io.BytesIO(os.urandom(12 << 20)), "scratch", "big.bin")
    assert sorted(keys(s3, "scratch")) == ["big.bin", "copy.txt", "keep.txt", "short.txt"]
    time.sleep(2.5)
    # Gone at once, everywhere, before the sweep has run.
    for k in ["short.txt", "copy.txt", "big.bin"]:
        assert missing(s3.get_object, Bucket="scratch", Key=k) == "NoSuchKey"
    assert keys(s3, "scratch") == ["keep.txt"]
    assert requests.get(f"{st.endpoint}/-/files/scratch/short.txt", auth=None, timeout=10).status_code in (403, 404)
    # The sweep deletes them; the metric counts them.
    assert wait_for(lambda: "cairn_store_lifecycle_ttl_expired_total 3" in requests.get(f"{st.endpoint}/-/metrics", timeout=10).text)
    # A key whose object expired can be written again at once.
    s3.put_object(Bucket="scratch", Key="short.txt", Body=b"again")
    assert s3.get_object(Bucket="scratch", Key="short.txt")["Body"].read() == b"again"


def test_versions_expire_with_their_bytes(custom_store):
    st = custom_store(FAST)
    s3 = st.client()
    s3.create_bucket(Bucket="vers")
    s3.put_bucket_versioning(Bucket="vers", VersioningConfiguration={"Status": "Enabled"})
    s3.put_object(Bucket="vers", Key="k", Body=b"v1 stays")
    with_ttl(st.client(), "1s").put_object(Bucket="vers", Key="k", Body=b"v2 expires")
    time.sleep(1.5)
    assert missing(s3.get_object, Bucket="vers", Key="k") == "NoSuchKey"
    # The expired version is deleted itself (no delete marker left behind); v1 remains as it was.
    assert wait_for(lambda: len(s3.list_object_versions(Bucket="vers").get("Versions", [])) == 1)
    vs = s3.list_object_versions(Bucket="vers")
    assert not vs.get("DeleteMarkers")
    assert s3.get_object(Bucket="vers", Key="k", VersionId=vs["Versions"][0]["VersionId"])["Body"].read() == b"v1 stays"


def test_bucket_default_and_opting_out(custom_store):
    st = custom_store(FAST)
    s3 = st.client()
    s3.create_bucket(Bucket="tmp")
    code, b = admin(st, "PUT", "buckets/tmp/ttl", {"ttl": "1s"})
    assert code == 200 and b["ttl_seconds"] == 1
    s3.put_object(Bucket="tmp", Key="default.txt", Body=b"x")
    with_ttl(st.client(), "0").put_object(Bucket="tmp", Key="kept.txt", Body=b"y")
    with_ttl(st.client(), "1h").put_object(Bucket="tmp", Key="hour.txt", Body=b"z")
    time.sleep(1.5)
    assert sorted(keys(s3, "tmp")) == ["hour.txt", "kept.txt"]
    code, b = admin(st, "DELETE", "buckets/tmp/ttl")
    assert code == 200 and b["ttl_seconds"] == 0
    s3.put_object(Bucket="tmp", Key="after.txt", Body=b"w")
    time.sleep(1.5)
    assert "after.txt" in keys(s3, "tmp")
    assert admin(st, "PUT", "buckets/tmp/ttl", {"ttl": "forever"})[0] == 400
    actions = [e["action"] for e in admin(st, "GET", "audit?after=0")[1]["entries"] if e["action"].startswith("bucket.ttl")]
    assert actions == ["bucket.ttl.put", "bucket.ttl.delete"]


def test_refusals(custom_store):
    st = custom_store(FAST)
    s3 = st.client()
    s3.create_bucket(Bucket="plain")
    for bad in ["soon", "1.5h", "-5", "3y", "5000000d"]:
        assert missing(with_ttl(st.client(), bad).put_object, Bucket="plain", Key="k", Body=b"x") == "InvalidArgument"
    s3.create_bucket(Bucket="locked", ObjectLockEnabledForBucket=True)
    assert missing(with_ttl(st.client(), "1h").put_object, Bucket="locked", Key="k", Body=b"x") == "InvalidRequest"
    assert admin(st, "PUT", "buckets/locked/ttl", {"seconds": 60})[0] == 400
    off = custom_store({"CAIRN_STORE_LIFECYCLE_INTERVAL": "0"})
    off.client().create_bucket(Bucket="never-swept")
    assert missing(with_ttl(off.client(), "1h").put_object, Bucket="never-swept", Key="k", Body=b"x") == "InvalidRequest"
