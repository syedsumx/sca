"""Object lock: lock-enabled buckets, default retention, GOVERNANCE and COMPLIANCE retention, legal hold,
and that no path (client deletes, batch deletes, lifecycle) removes a protected version."""

import datetime as dt
import json
import time
import uuid

import pytest
from botocore.exceptions import ClientError

from conftest import ANALYST


def code(exc_info) -> str:
    return exc_info.value.response["Error"]["Code"]


def soon(seconds: float) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)


@pytest.fixture
def locked(s3):
    name = "lock-" + uuid.uuid4().hex[:12]
    s3.create_bucket(Bucket=name, ObjectLockEnabledForBucket=True)
    return name


def test_lock_enabled_bucket(s3, locked):
    assert s3.get_bucket_versioning(Bucket=locked)["Status"] == "Enabled"
    assert s3.get_object_lock_configuration(Bucket=locked)["ObjectLockConfiguration"]["ObjectLockEnabled"] == "Enabled"
    with pytest.raises(ClientError) as ei:
        s3.put_bucket_versioning(Bucket=locked, VersioningConfiguration={"Status": "Suspended"})
    assert code(ei) == "InvalidBucketState"


def test_lock_configuration_on_existing_buckets(s3, bucket):
    with pytest.raises(ClientError) as ei:
        s3.get_object_lock_configuration(Bucket=bucket)
    assert code(ei) == "ObjectLockConfigurationNotFoundError"
    cfg = {"ObjectLockEnabled": "Enabled", "Rule": {"DefaultRetention": {"Mode": "GOVERNANCE", "Days": 1}}}
    with pytest.raises(ClientError) as ei:
        s3.put_object_lock_configuration(Bucket=bucket, ObjectLockConfiguration=cfg)
    assert code(ei) == "InvalidBucketState"  # versioning must be on first
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    s3.put_object_lock_configuration(Bucket=bucket, ObjectLockConfiguration=cfg)
    got = s3.get_object_lock_configuration(Bucket=bucket)["ObjectLockConfiguration"]
    assert got["Rule"]["DefaultRetention"] == {"Mode": "GOVERNANCE", "Days": 1}


def test_default_retention_applies_to_new_versions(s3, locked):
    s3.put_object_lock_configuration(Bucket=locked, ObjectLockConfiguration={
        "ObjectLockEnabled": "Enabled", "Rule": {"DefaultRetention": {"Mode": "GOVERNANCE", "Days": 1}}})
    v = s3.put_object(Bucket=locked, Key="k", Body=b"x")["VersionId"]
    head = s3.head_object(Bucket=locked, Key="k")
    assert head["ObjectLockMode"] == "GOVERNANCE" and head["ObjectLockRetainUntilDate"] > soon(23 * 3600)
    with pytest.raises(ClientError) as ei:
        s3.delete_object(Bucket=locked, Key="k", VersionId=v)
    assert code(ei) == "AccessDenied"
    s3.delete_object(Bucket=locked, Key="k", VersionId=v, BypassGovernanceRetention=True)  # admin may bypass


def test_compliance_retention_cannot_be_shortened_or_bypassed(s3, locked):
    until = soon(4)
    v = s3.put_object(Bucket=locked, Key="c", Body=b"x", ObjectLockMode="COMPLIANCE", ObjectLockRetainUntilDate=until)["VersionId"]
    r = s3.get_object_retention(Bucket=locked, Key="c", VersionId=v)["Retention"]
    assert r["Mode"] == "COMPLIANCE"
    for call in (
        lambda: s3.delete_object(Bucket=locked, Key="c", VersionId=v, BypassGovernanceRetention=True),
        lambda: s3.put_object_retention(Bucket=locked, Key="c", VersionId=v, BypassGovernanceRetention=True,
                                        Retention={"Mode": "GOVERNANCE", "RetainUntilDate": soon(60)}),
        lambda: s3.put_object_retention(Bucket=locked, Key="c", VersionId=v, BypassGovernanceRetention=True,
                                        Retention={"Mode": "COMPLIANCE", "RetainUntilDate": soon(1)}),
    ):
        with pytest.raises(ClientError) as ei:
            call()
        assert code(ei) == "AccessDenied"
    # Extending is allowed.
    s3.put_object_retention(Bucket=locked, Key="c", VersionId=v, Retention={"Mode": "COMPLIANCE", "RetainUntilDate": soon(5)})
    # A plain delete only adds a delete marker; the locked version stays readable.
    s3.delete_object(Bucket=locked, Key="c")
    assert s3.get_object(Bucket=locked, Key="c", VersionId=v)["Body"].read() == b"x"
    res = s3.delete_objects(Bucket=locked, Delete={"Objects": [{"Key": "c", "VersionId": v}]})
    assert res["Errors"][0]["Code"] == "AccessDenied"
    time.sleep(6)
    s3.delete_object(Bucket=locked, Key="c", VersionId=v)  # retention has expired


def test_governance_bypass_needs_permission(store, s3, locked):
    v = s3.put_object(Bucket=locked, Key="g", Body=b"x", ObjectLockMode="GOVERNANCE", ObjectLockRetainUntilDate=soon(3600))["VersionId"]
    s3.put_bucket_policy(Bucket=locked, Policy=json.dumps({"Statement": [{
        "Effect": "Allow", "Principal": {"AWS": ANALYST[0]}, "Action": ["s3:DeleteObjectVersion", "s3:PutObjectRetention"],
        "Resource": f"arn:aws:s3:::{locked}/*"}]}))
    analyst = store.client(key=ANALYST)
    with pytest.raises(ClientError) as ei:
        analyst.delete_object(Bucket=locked, Key="g", VersionId=v, BypassGovernanceRetention=True)
    assert code(ei) == "AccessDenied"  # no s3:BypassGovernanceRetention
    with pytest.raises(ClientError):
        analyst.put_object_retention(Bucket=locked, Key="g", VersionId=v, BypassGovernanceRetention=True, Retention={})
    s3.put_object_retention(Bucket=locked, Key="g", VersionId=v, BypassGovernanceRetention=True, Retention={})  # remove
    with pytest.raises(ClientError) as ei:
        s3.get_object_retention(Bucket=locked, Key="g", VersionId=v)
    assert code(ei) == "NoSuchObjectLockConfiguration"
    analyst.delete_object(Bucket=locked, Key="g", VersionId=v)


def test_legal_hold(s3, locked):
    v = s3.put_object(Bucket=locked, Key="h", Body=b"x", ObjectLockLegalHoldStatus="ON")["VersionId"]
    assert s3.get_object_legal_hold(Bucket=locked, Key="h")["LegalHold"]["Status"] == "ON"
    assert s3.head_object(Bucket=locked, Key="h")["ObjectLockLegalHoldStatus"] == "ON"
    with pytest.raises(ClientError) as ei:
        s3.delete_object(Bucket=locked, Key="h", VersionId=v, BypassGovernanceRetention=True)
    assert code(ei) == "AccessDenied"
    s3.put_object_legal_hold(Bucket=locked, Key="h", VersionId=v, LegalHold={"Status": "OFF"})
    s3.delete_object(Bucket=locked, Key="h", VersionId=v)


@pytest.mark.parametrize("kw,err", [
    ({"ObjectLockMode": "GOVERNANCE"}, "InvalidArgument"),
    ({"ObjectLockMode": "FOREVER", "ObjectLockRetainUntilDate": "2099-01-01T00:00:00Z"}, "InvalidArgument"),
    ({"ObjectLockMode": "GOVERNANCE", "ObjectLockRetainUntilDate": "2001-01-01T00:00:00Z"}, "InvalidArgument"),
])
def test_invalid_lock_headers(s3, locked, kw, err):
    with pytest.raises(ClientError) as ei:
        s3.put_object(Bucket=locked, Key="bad", Body=b"x", **kw)
    assert code(ei) == err


def test_lock_on_multipart_and_copy(s3, locked):
    up = s3.create_multipart_upload(Bucket=locked, Key="m", ObjectLockMode="COMPLIANCE", ObjectLockRetainUntilDate=soon(3600))
    p = s3.upload_part(Bucket=locked, Key="m", UploadId=up["UploadId"], PartNumber=1, Body=b"x")
    v = s3.complete_multipart_upload(Bucket=locked, Key="m", UploadId=up["UploadId"],
                                     MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": p["ETag"]}]})["VersionId"]
    assert s3.get_object_retention(Bucket=locked, Key="m", VersionId=v)["Retention"]["Mode"] == "COMPLIANCE"
    c = s3.copy_object(Bucket=locked, Key="copy", CopySource={"Bucket": locked, "Key": "m"})
    assert "ObjectLockMode" not in s3.head_object(Bucket=locked, Key="copy", VersionId=c["VersionId"])  # not copied


def test_lifecycle_never_expires_locked_versions(custom_store):
    st = custom_store({"CAIRN_STORE_LIFECYCLE_INTERVAL": "1", "CAIRN_STORE_LIFECYCLE_DAY_SECONDS": "1"})
    s3 = st.client()
    s3.create_bucket(Bucket="worm", ObjectLockEnabledForBucket=True)
    v = s3.put_object(Bucket="worm", Key="k", Body=b"x", ObjectLockMode="COMPLIANCE", ObjectLockRetainUntilDate=soon(3600))["VersionId"]
    s3.put_object(Bucket="worm", Key="k", Body=b"y")  # v becomes noncurrent
    s3.put_bucket_lifecycle_configuration(Bucket="worm", LifecycleConfiguration={"Rules": [
        {"ID": "nc", "Status": "Enabled", "Filter": {"Prefix": ""}, "NoncurrentVersionExpiration": {"NoncurrentDays": 1}}]})
    time.sleep(4)
    assert s3.get_object(Bucket="worm", Key="k", VersionId=v)["Body"].read() == b"x"


def test_lock_survives_restart(fresh_store):
    st = fresh_store.start()
    s3 = st.client()
    s3.create_bucket(Bucket="worm2", ObjectLockEnabledForBucket=True)
    v = s3.put_object(Bucket="worm2", Key="k", Body=b"x", ObjectLockLegalHoldStatus="ON")["VersionId"]
    st.stop(kill=True)
    s3 = st.start().client()
    with pytest.raises(ClientError) as ei:
        s3.delete_object(Bucket="worm2", Key="k", VersionId=v)
    assert code(ei) == "AccessDenied"
