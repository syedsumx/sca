"""Bucket replication to another store: new versions (small, multipart, encrypted at the source),
metadata and tags, delete markers, rule filters, status headers, and a destination that is down for a while."""

import base64
import datetime
import urllib.request
import hashlib
import json
import os
import time

import pytest
from botocore.exceptions import ClientError

from conftest import ADMIN


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


RULES = {"Role": "arn:cairn:store:::dr", "Rules": [
    {"ID": "rep", "Priority": 1, "Status": "Enabled", "Filter": {"Prefix": "rep/"},
     "DeleteMarkerReplication": {"Status": "Enabled"}, "Destination": {"Bucket": "arn:aws:s3:::replica"}}]}


@pytest.fixture
def pair(custom_store, tmp_path):
    dest = custom_store({})
    d3 = dest.client()
    d3.create_bucket(Bucket="replica")
    d3.put_bucket_versioning(Bucket="replica", VersioningConfiguration={"Status": "Enabled"})
    targets = tmp_path / "repl.json"
    targets.write_text(json.dumps([{"id": "dr", "endpoint": dest.endpoint, "access_key": ADMIN[0], "secret_key": ADMIN[1]}]))
    keyring = tmp_path / "kr.json"
    keyring.write_text(json.dumps({"keys": {"k": base64.b64encode(os.urandom(32)).decode()}, "sse_s3_key": "k"}))
    src = custom_store({"CAIRN_STORE_REPLICATION_TARGETS_FILE": str(targets), "CAIRN_STORE_KEYRING_FILE": str(keyring),
                        "CAIRN_STORE_REPLICATION_MULTIPART_BYTES": str(8 << 20), "CAIRN_STORE_REPLICATION_PART_BYTES": str(5 << 20)})
    s3 = src.client()
    s3.create_bucket(Bucket="primary")
    return src, s3, dest, d3


def test_configuration(pair):
    _, s3, _, _ = pair
    with pytest.raises(ClientError) as ei:
        s3.put_bucket_replication(Bucket="primary", ReplicationConfiguration=RULES)
    assert code(ei) == "InvalidRequest"  # versioning first
    s3.put_bucket_versioning(Bucket="primary", VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_replication(Bucket="primary", ReplicationConfiguration=RULES)
    got = s3.get_bucket_replication(Bucket="primary")["ReplicationConfiguration"]
    assert got["Role"] == RULES["Role"] and got["Rules"][0]["Destination"]["Bucket"] == "arn:aws:s3:::replica"
    bad = json.loads(json.dumps(RULES))
    bad["Role"] = "arn:cairn:store:::nowhere"
    with pytest.raises(ClientError) as ei:
        s3.put_bucket_replication(Bucket="primary", ReplicationConfiguration=bad)
    assert code(ei) == "InvalidArgument"
    bad = json.loads(json.dumps(RULES))
    bad["Rules"][0]["ExistingObjectReplication"] = {"Status": "Enabled"}
    with pytest.raises(ClientError) as ei:
        s3.put_bucket_replication(Bucket="primary", ReplicationConfiguration=bad)
    assert code(ei) == "NotImplemented"
    s3.delete_bucket_replication(Bucket="primary")
    with pytest.raises(ClientError):
        s3.get_bucket_replication(Bucket="primary")


def test_objects_are_replicated(pair):
    _, s3, _, d3 = pair
    s3.put_bucket_versioning(Bucket="primary", VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_replication(Bucket="primary", ReplicationConfiguration=RULES)
    small = b"hello replica"
    s3.put_object(Bucket="primary", Key="rep/small.txt", Body=small, ContentType="text/plain", Metadata={"source": "lake"},
                  Tagging="tier=gold")
    s3.put_object(Bucket="primary", Key="local/only.txt", Body=b"stays here")  # outside the rule
    big = os.urandom(12 << 20)
    s3.upload_fileobj(__import__("io").BytesIO(big), "primary", "rep/big.bin")
    assert s3.head_object(Bucket="primary", Key="rep/small.txt")["ServerSideEncryption"] == "AES256"
    assert wait_for(lambda: d3.head_object(Bucket="replica", Key="rep/big.bin")["ContentLength"] == len(big))
    got = d3.get_object(Bucket="replica", Key="rep/small.txt")
    assert got["Body"].read() == small and got["ContentType"] == "text/plain" and got["Metadata"] == {"source": "lake"}
    assert d3.get_object_tagging(Bucket="replica", Key="rep/small.txt")["TagSet"] == [{"Key": "tier", "Value": "gold"}]
    assert hashlib.sha256(d3.get_object(Bucket="replica", Key="rep/big.bin")["Body"].read()).digest() == hashlib.sha256(big).digest()
    assert wait_for(lambda: s3.head_object(Bucket="primary", Key="rep/small.txt").get("ReplicationStatus") == "COMPLETED")
    assert "ReplicationStatus" not in s3.head_object(Bucket="primary", Key="local/only.txt")
    with pytest.raises(ClientError):
        d3.head_object(Bucket="replica", Key="local/only.txt")
    # Delete markers follow.
    s3.delete_object(Bucket="primary", Key="rep/small.txt")
    assert wait_for(lambda: _gone(d3, "rep/small.txt"))


def _gone(d3, key):
    try:
        d3.head_object(Bucket="replica", Key=key)
        return False
    except ClientError as e:
        return e.response["ResponseMetadata"]["HTTPStatusCode"] == 404


def test_destination_outage_delays_but_loses_nothing(pair):
    _, s3, dest, _ = pair
    s3.put_bucket_versioning(Bucket="primary", VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_replication(Bucket="primary", ReplicationConfiguration=RULES)
    dest.stop()
    for i in range(3):
        s3.put_object(Bucket="primary", Key=f"rep/while-down-{i}", Body=str(i).encode())
    time.sleep(2)
    assert s3.head_object(Bucket="primary", Key="rep/while-down-0").get("ReplicationStatus") == "PENDING"
    d3 = dest.start().client()
    assert wait_for(lambda: all(d3.get_object(Bucket="replica", Key=f"rep/while-down-{i}")["Body"].read() == str(i).encode()
                                for i in range(3)), timeout=60)


def test_object_lock_is_replicated(pair):
    _, s3, _, d3 = pair
    s3.delete_bucket(Bucket="primary")
    s3.create_bucket(Bucket="primary", ObjectLockEnabledForBucket=True)
    d3.create_bucket(Bucket="locked", ObjectLockEnabledForBucket=True)
    rules = json.loads(json.dumps(RULES))
    rules["Rules"][0]["Destination"]["Bucket"] = "arn:aws:s3:::locked"
    s3.put_bucket_replication(Bucket="primary", ReplicationConfiguration=rules)
    until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
    for key, body in [("rep/kept.txt", b"keep me"), ("rep/kept-big.bin", os.urandom(9 << 20))]:
        s3.put_object(Bucket="primary", Key=key, Body=body, ObjectLockMode="GOVERNANCE", ObjectLockRetainUntilDate=until,
                      ObjectLockLegalHoldStatus="ON", ChecksumAlgorithm="SHA256")
        assert wait_for(lambda k=key: s3.head_object(Bucket="primary", Key=k).get("ReplicationStatus") == "COMPLETED")
        h = d3.head_object(Bucket="locked", Key=key)
        assert h["ObjectLockMode"] == "GOVERNANCE" and h["ObjectLockLegalHoldStatus"] == "ON"
        assert abs(h["ObjectLockRetainUntilDate"].timestamp() - until.timestamp()) < 2
        assert h["ContentLength"] == len(body)


def test_refused_copies_are_marked_failed(pair):
    _, s3, _, _ = pair
    s3.put_bucket_versioning(Bucket="primary", VersioningConfiguration={"Status": "Enabled"})
    rules = json.loads(json.dumps(RULES))
    rules["Rules"][0]["Destination"]["Bucket"] = "arn:aws:s3:::no-such-bucket"
    s3.put_bucket_replication(Bucket="primary", ReplicationConfiguration=rules)
    s3.put_object(Bucket="primary", Key="rep/lost.txt", Body=b"x")
    assert wait_for(lambda: s3.head_object(Bucket="primary", Key="rep/lost.txt").get("ReplicationStatus") == "FAILED")
    metrics = urllib.request.urlopen(f"{pair[0].endpoint}/-/metrics").read().decode()
    assert "cairn_store_replication_dropped_total 1" in metrics
