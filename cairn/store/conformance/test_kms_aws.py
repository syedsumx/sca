"""Customer-managed keys in AWS KMS (or any service that speaks its API), with a real KMS-compatible
server (local-kms): objects encrypted under a key the store never holds, bound to their names through
the encryption context, unreadable while the customer disables the key and for good once it is deleted.
Needs docker and the nsmithuk/local-kms image."""

import json
import shutil
import socket
import subprocess
import time
import uuid

import boto3
import pytest
from botocore.exceptions import ClientError

from conftest import _free_port
from test_sse import disk_contains

IMAGE = "nsmithuk/local-kms:3"
REGION = "eu-west-2"


def _docker_ok():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ok(), reason=f"needs docker and {IMAGE}")


@pytest.fixture(scope="module")
def kms():
    port = _free_port()
    name = f"cairn-kms-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "host", "-e", f"PORT={port}",
                    "-e", f"KMS_REGION={REGION}", "-e", "KMS_ACCOUNT_ID=111122223333", IMAGE], check=True, capture_output=True)
    try:
        end = time.time() + 60
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), 0.3).close()
                break
            except OSError:
                assert time.time() < end, "local-kms did not start"
                time.sleep(0.3)
        url = f"http://127.0.0.1:{port}"
        client = boto3.client("kms", endpoint_url=url, region_name=REGION, aws_access_key_id="AKIDCUSTOMER",
                              aws_secret_access_key="customer-secret")
        yield url, client
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def code(ei):
    return ei.value.response["Error"]["Code"]


def kms_store(custom_store, tmp_path, url, key, ident="customer-b"):
    secret = tmp_path / f"kms-secret-{ident}"
    secret.write_text("store-secret-key\n")
    ring = tmp_path / f"keyring-{ident}.json"
    ring.write_text(json.dumps({"external": {ident: {"type": "aws-kms", "addr": url, "region": REGION, "key": key,
                                                     "access_key": "AKIDSTORE", "secret_key_file": str(secret),
                                                     "cache_seconds": 1}}}))
    return custom_store({"CAIRN_STORE_KEYRING_FILE": str(ring)})


def test_objects_are_encrypted_under_a_kms_key(custom_store, tmp_path, kms):
    url, client = kms
    key = client.create_key(Description="customer b")["KeyMetadata"]
    st = kms_store(custom_store, tmp_path, url, key["Arn"])
    s3 = st.client()
    s3.create_bucket(Bucket="acme-b")
    s3.put_bucket_encryption(Bucket="acme-b", ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "aws:kms", "KMSMasterKeyID": "customer-b"}}]})
    secret = b"customer-b's ledger " + uuid.uuid4().hex.encode()
    put = s3.put_object(Bucket="acme-b", Key="ledger.csv", Body=secret)
    assert put["ServerSideEncryption"] == "aws:kms" and put["SSEKMSKeyId"] == "customer-b"
    assert s3.get_object(Bucket="acme-b", Key="ledger.csv")["Body"].read() == secret
    assert not disk_contains(st, secret)
    s3.copy_object(Bucket="acme-b", Key="copy.csv", CopySource={"Bucket": "acme-b", "Key": "ledger.csv"})
    assert s3.get_object(Bucket="acme-b", Key="copy.csv")["Body"].read() == secret

    # The object's name is the encryption context: KMS itself refuses a wrapped key under another name.
    import base64
    import hashlib
    blob = client.encrypt(KeyId=key["KeyId"], Plaintext=b"x" * 64,
                          EncryptionContext={"cairn-object": hashlib.sha256(b"acme-b/ledger.csv").hexdigest()})["CiphertextBlob"]
    with pytest.raises(ClientError) as ei:
        client.decrypt(CiphertextBlob=blob, EncryptionContext={"cairn-object": hashlib.sha256(b"acme-b/other").hexdigest()})
    assert code(ei) == "InvalidCiphertextException"
    assert base64.b64encode(blob)

    # The customer disables the key: reads and writes stop (after the 1-second cache); enabled again, they work.
    client.disable_key(KeyId=key["KeyId"])
    time.sleep(1.2)
    with pytest.raises(ClientError) as ei:
        s3.get_object(Bucket="acme-b", Key="ledger.csv")
    assert code(ei) == "KMS.DisabledException"
    with pytest.raises(ClientError) as ei:
        s3.put_object(Bucket="acme-b", Key="more", Body=b"x")
    assert code(ei) == "KMS.DisabledException"
    client.enable_key(KeyId=key["KeyId"])
    assert s3.get_object(Bucket="acme-b", Key="ledger.csv")["Body"].read() == secret

    # Crypto-shredding: the key is scheduled for deletion, and nothing opens.
    client.schedule_key_deletion(KeyId=key["KeyId"], PendingWindowInDays=7)
    time.sleep(1.2)
    for k in ("ledger.csv", "copy.csv"):
        with pytest.raises(ClientError) as ei:
            s3.get_object(Bucket="acme-b", Key=k)
        assert code(ei) == "KMS.DisabledException"


def test_an_alias_names_the_key_and_another_key_opens_nothing(custom_store, tmp_path, kms):
    url, client = kms
    a = client.create_key()["KeyMetadata"]
    client.create_alias(AliasName="alias/cairn-c", TargetKeyId=a["KeyId"])
    st = kms_store(custom_store, tmp_path, url, "alias/cairn-c", "customer-c")
    s3 = st.client()
    s3.create_bucket(Bucket="cust-c")
    s3.put_object(Bucket="cust-c", Key="k", Body=b"by alias", ServerSideEncryption="aws:kms", SSEKMSKeyId="customer-c")
    assert s3.get_object(Bucket="cust-c", Key="k")["Body"].read() == b"by alias"
    # A keyring naming a key that does not exist: nothing is written.
    st2 = kms_store(custom_store, tmp_path, url, "arn:aws:kms:eu-west-2:111122223333:key/" + str(uuid.uuid4()), "customer-d")
    s3b = st2.client()
    s3b.create_bucket(Bucket="cust-d")
    with pytest.raises(ClientError) as ei:
        s3b.put_object(Bucket="cust-d", Key="k", Body=b"x", ServerSideEncryption="aws:kms", SSEKMSKeyId="customer-d")
    assert code(ei) == "KMS.DisabledException"


def test_a_bad_aws_key_config_stops_the_store(custom_store, tmp_path, kms):
    url, _ = kms
    ring = tmp_path / "bad.json"
    for bad in [{"type": "aws-kms", "addr": url, "key": "k"},                                  # no region
                {"type": "aws-kms", "addr": url, "region": REGION, "key": "k", "access_key": "AKID"},    # no secret
                {"type": "aws-kms", "addr": url, "region": REGION, "key": "k", "access_key": "AKID",
                 "secret_key_file": str(tmp_path / "missing")}]:
        ring.write_text(json.dumps({"external": {"x": bad}}))
        with pytest.raises(RuntimeError, match="did not start"):
            custom_store({"CAIRN_STORE_KEYRING_FILE": str(ring)})
