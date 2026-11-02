"""Customer-managed keys in HashiCorp Vault (Transit), with a real Vault: objects encrypted under a key the
store never holds, readable while Vault agrees, unreadable once the customer revokes the store's access or
deletes the key. Needs docker and the hashicorp/vault image."""

import json
import shutil
import socket
import subprocess
import time
import uuid

import pytest
import requests
from botocore.exceptions import ClientError

from conftest import _free_port
from test_sse import disk_contains

VAULT_IMAGE = "hashicorp/vault:1.20"


def _docker_ok():
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "image", "inspect", VAULT_IMAGE], capture_output=True)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _docker_ok(), reason=f"needs docker and {VAULT_IMAGE}")


class Vault:
    def __init__(self, addr):
        self.addr = addr

    def __call__(self, method, path, token="root", **kw):
        r = requests.request(method, f"{self.addr}/v1/{path}", headers={"X-Vault-Token": token}, timeout=10, **kw)
        assert r.status_code < 300, (path, r.status_code, r.text)
        return r.json() if r.text else {}

    def store_token(self, key):
        """A token that may only encrypt and decrypt with `key` (what the store gets)."""
        self("PUT", f"sys/policies/acl/store-{key}", json={"policy": (
            f'path "transit/encrypt/{key}" {{ capabilities = ["update"] }}\n'
            f'path "transit/decrypt/{key}" {{ capabilities = ["update"] }}')})
        return self("POST", "auth/token/create", json={"policies": [f"store-{key}"], "no_default_policy": True})["auth"]["client_token"]


@pytest.fixture(scope="module")
def vault():
    port = _free_port()
    name = f"cairn-vault-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "host", "-e", "SKIP_SETCAP=1",
                    VAULT_IMAGE, "server", "-dev", "-dev-root-token-id=root", f"-dev-listen-address=127.0.0.1:{port}"],
                   check=True, capture_output=True)
    try:
        v = Vault(f"http://127.0.0.1:{port}")
        end = time.time() + 60
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), 0.3).close()
                if requests.get(f"{v.addr}/v1/sys/health", timeout=2).status_code == 200:
                    break
            except (OSError, requests.RequestException):
                pass
            assert time.time() < end, "vault did not start"
            time.sleep(0.3)
        v("POST", "sys/mounts/transit", json={"type": "transit"})
        yield v
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def code(ei):
    return ei.value.response["Error"]["Code"]


def kms_store(custom_store, tmp_path, vault, key, token, **extra):
    vault("POST", f"transit/keys/{key}")
    token_file = tmp_path / f"vault-token-{key}"
    token_file.write_text(token or vault.store_token(key))
    ring = tmp_path / f"keyring-{key}.json"
    ring.write_text(json.dumps({"external": {key: {"type": "vault-transit", "addr": vault.addr, "key": key,
                                                   "token_file": str(token_file), "cache_seconds": 1, **extra}}}))
    return custom_store({"CAIRN_STORE_KEYRING_FILE": str(ring)}), token_file


def test_objects_are_encrypted_under_the_customers_key(custom_store, tmp_path, vault):
    st, token_file = kms_store(custom_store, tmp_path, vault, "customer-a", None)
    s3 = st.client()
    s3.create_bucket(Bucket="acme")
    s3.put_bucket_encryption(Bucket="acme", ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "aws:kms", "KMSMasterKeyID": "customer-a"}}]})
    secret = b"customer-a's ledger " + uuid.uuid4().hex.encode()
    put = s3.put_object(Bucket="acme", Key="ledger.csv", Body=secret)
    assert put["ServerSideEncryption"] == "aws:kms" and put["SSEKMSKeyId"] == "customer-a"
    assert s3.get_object(Bucket="acme", Key="ledger.csv")["Body"].read() == secret
    assert not disk_contains(st, secret)
    meta = b"".join(p.read_bytes() for p in __import__("pathlib").Path(st.data).rglob("*") if p.is_file())
    assert b"vault:v1:" in meta                                 # the data key as Vault encrypted it

    # A copy gets a data key of its own (bound to its own name), through Vault.
    s3.copy_object(Bucket="acme", Key="copy.csv", CopySource={"Bucket": "acme", "Key": "ledger.csv"})
    assert s3.get_object(Bucket="acme", Key="copy.csv")["Body"].read() == secret

    # Rotation in Vault: old objects still open, new ones use the new version.
    vault("POST", "transit/keys/customer-a/rotate")
    s3.put_object(Bucket="acme", Key="after-rotation", Body=b"new")
    time.sleep(1.2)
    assert s3.get_object(Bucket="acme", Key="ledger.csv")["Body"].read() == secret
    assert s3.get_object(Bucket="acme", Key="after-rotation")["Body"].read() == b"new"

    # The customer revokes the store's access: reads and writes stop (after the 1-second cache).
    vault("POST", "auth/token/revoke", json={"token": token_file.read_text()})
    time.sleep(1.2)
    with pytest.raises(ClientError) as ei:
        s3.get_object(Bucket="acme", Key="ledger.csv")
    assert code(ei) == "KMS.DisabledException"
    with pytest.raises(ClientError) as ei:
        s3.put_object(Bucket="acme", Key="more", Body=b"x")
    assert code(ei) == "KMS.DisabledException"

    # ... and gives it back with a new token (the file is read on each call): readable again.
    token_file.write_text(vault.store_token("customer-a"))
    assert s3.get_object(Bucket="acme", Key="ledger.csv")["Body"].read() == secret

    # Crypto-shredding: the key is deleted in Vault, and the data is gone for good.
    vault("POST", "transit/keys/customer-a/config", json={"deletion_allowed": True})
    vault("DELETE", "transit/keys/customer-a")
    time.sleep(1.2)
    for k in ("ledger.csv", "copy.csv", "after-rotation"):
        with pytest.raises(ClientError) as ei:
            s3.get_object(Bucket="acme", Key=k)
        assert code(ei) == "KMS.DisabledException"


def test_a_token_scoped_to_another_key_opens_nothing(custom_store, tmp_path, vault):
    vault("POST", "transit/keys/customer-b")
    st, _ = kms_store(custom_store, tmp_path, vault, "customer-c", vault.store_token("customer-b"))
    s3 = st.client()
    s3.create_bucket(Bucket="cust-c")
    with pytest.raises(ClientError) as ei:
        s3.put_object(Bucket="cust-c", Key="k", Body=b"x", ServerSideEncryption="aws:kms", SSEKMSKeyId="customer-c")
    assert code(ei) == "KMS.DisabledException"
    with pytest.raises(ClientError) as ei:                       # a key the keyring does not know
        s3.put_object(Bucket="cust-c", Key="k", Body=b"x", ServerSideEncryption="aws:kms", SSEKMSKeyId="customer-z")
    assert code(ei) == "KMS.NotFoundException"


def test_a_keyring_that_does_not_load_stops_the_store(custom_store, tmp_path, vault):
    ring = tmp_path / "bad.json"
    for bad in [{"type": "vault-transit", "addr": vault.addr, "key": "k", "token_file": str(tmp_path / "missing")},
                {"type": "aws-kms", "addr": vault.addr, "key": "k", "token": "t"},
                {"type": "vault-transit", "addr": vault.addr, "key": "../sys", "token": "t"}]:
        ring.write_text(json.dumps({"external": {"x": bad}}))
        with pytest.raises(RuntimeError, match="did not start"):
            custom_store({"CAIRN_STORE_KEYRING_FILE": str(ring)})
