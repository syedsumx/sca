"""Times to live: x-cairn-ttl on PutObject, CopyObject and multipart uploads, and a bucket default (with
0 to opt out). Expired objects are gone at once for reads, listings and the file API, then deleted by the
lifecycle worker (the expired version itself, in versioned buckets, leaving a delete marker). Bad durations, Object Lock buckets and a
store without lifecycle processing are refused."""

import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

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
    """In a versioned bucket the sweep must not bring an older version back: the key stays gone (a delete
    marker), the expired version's bytes go, and earlier versions are kept as noncurrent versions."""
    st = custom_store(FAST)
    s3 = st.client()
    s3.create_bucket(Bucket="back")
    s3.put_bucket_versioning(Bucket="back", VersioningConfiguration={"Status": "Enabled"})
    v1 = s3.put_object(Bucket="back", Key="k", Body=b"old")["VersionId"]
    with_ttl(st.client(), "1s").put_object(Bucket="back", Key="k", Body=b"temporary")
    time.sleep(1.5)
    assert missing(s3.get_object, Bucket="back", Key="k") == "NoSuchKey"
    assert wait_for(lambda: "cairn_store_lifecycle_ttl_expired_total 1" in requests.get(f"{st.endpoint}/-/metrics", timeout=10).text)
    assert missing(s3.get_object, Bucket="back", Key="k") == "NoSuchKey"
    assert keys(s3, "back") == []
    vs = s3.list_object_versions(Bucket="back")
    assert [v["VersionId"] for v in vs["Versions"]] == [v1]
    assert len(vs.get("DeleteMarkers", [])) == 1
    assert s3.get_object(Bucket="back", Key="k", VersionId=v1)["Body"].read() == b"old"


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


def test_held_and_retained_versions_outlive_their_time(custom_store):
    """Object Lock switched on after objects were written with a time to live: a version under legal hold or
    retention (governance too) stays readable and is not deleted when its time runs out."""
    st = custom_store(FAST)
    s3 = st.client()
    s3.create_bucket(Bucket="later")
    s3.put_bucket_versioning(Bucket="later", VersioningConfiguration={"Status": "Enabled"})
    t3 = with_ttl(st.client(), "2s")
    vh = t3.put_object(Bucket="later", Key="held", Body=b"evidence")["VersionId"]
    vg = t3.put_object(Bucket="later", Key="governed", Body=b"kept")["VersionId"]
    t3.put_object(Bucket="later", Key="free", Body=b"goes")
    s3.put_object_lock_configuration(Bucket="later", ObjectLockConfiguration={"ObjectLockEnabled": "Enabled"})
    s3.put_object_legal_hold(Bucket="later", Key="held", VersionId=vh, LegalHold={"Status": "ON"})
    s3.put_object_retention(Bucket="later", Key="governed", VersionId=vg,
                            Retention={"Mode": "GOVERNANCE", "RetainUntilDate": time.time() + 3600})
    assert wait_for(lambda: "cairn_store_lifecycle_ttl_expired_total 1" in requests.get(f"{st.endpoint}/-/metrics", timeout=10).text)
    time.sleep(2)  # more passes
    assert sorted(keys(s3, "later")) == ["governed", "held"]
    assert s3.get_object(Bucket="later", Key="held")["Body"].read() == b"evidence"
    assert s3.get_object(Bucket="later", Key="governed", VersionId=vg)["Body"].read() == b"kept"
    assert s3.get_object_legal_hold(Bucket="later", Key="held", VersionId=vh)["LegalHold"]["Status"] == "ON"
    # Once the hold is lifted, the time to live applies again.
    s3.put_object_legal_hold(Bucket="later", Key="held", VersionId=vh, LegalHold={"Status": "OFF"})
    assert missing(s3.get_object, Bucket="later", Key="held") == "NoSuchKey"


def test_object_lock_refused_while_a_bucket_default_is_set(custom_store):
    """Turning Object Lock on would make every write to a bucket with a default time to live fail."""
    st = custom_store(FAST)
    s3 = st.client()
    s3.create_bucket(Bucket="dflt")
    s3.put_bucket_versioning(Bucket="dflt", VersioningConfiguration={"Status": "Enabled"})
    assert admin(st, "PUT", "buckets/dflt/ttl", {"ttl": "1h"})[0] == 200
    cfg = {"ObjectLockEnabled": "Enabled"}
    assert missing(s3.put_object_lock_configuration, Bucket="dflt", ObjectLockConfiguration=cfg) == "InvalidRequest"
    s3.put_object(Bucket="dflt", Key="k", Body=b"x")  # writes keep working
    assert admin(st, "DELETE", "buckets/dflt/ttl")[0] == 200
    s3.put_object_lock_configuration(Bucket="dflt", ObjectLockConfiguration=cfg)
    s3.put_object(Bucket="dflt", Key="k", Body=b"y")


def test_expired_key_counts_as_absent_for_conditional_writes(custom_store):
    """Before the sweep has run, an expired object is gone for conditional writes too."""
    st = custom_store({"CAIRN_STORE_LIFECYCLE_INTERVAL": "3600"})
    s3 = st.client()
    s3.create_bucket(Bucket="cond")
    etag = with_ttl(st.client(), "1s").put_object(Bucket="cond", Key="m.json", Body=b"{}")["ETag"]
    time.sleep(1.5)
    assert missing(s3.put_object, Bucket="cond", Key="m.json", Body=b"{1}", IfMatch=etag) in ("PreconditionFailed", "NoSuchKey")
    s3.put_object(Bucket="cond", Key="m.json", Body=b"{2}", IfNoneMatch="*")
    assert s3.get_object(Bucket="cond", Key="m.json")["Body"].read() == b"{2}"
    # Metadata changes do not reach an expired object either.
    with_ttl(st.client(), "1s").put_object(Bucket="cond", Key="t", Body=b"x")
    time.sleep(1.5)
    tags = {"TagSet": [{"Key": "a", "Value": "b"}]}
    assert missing(s3.put_object_tagging, Bucket="cond", Key="t", Tagging=tags) == "NoSuchKey"


def test_sweep_keeps_an_object_rewritten_after_it_was_listed(custom_store):
    """The sweep lists expired objects, then deletes them one by one. An object written again in between
    (same bytes, so the same ETag, but no time to live) must be kept."""
    # Each delete is its own durable transaction, so deleting the page takes a while.
    st = custom_store({"CAIRN_STORE_LIFECYCLE_INTERVAL": "15", "CAIRN_STORE_FSYNC": "true"})
    s3 = st.client()
    s3.create_bucket(Bucket="race")
    t3 = with_ttl(st.client(), "1s")
    with ThreadPoolExecutor(16) as ex:
        list(ex.map(lambda i: t3.put_object(Bucket="race", Key=f"a{i:04d}", Body=b"filler"), range(999)))
    t3.put_object(Bucket="race", Key="zz", Body=b"same bytes")
    count = lambda: admin(st, "GET", "buckets/race")[1]["usage"]["objects"]  # noqa: E731
    assert count() == 1000
    # Wait for the pass to start deleting (it has listed the page, zz last), then write zz again.
    end = time.time() + 30
    while count() == 1000 and time.time() < end:
        time.sleep(0.01)
    left = count()
    s3.put_object(Bucket="race", Key="zz", Body=b"same bytes")
    assert left > 100, f"the pass went too fast to test ({left} left)"
    assert wait_for(lambda: count() <= 1, timeout=60)
    time.sleep(1)
    assert s3.get_object(Bucket="race", Key="zz")["Body"].read() == b"same bytes"


def test_bucket_default_applies_to_browser_uploads(custom_store):
    """POST (browser form) uploads are new objects too: they get the bucket's default time to live."""
    st = custom_store(FAST)
    s3 = st.client()
    s3.create_bucket(Bucket="drop")
    assert admin(st, "PUT", "buckets/drop/ttl", {"ttl": "1s"})[0] == 200
    form = s3.generate_presigned_post("drop", "upload.txt", ExpiresIn=300)
    r = requests.post(form["url"], data=form["fields"], files={"file": ("upload.txt", b"temporary")}, timeout=30)
    assert r.status_code in (200, 204), r.text
    assert "x-cairn-expires" in s3.head_object(Bucket="drop", Key="upload.txt")["ResponseMetadata"]["HTTPHeaders"]
    time.sleep(1.5)
    assert missing(s3.get_object, Bucket="drop", Key="upload.txt") == "NoSuchKey"


def test_a_bucket_created_again_is_swept(custom_store):
    """Deleting a bucket drops its configuration, including the mark that sends the sweep there; a bucket
    created again under the same name must be marked (and swept) again."""
    st = custom_store(FAST)
    s3 = st.client()
    t3 = with_ttl(st.client(), "1s")
    metric = lambda: requests.get(f"{st.endpoint}/-/metrics", timeout=10).text  # noqa: E731
    s3.create_bucket(Bucket="again")
    t3.put_object(Bucket="again", Key="a", Body=b"x")
    assert wait_for(lambda: "cairn_store_lifecycle_ttl_expired_total 1" in metric())
    s3.delete_bucket(Bucket="again")
    s3.create_bucket(Bucket="again")
    t3.put_object(Bucket="again", Key="b", Body=b"y")
    assert wait_for(lambda: "cairn_store_lifecycle_ttl_expired_total 2" in metric())
    s3.delete_bucket(Bucket="again")  # empty: the expired object's version is really gone
