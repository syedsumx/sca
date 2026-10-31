"""Storage tiers: lifecycle transitions move object data to another S3 endpoint (here a second store),
reads fetch it from there, and deletes remove it there. The stores run with a one-second lifecycle "day"."""

import base64
import hashlib
import io
import json
import os
import time

import pytest
import requests
from botocore.exceptions import ClientError

from conftest import ADMIN

FAST = {"CAIRN_STORE_LIFECYCLE_INTERVAL": "1", "CAIRN_STORE_LIFECYCLE_DAY_SECONDS": "1"}


def code(exc_info) -> str:
    return exc_info.value.response["Error"]["Code"]


def wait_for(cond, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if cond():
                return True
        except ClientError:
            pass
        time.sleep(0.3)
    return False


def rule(rid, prefix, **actions):
    return {"ID": rid, "Status": "Enabled", "Filter": {"Prefix": prefix}, **actions}


@pytest.fixture
def tiered(custom_store, tmp_path):
    cold = custom_store({})
    c3 = cold.client()
    c3.create_bucket(Bucket="cold")
    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps([{"storage_class": "STANDARD_IA", "endpoint": cold.endpoint, "bucket": "cold",
                                  "prefix": "lake/", "access_key": ADMIN[0], "secret_key": ADMIN[1]}]))
    keyring = tmp_path / "kr.json"
    keyring.write_text(json.dumps({"keys": {"k": base64.b64encode(os.urandom(32)).decode()}, "sse_s3_key": "k"}))
    src = custom_store({**FAST, "CAIRN_STORE_TIERS_FILE": str(tiers), "CAIRN_STORE_KEYRING_FILE": str(keyring)})
    s3 = src.client()
    s3.create_bucket(Bucket="data")
    return src, s3, c3


def tier_keys(c3):
    return [o["Key"] for o in c3.list_objects_v2(Bucket="cold").get("Contents", [])]


def test_configuration_names_only_configured_tiers(tiered):
    _, s3, _ = tiered
    for cls in ["GLACIER", "ONEZONE_IA"]:  # a restore class, and a class without a tier
        with pytest.raises(ClientError) as ei:
            s3.put_bucket_lifecycle_configuration(Bucket="data", LifecycleConfiguration={"Rules": [
                rule("t", "", Transitions=[{"Days": 30, "StorageClass": cls}])]})
        assert code(ei) == "InvalidStorageClass"
    with pytest.raises(ClientError) as ei:
        s3.put_bucket_lifecycle_configuration(Bucket="data", LifecycleConfiguration={"Rules": [
            rule("t", "", Transitions=[{"Days": 30, "StorageClass": "STANDARD_IA"},
                                       {"Days": 60, "StorageClass": "STANDARD_IA"}])]})
    assert code(ei) == "NotImplemented"
    cfg = {"Rules": [rule("t", "logs/", Transitions=[{"Days": 30, "StorageClass": "STANDARD_IA"}],
                          NoncurrentVersionTransitions=[{"NoncurrentDays": 7, "StorageClass": "STANDARD_IA"}],
                          Expiration={"Days": 365})]}
    s3.put_bucket_lifecycle_configuration(Bucket="data", LifecycleConfiguration=cfg)
    got = s3.get_bucket_lifecycle_configuration(Bucket="data")["Rules"][0]
    assert got["Transitions"] == [{"Days": 30, "StorageClass": "STANDARD_IA"}]
    assert got["NoncurrentVersionTransitions"] == [{"NoncurrentDays": 7, "StorageClass": "STANDARD_IA"}]
    # Writes cannot pick a tier (or any class this store cannot honour).
    for cls in ["STANDARD_IA", "GLACIER"]:
        with pytest.raises(ClientError) as ei:
            s3.put_object(Bucket="data", Key="x", Body=b"x", StorageClass=cls)
        assert code(ei) == "InvalidStorageClass"


def test_transition_read_and_delete(tiered):
    src, s3, c3 = tiered
    small = b"cold data " * 1000
    big = os.urandom(11 << 20)
    s3.put_object(Bucket="data", Key="old/small.txt", Body=small)
    s3.upload_fileobj(io.BytesIO(big), "data", "old/big.bin")  # multipart: several segments
    s3.put_object(Bucket="data", Key="hot/keep.txt", Body=b"stays local")
    s3.put_bucket_lifecycle_configuration(Bucket="data", LifecycleConfiguration={"Rules": [
        rule("cold", "old/", Transitions=[{"Days": 1, "StorageClass": "STANDARD_IA"}])]})
    assert wait_for(lambda: all(s3.head_object(Bucket="data", Key=k).get("StorageClass") == "STANDARD_IA"
                                for k in ["old/small.txt", "old/big.bin"]))
    assert "StorageClass" not in s3.head_object(Bucket="data", Key="hot/keep.txt")
    listed = {o["Key"]: o["StorageClass"] for o in s3.list_objects_v2(Bucket="data")["Contents"]}
    assert listed == {"hot/keep.txt": "STANDARD", "old/big.bin": "STANDARD_IA", "old/small.txt": "STANDARD_IA"}
    # The tier holds the blobs as stored: encrypted, under the prefix.
    stored = tier_keys(c3)
    assert len(stored) >= 3 and all(k.startswith("lake/") for k in stored)
    for k in stored:
        assert b"cold data" not in c3.get_object(Bucket="cold", Key=k)["Body"].read()
    # Reads are transparent, ranges included.
    assert s3.get_object(Bucket="data", Key="old/small.txt")["Body"].read() == small
    got = s3.get_object(Bucket="data", Key="old/big.bin")["Body"].read()
    assert hashlib.sha256(got).digest() == hashlib.sha256(big).digest()
    part = s3.get_object(Bucket="data", Key="old/big.bin", Range="bytes=6291000-6292000")["Body"].read()
    assert part == big[6291000:6292001]
    metrics = requests.get(f"{src.endpoint}/-/metrics").text
    assert "cairn_store_lifecycle_transitioned_total 2" in metrics
    assert "cairn_store_tier_transitions_total 2" in metrics
    # Copying back with STANDARD brings an object home; the tier's copy goes.
    s3.copy_object(Bucket="data", Key="old/small.txt", CopySource={"Bucket": "data", "Key": "old/small.txt"},
                   StorageClass="STANDARD")
    h = s3.head_object(Bucket="data", Key="old/small.txt")
    assert "StorageClass" not in h
    s3.put_bucket_lifecycle_configuration(Bucket="data", LifecycleConfiguration={"Rules": [
        rule("none", "nothing/", Transitions=[{"Days": 1, "StorageClass": "STANDARD_IA"}])]})
    assert s3.get_object(Bucket="data", Key="old/small.txt")["Body"].read() == small
    # Deleting the tiered object removes its blobs from the tier.
    s3.delete_object(Bucket="data", Key="old/big.bin")
    assert wait_for(lambda: tier_keys(c3) == [])


def test_noncurrent_versions_transition(tiered):
    _, s3, c3 = tiered
    s3.put_bucket_versioning(Bucket="data", VersioningConfiguration={"Status": "Enabled"})
    v1 = s3.put_object(Bucket="data", Key="doc.txt", Body=b"first")["VersionId"]
    s3.put_object(Bucket="data", Key="doc.txt", Body=b"second")
    s3.put_bucket_lifecycle_configuration(Bucket="data", LifecycleConfiguration={"Rules": [
        rule("old-versions", "", NoncurrentVersionTransitions=[{"NoncurrentDays": 1, "StorageClass": "STANDARD_IA"}])]})
    assert wait_for(lambda: s3.head_object(Bucket="data", Key="doc.txt", VersionId=v1).get("StorageClass") == "STANDARD_IA")
    assert "StorageClass" not in s3.head_object(Bucket="data", Key="doc.txt")
    assert s3.get_object(Bucket="data", Key="doc.txt", VersionId=v1)["Body"].read() == b"first"
    versions = {v["VersionId"]: v["StorageClass"] for v in s3.list_object_versions(Bucket="data")["Versions"]}
    assert versions[v1] == "STANDARD_IA"
    assert len(tier_keys(c3)) == 1
