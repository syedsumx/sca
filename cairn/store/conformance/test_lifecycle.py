"""Lifecycle rules: configuration, and the worker expiring objects, versions and uploads. These stores run
with a one-second lifecycle "day" and a pass every second."""

import time

import pytest
import requests
from botocore.exceptions import ClientError

FAST = {"CAIRN_STORE_LIFECYCLE_INTERVAL": "1", "CAIRN_STORE_LIFECYCLE_DAY_SECONDS": "1"}


def code(exc_info) -> str:
    return exc_info.value.response["Error"]["Code"]


def rules(*rs):
    return {"Rules": list(rs)}


def wait_for(cond, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.25)
    return False


def keys(s3, bucket):
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=bucket).get("Contents", []))


def test_lifecycle_configuration_round_trip(s3, bucket):
    with pytest.raises(ClientError) as ei:
        s3.get_bucket_lifecycle_configuration(Bucket=bucket)
    assert code(ei) == "NoSuchLifecycleConfiguration"
    cfg = rules(
        {"ID": "tmp", "Status": "Enabled", "Filter": {"And": {"Prefix": "tmp/", "Tags": [{"Key": "tier", "Value": "scratch"}]}},
         "Expiration": {"Days": 7}},
        {"ID": "versions", "Status": "Enabled", "Filter": {"Prefix": ""},
         "NoncurrentVersionExpiration": {"NoncurrentDays": 30, "NewerNoncurrentVersions": 2}},
        {"ID": "uploads", "Status": "Disabled", "Filter": {"Prefix": ""}, "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3}},
    )
    s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration=cfg)
    got = s3.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
    assert [r["ID"] for r in got] == ["tmp", "versions", "uploads"]
    assert got[0]["Filter"]["And"]["Tags"] == [{"Key": "tier", "Value": "scratch"}] and got[0]["Expiration"]["Days"] == 7
    assert got[1]["NoncurrentVersionExpiration"] == {"NoncurrentDays": 30, "NewerNoncurrentVersions": 2}
    assert got[2]["Status"] == "Disabled"
    s3.delete_bucket_lifecycle(Bucket=bucket)
    with pytest.raises(ClientError):
        s3.get_bucket_lifecycle_configuration(Bucket=bucket)


@pytest.mark.parametrize("rule,err", [
    ({"ID": "t", "Status": "Enabled", "Filter": {"Prefix": ""},
      "Transitions": [{"Days": 30, "StorageClass": "GLACIER"}]}, "InvalidStorageClass"),  # no such tier
    ({"ID": "z", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 0}}, "InvalidArgument"),
    ({"ID": "n", "Status": "Enabled", "Filter": {"Prefix": ""}}, "InvalidRequest"),
])
def test_invalid_lifecycle_rules(s3, bucket, rule, err):
    with pytest.raises(ClientError) as ei:
        s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration=rules(rule))
    assert code(ei) == err


def test_expiration_header(s3, bucket):
    s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration=rules(
        {"ID": "logs", "Status": "Enabled", "Filter": {"Prefix": "logs/"}, "Expiration": {"Days": 30}}))
    r = s3.put_object(Bucket=bucket, Key="logs/a.log", Body=b"x")
    assert 'rule-id="logs"' in r["Expiration"]
    assert 'rule-id="logs"' in s3.head_object(Bucket=bucket, Key="logs/a.log")["Expiration"]
    assert "Expiration" not in s3.put_object(Bucket=bucket, Key="data/a", Body=b"x")


def test_worker_expires_matching_objects_only(custom_store):
    st = custom_store(FAST)
    s3 = st.client()
    s3.create_bucket(Bucket="life")
    s3.put_bucket_lifecycle_configuration(Bucket="life", LifecycleConfiguration=rules(
        {"ID": "tmp", "Status": "Enabled", "Filter": {"And": {"Prefix": "tmp/", "Tags": [{"Key": "tier", "Value": "scratch"}]}},
         "Expiration": {"Days": 2}},
        {"ID": "off", "Status": "Disabled", "Filter": {"Prefix": "keep/"}, "Expiration": {"Days": 1}},
    ))
    s3.put_object(Bucket="life", Key="tmp/a", Body=b"x", Tagging="tier=scratch")
    s3.put_object(Bucket="life", Key="tmp/b", Body=b"x")  # no tag: kept
    s3.put_object(Bucket="life", Key="keep/c", Body=b"x")  # disabled rule: kept
    assert keys(s3, "life") == ["keep/c", "tmp/a", "tmp/b"]
    assert wait_for(lambda: keys(s3, "life") == ["keep/c", "tmp/b"])
    time.sleep(2.5)
    assert keys(s3, "life") == ["keep/c", "tmp/b"]
    metrics = requests.get(f"{st.endpoint}/-/metrics", timeout=5).text
    assert "cairn_store_lifecycle_expired_total 1" in metrics


def test_worker_expires_noncurrent_versions_and_markers(custom_store):
    st = custom_store(FAST)
    s3 = st.client()
    s3.create_bucket(Bucket="vers")
    s3.put_bucket_versioning(Bucket="vers", VersioningConfiguration={"Status": "Enabled"})
    ids = [s3.put_object(Bucket="vers", Key="k", Body=str(i).encode())["VersionId"] for i in range(4)]
    s3.put_object(Bucket="vers", Key="gone", Body=b"x")
    s3.delete_object(Bucket="vers", Key="gone")
    s3.put_bucket_lifecycle_configuration(Bucket="vers", LifecycleConfiguration=rules(
        {"ID": "nc", "Status": "Enabled", "Filter": {"Prefix": "k"},
         "NoncurrentVersionExpiration": {"NoncurrentDays": 1, "NewerNoncurrentVersions": 1}},
        {"ID": "dm", "Status": "Enabled", "Filter": {"Prefix": "gone"}, "Expiration": {"ExpiredObjectDeleteMarker": True}},
        {"ID": "ncg", "Status": "Enabled", "Filter": {"Prefix": "gone"}, "NoncurrentVersionExpiration": {"NoncurrentDays": 1}},
    ))

    def versions(key):
        lv = s3.list_object_versions(Bucket="vers", Prefix=key)
        return [v["VersionId"] for v in lv.get("Versions", [])], [m["VersionId"] for m in lv.get("DeleteMarkers", [])]

    # The current version and the newest noncurrent one stay; older ones go.
    assert wait_for(lambda: versions("k")[0] == [ids[3], ids[2]])
    assert s3.get_object(Bucket="vers", Key="k")["Body"].read() == b"3"
    # "gone": its old version expires, then the delete marker left alone is removed.
    assert wait_for(lambda: versions("gone") == ([], []))


def test_worker_aborts_stale_uploads(custom_store):
    st = custom_store(FAST)
    s3 = st.client()
    s3.create_bucket(Bucket="mpu")
    up = s3.create_multipart_upload(Bucket="mpu", Key="big")["UploadId"]
    s3.upload_part(Bucket="mpu", Key="big", UploadId=up, PartNumber=1, Body=b"x" * 1024)
    s3.put_bucket_lifecycle_configuration(Bucket="mpu", LifecycleConfiguration=rules(
        {"ID": "abort", "Status": "Enabled", "Filter": {"Prefix": ""}, "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}))
    assert wait_for(lambda: not s3.list_multipart_uploads(Bucket="mpu").get("Uploads"))
    with pytest.raises(ClientError) as ei:
        s3.list_parts(Bucket="mpu", Key="big", UploadId=up)
    assert code(ei) == "NoSuchUpload"


def test_expiration_counts_from_the_latest_write(custom_store):
    """Rewriting an object restarts its expiration clock (the worker also deletes only the version it
    scanned, by If-Match on its ETag)."""
    st = custom_store(FAST)
    s3 = st.client()
    s3.create_bucket(Bucket="race")
    s3.put_bucket_lifecycle_configuration(Bucket="race", LifecycleConfiguration=rules(
        {"ID": "x", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 3}}))
    s3.put_object(Bucket="race", Key="k", Body=b"old")
    time.sleep(1.5)
    s3.put_object(Bucket="race", Key="k", Body=b"new")
    time.sleep(2)
    assert s3.get_object(Bucket="race", Key="k")["Body"].read() == b"new"  # not due yet: written 2 s ago
