"""The audit trail (Cairn's hash-chain format) and server access logs."""

import hashlib
import json
import sys
import time
from pathlib import Path

import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

from conftest import ADMIN, REGION, SCOPED

GENESIS = "0" * 64


def cairn_verify(entries):
    """Cairn's own verifier (cairn.common.audit.verify_entries) when it can be imported, else the same
    algorithm: sha256(prev_hash || json.dumps(body, sort_keys=True, separators=(",", ":")))."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    try:
        from cairn.common.audit import verify_entries
        return verify_entries(entries)
    except ImportError:
        prev = None
        for e in entries:
            body = {k: e.get(k) for k in ("id", "ts", "category", "action", "actor", "subject", "details")}
            if prev is not None and e["prev_hash"] != prev:
                return {"valid": False}
            canon = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
            if hashlib.sha256(e["prev_hash"].encode() + canon).hexdigest() != e["hash"]:
                return {"valid": False}
            prev = e["hash"]
        return {"valid": True, "entries_checked": len(entries), "head": prev}


def export(store, after=0):
    req = AWSRequest(method="GET", url=f"{store.endpoint}/-/admin/v1/audit?after={after}&format=jsonl")
    S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
    r = requests.get(req.url, headers=dict(req.headers), timeout=10)
    assert r.status_code == 200
    return [json.loads(line) for line in r.text.splitlines() if line.strip()]


def set_quota(store, bucket, body):
    data = json.dumps(body).encode()
    req = AWSRequest(method="PUT", url=f"{store.endpoint}/-/admin/v1/buckets/{bucket}/quota", data=data)
    S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
    assert requests.put(req.url, data=data, headers=dict(req.headers), timeout=10).status_code == 200


def test_changes_are_recorded_in_a_verifiable_chain(fresh_store):
    st = fresh_store.start()
    s3 = st.client()
    s3.create_bucket(Bucket="aud")
    s3.put_bucket_policy(Bucket="aud", Policy=json.dumps({"Statement": [{"Effect": "Deny", "Principal": "*", "Action": "s3:DeleteBucket",
                                                                         "Resource": "arn:aws:s3:::aud"}]}))
    s3.put_bucket_versioning(Bucket="aud", VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_tagging(Bucket="aud", Tagging={"TagSet": [{"Key": "owner", "Value": "Zoë"}]})  # non-ASCII in the chain
    set_quota(st, "aud", {"max_bytes": 1 << 30})
    s3.create_bucket(Bucket="aud-lock", ObjectLockEnabledForBucket=True)
    v = s3.put_object(Bucket="aud-lock", Key="k", Body=b"x", ObjectLockLegalHoldStatus="ON")["VersionId"]
    s3.put_object_legal_hold(Bucket="aud-lock", Key="k", VersionId=v, LegalHold={"Status": "OFF"})
    with pytest.raises(ClientError):
        st.client(key=SCOPED).get_object(Bucket="aud", Key="secret")  # refused: recorded
    s3.delete_bucket_policy(Bucket="aud")
    entries = export(st)
    actions = [(e["action"], e["subject"]) for e in entries]
    assert actions[:5] == [("bucket.create", "aud"), ("config.policy.put", "aud"), ("versioning.enabled", "aud"),
                           ("config.tagging.put", "aud"), ("quota.put", "aud")]
    assert ("bucket.create", "aud-lock") in actions and ("object.legal_hold.off", "aud-lock/k") in actions
    denied = [e for e in entries if e["action"] == "request.denied"]
    assert denied and denied[0]["actor"] == SCOPED[0] and denied[0]["details"]["operation"] == "GetObject"
    assert actions[-1] == ("config.policy.delete", "aud")
    assert entries[0]["prev_hash"] == GENESIS and all(e["actor"] for e in entries)
    res = cairn_verify(entries)
    assert res["valid"] and res["entries_checked"] == len(entries)

    # Exports continue from the last head; the chain survives a restart.
    head = entries[-1]["hash"]
    st.stop(kill=True)
    s3 = st.start().client()
    s3.delete_bucket_tagging(Bucket="aud")
    more = export(st, after=entries[-1]["seq"])
    assert more[0]["prev_hash"] == head and cairn_verify(entries + more)["valid"]

    # Tampering is detected by the offline verifier.
    st.stop()
    import subprocess
    ok = subprocess.run([st.binary, "audit-verify", "-data", st.data], capture_output=True, text=True)
    assert ok.returncode == 0 and json.loads(ok.stdout)["valid"] is True
    forged = [dict(e) for e in entries]
    forged[1]["actor"] = "someone-else"
    assert cairn_verify(forged)["valid"] is False


def test_server_access_logs(custom_store):
    st = custom_store({"CAIRN_STORE_ACCESS_LOG_FLUSH_SECONDS": "1"})
    s3 = st.client()
    s3.create_bucket(Bucket="app")
    s3.create_bucket(Bucket="logs")
    s3.put_bucket_logging(Bucket="app", BucketLoggingStatus={"LoggingEnabled": {"TargetBucket": "logs", "TargetPrefix": "app/"}})
    assert s3.get_bucket_logging(Bucket="app")["LoggingEnabled"] == {"TargetBucket": "logs", "TargetPrefix": "app/"}
    s3.put_object(Bucket="app", Key="data/a b.csv", Body=b"x" * 10)
    s3.get_object(Bucket="app", Key="data/a b.csv")["Body"].read()
    with pytest.raises(ClientError):
        s3.get_object(Bucket="app", Key="missing")
    deadline, lines = time.time() + 10, []
    while time.time() < deadline and len(lines) < 3:
        time.sleep(0.5)
        lines = []
        for o in s3.list_objects_v2(Bucket="logs", Prefix="app/").get("Contents", []):
            lines += s3.get_object(Bucket="logs", Key=o["Key"])["Body"].read().decode().splitlines()
    ops = [ln.split(" ")[7] for ln in lines]
    assert "REST.PUT.OBJECT" in ops and "REST.GET.OBJECT" in ops, lines
    get = next(ln for ln in lines if " REST.GET.OBJECT data/a%20b.csv " in ln and " 200 " in ln)
    assert " cairnadmin " in get and " SigV4 " in get and " 10 10 " in get
    assert any(" 404 NoSuchKey " in ln for ln in lines)
    with pytest.raises(ClientError) as ei:
        s3.put_bucket_logging(Bucket="app", BucketLoggingStatus={"LoggingEnabled": {"TargetBucket": "nope", "TargetPrefix": ""}})
    assert ei.value.response["Error"]["Code"] == "InvalidTargetBucketForLogging"
    s3.put_bucket_logging(Bucket="app", BucketLoggingStatus={})
    assert "LoggingEnabled" not in s3.get_bucket_logging(Bucket="app")
