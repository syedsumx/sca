"""Encryption at rest: SSE-S3, SSE-KMS (keyring keys), SSE-C, bucket default encryption, the store
default, key rotation, and that no plaintext reaches the disk."""

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

SECRET = b"cairn-plaintext-marker-" * 400  # ~9 KiB, easy to look for on disk


def code(exc_info) -> str:
    return exc_info.value.response["Error"]["Code"]


def b64key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def keyring_store(custom_store, tmp_path, keys, sse_s3_key, **env):
    path = tmp_path / f"keyring-{sse_s3_key}.json"
    path.write_text(json.dumps({"keys": keys, "sse_s3_key": sse_s3_key}))
    return custom_store({"CAIRN_STORE_KEYRING_FILE": str(path), "CAIRN_STORE_SSE_C_ALLOW_HTTP": "true", **env})


def disk_contains(store, needle: bytes) -> bool:
    for p in Path(store.data).rglob("*"):
        if p.is_file() and p.name != "index.redb" and needle in p.read_bytes():
            return True
    return False


def customer_key():
    raw = os.urandom(32)
    return {"SSECustomerAlgorithm": "AES256", "SSECustomerKey": raw}, raw


def test_without_a_keyring_encryption_is_refused(s3, bucket):
    with pytest.raises(ClientError) as ei:
        s3.put_object(Bucket=bucket, Key="k", Body=b"x", ServerSideEncryption="AES256")
    assert code(ei) == "InvalidRequest"
    with pytest.raises(ClientError) as ei:
        s3.get_bucket_encryption(Bucket=bucket)
    assert code(ei) == "ServerSideEncryptionConfigurationNotFoundError"
    assert "ServerSideEncryption" not in s3.put_object(Bucket=bucket, Key="k", Body=b"x")


def test_sse_c_needs_tls(s3, bucket):
    kw, _ = customer_key()
    with pytest.raises(ClientError) as ei:
        s3.put_object(Bucket=bucket, Key="k", Body=b"x", **kw)
    assert code(ei) == "InvalidRequest"


def test_store_default_encrypts_everything(custom_store, tmp_path):
    st = keyring_store(custom_store, tmp_path, {"k1": b64key()}, "k1")
    s3 = st.client()
    s3.create_bucket(Bucket="enc")
    r = s3.put_object(Bucket="enc", Key="plain-request", Body=SECRET)
    assert r["ServerSideEncryption"] == "AES256"
    got = s3.get_object(Bucket="enc", Key="plain-request")
    assert got["Body"].read() == SECRET and got["ServerSideEncryption"] == "AES256"
    assert s3.get_object(Bucket="enc", Key="plain-request", Range="bytes=100-199")["Body"].read() == SECRET[100:200]
    assert not disk_contains(st, SECRET[:200])
    enc = s3.get_bucket_encryption(Bucket="enc")["ServerSideEncryptionConfiguration"]["Rules"][0]
    assert enc["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"


def test_kms_keys_multipart_and_copy(custom_store, tmp_path):
    st = keyring_store(custom_store, tmp_path, {"k1": b64key(), "finance": b64key()}, "k1")
    s3 = st.client()
    s3.create_bucket(Bucket="kms")
    r = s3.put_object(Bucket="kms", Key="a", Body=SECRET, ServerSideEncryption="aws:kms", SSEKMSKeyId="finance")
    assert r["ServerSideEncryption"] == "aws:kms" and r["SSEKMSKeyId"] == "finance"
    with pytest.raises(ClientError) as ei:
        s3.put_object(Bucket="kms", Key="b", Body=b"x", ServerSideEncryption="aws:kms", SSEKMSKeyId="nope")
    assert code(ei) == "KMS.NotFoundException"
    part = SECRET * 600  # ~5.4 MiB, so the first part meets the minimum size
    up = s3.create_multipart_upload(Bucket="kms", Key="mp", ServerSideEncryption="aws:kms", SSEKMSKeyId="finance")
    parts = [s3.upload_part(Bucket="kms", Key="mp", UploadId=up["UploadId"], PartNumber=i + 1, Body=b)["ETag"]
             for i, b in enumerate([part, b"tail"])]
    s3.complete_multipart_upload(Bucket="kms", Key="mp", UploadId=up["UploadId"],
                                 MultipartUpload={"Parts": [{"PartNumber": i + 1, "ETag": e} for i, e in enumerate(parts)]})
    got = s3.get_object(Bucket="kms", Key="mp", Range=f"bytes={len(part) - 3}-{len(part) + 1}")
    assert got["Body"].read() == part[-3:] + b"ta" and got["SSEKMSKeyId"] == "finance"
    c = s3.copy_object(Bucket="kms", Key="copied", CopySource={"Bucket": "kms", "Key": "a"}, ServerSideEncryption="AES256")
    assert c["ServerSideEncryption"] == "AES256"
    assert s3.get_object(Bucket="kms", Key="copied")["Body"].read() == SECRET
    assert not disk_contains(st, SECRET[:200])


def test_bucket_default_encryption(custom_store, tmp_path):
    st = keyring_store(custom_store, tmp_path, {"k1": b64key(), "team": b64key()}, "k1", CAIRN_STORE_ENCRYPT_BY_DEFAULT="false")
    s3 = st.client()
    s3.create_bucket(Bucket="dflt")
    assert "ServerSideEncryption" not in s3.put_object(Bucket="dflt", Key="clear", Body=b"x")
    s3.put_bucket_encryption(Bucket="dflt", ServerSideEncryptionConfiguration={"Rules": [
        {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": "team"}}]})
    r = s3.put_object(Bucket="dflt", Key="enc", Body=b"x")
    assert r["ServerSideEncryption"] == "aws:kms" and r["SSEKMSKeyId"] == "team"
    with pytest.raises(ClientError) as ei:
        s3.put_bucket_encryption(Bucket="dflt", ServerSideEncryptionConfiguration={"Rules": [
            {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": "unknown"}}]})
    assert code(ei) == "KMS.NotFoundException"
    s3.delete_bucket_encryption(Bucket="dflt")
    with pytest.raises(ClientError):
        s3.get_bucket_encryption(Bucket="dflt")


def test_customer_keys(custom_store, tmp_path):
    st = keyring_store(custom_store, tmp_path, {"k1": b64key()}, "k1")
    s3 = st.client()
    s3.create_bucket(Bucket="ssec")
    kw, raw = customer_key()
    r = s3.put_object(Bucket="ssec", Key="c", Body=SECRET, **kw)
    assert r["SSECustomerKeyMD5"] == base64.b64encode(hashlib.md5(raw).digest()).decode()
    assert s3.get_object(Bucket="ssec", Key="c", **kw)["Body"].read() == SECRET
    assert s3.head_object(Bucket="ssec", Key="c", **kw)["ContentLength"] == len(SECRET)
    with pytest.raises(ClientError) as ei:
        s3.get_object(Bucket="ssec", Key="c")
    assert code(ei) == "InvalidRequest"
    other, _ = customer_key()
    with pytest.raises(ClientError) as ei:
        s3.get_object(Bucket="ssec", Key="c", **other)
    assert code(ei) == "AccessDenied"
    # Copy out of SSE-C into the store's encryption.
    s3.copy_object(Bucket="ssec", Key="d", CopySource={"Bucket": "ssec", "Key": "c"},
                   CopySourceSSECustomerAlgorithm="AES256", CopySourceSSECustomerKey=raw)
    assert s3.get_object(Bucket="ssec", Key="d")["Body"].read() == SECRET
    # Multipart with a customer key: every part sends it.
    up = s3.create_multipart_upload(Bucket="ssec", Key="m", **kw)
    e = s3.upload_part(Bucket="ssec", Key="m", UploadId=up["UploadId"], PartNumber=1, Body=b"part", **kw)["ETag"]
    with pytest.raises(ClientError):
        s3.upload_part(Bucket="ssec", Key="m", UploadId=up["UploadId"], PartNumber=2, Body=b"part", **other)
    s3.complete_multipart_upload(Bucket="ssec", Key="m", UploadId=up["UploadId"],
                                 MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": e}]})
    assert s3.get_object(Bucket="ssec", Key="m", **kw)["Body"].read() == b"part"
    assert not disk_contains(st, SECRET[:200])
    assert not disk_contains(st, raw)  # the customer key is never stored


def test_key_rotation(custom_store, tmp_path):
    old, new = b64key(), b64key()
    st = keyring_store(custom_store, tmp_path, {"old": old}, "old")
    s3 = st.client()
    s3.create_bucket(Bucket="rot")
    s3.put_object(Bucket="rot", Key="before", Body=b"written with the old key")
    st.stop()
    (tmp_path / "keyring-new.json").write_text(json.dumps({"keys": {"old": old, "new": new}, "sse_s3_key": "new"}))
    st.extra_env["CAIRN_STORE_KEYRING_FILE"] = str(tmp_path / "keyring-new.json")
    s3 = st.start().client()
    s3.put_object(Bucket="rot", Key="after", Body=b"written with the new key")
    assert s3.get_object(Bucket="rot", Key="before")["Body"].read() == b"written with the old key"
    st.stop()
    (tmp_path / "keyring-only-new.json").write_text(json.dumps({"keys": {"new": new}, "sse_s3_key": "new"}))
    st.extra_env["CAIRN_STORE_KEYRING_FILE"] = str(tmp_path / "keyring-only-new.json")
    s3 = st.start().client()
    assert s3.get_object(Bucket="rot", Key="after")["Body"].read() == b"written with the new key"
    with pytest.raises(ClientError) as ei:
        s3.get_object(Bucket="rot", Key="before")
    assert code(ei) == "KMS.DisabledException"  # its key was removed from the keyring
