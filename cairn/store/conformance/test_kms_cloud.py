"""Customer-managed keys in Azure Key Vault, Google Cloud KMS and KMIP servers (HSMs), each with a real
server: Lowkey Vault (an Azure Key Vault emulator), floci-gcp (a Google Cloud emulator with Cloud KMS) and
PyKMIP's KMIP server. Objects are encrypted under a key the store never holds; each sign-in the store
supports is used; turning the key off (disabling, revoking) stops reads.

Needs docker with nagyesta/lowkey-vault:3.4.0 and floci/floci-gcp:0.12.0, and PyKMIP (pykmip-server)."""

import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest
import requests
from botocore.exceptions import ClientError

from conftest import _free_port
from test_sse import disk_contains

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kmip_fixture  # noqa: E402

LOWKEY = "nagyesta/lowkey-vault:3.4.0"
FLOCI = "floci/floci-gcp:0.12.0"
# Lowkey Vault checks encrypt/decrypt on wrapkey/unwrapkey (Key Vault itself checks wrapKey/unwrapKey).
OPS = ["wrapKey", "unwrapKey", "encrypt", "decrypt"]
TENANT, CLIENT = "11111111-2222-3333-4444-555555555555", "66666666-7777-8888-9999-000000000000"


def _image(name):
    return subprocess.run(["docker", "image", "inspect", name], capture_output=True).returncode == 0


def code(ei):
    return ei.value.response["Error"]["Code"]


def _wait(port, what, proc=None):
    end = time.time() + 90
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close()
            return
        except OSError:
            assert time.time() < end, f"{what} did not start"
            time.sleep(0.3)


def keyring(tmp_path, ident, cfg):
    ring = tmp_path / f"keyring-{ident}.json"
    ring.write_text(json.dumps({"external": {ident: {"cache_seconds": 1, **cfg}}}))
    return str(ring)


def sse_bucket(s3, bucket, ident):
    s3.create_bucket(Bucket=bucket)
    s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "aws:kms", "KMSMasterKeyID": ident}}]})


class TokenServer:
    """An OAuth token endpoint that checks what the store sends (client credentials, a federated token, a
    signed JWT) and answers with a token; ``seen`` keeps each request's form."""

    def __init__(self, check):
        self.seen, outer = [], self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
                outer.seen.append({"path": self.path, **form})
                ok = check(self.path, form)
                body = json.dumps({"access_token": f"tok-{len(outer.seen)}", "expires_in": 3600} if ok else {"error": "invalid_client"})
                self.send_response(200 if ok else 401)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *a):
                pass

        self.http = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()


# ---------------------------------------------------------------------------- Azure Key Vault
@pytest.fixture(scope="module")
def lowkey(tmp_path_factory):
    if not _image(LOWKEY):
        pytest.skip(f"needs {LOWKEY}")
    for p in (8443, 8080):                        # its vault addresses name port 8443 exactly
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) == 0:
                pytest.skip(f"port {p} is taken")
    name = f"cairn-lowkey-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", "8443:8443", "-p", "8080:8080", LOWKEY],
                   check=True, capture_output=True)
    try:
        _wait(8443, "Lowkey Vault")
        ca = tmp_path_factory.mktemp("lowkey") / "lowkey.pem"
        for _ in range(60):
            out = subprocess.run(["openssl", "s_client", "-connect", "127.0.0.1:8443", "-servername", "localhost"],
                                 input=b"", capture_output=True).stdout
            if b"BEGIN CERTIFICATE" in out:
                break
            time.sleep(1)
        ca.write_bytes(out[out.index(b"-----BEGIN CERTIFICATE"):out.index(b"-----END CERTIFICATE-----") + 25] + b"\n")
        vault = "https://localhost:8443"

        def call(method, path, body=None):
            r = requests.request(method, f"{vault}{path}?api-version=7.4", json=body, verify=str(ca),
                                 headers={"Authorization": "Bearer emulator"})
            assert r.status_code < 300, r.text
            return r.json()

        yield vault, str(ca), call
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_azure_key_vault(custom_store, tmp_path, lowkey):
    vault, ca, call = lowkey
    first = call("POST", "/keys/cairn-c/create", {"kty": "RSA", "key_size": 2048, "key_ops": OPS})
    v1 = first["key"]["kid"].rsplit("/", 1)[1]
    secret = tmp_path / "client-secret"
    secret.write_text("app-secret\n")
    fed = tmp_path / "federated-token"
    fed.write_text("eyJ.federated.token\n")
    login = TokenServer(lambda path, f: path == f"/{TENANT}/oauth2/v2.0/token" and f.get("client_id") == CLIENT
                        and f.get("scope") == "https://vault.azure.net/.default" and f.get("grant_type") == "client_credentials"
                        and (f.get("client_secret") == "app-secret" or (
                            f.get("client_assertion") == "eyJ.federated.token"
                            and f.get("client_assertion_type") == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer")))
    base = {"type": "azure-keyvault", "addr": vault, "key": "cairn-c", "tenant_id": TENANT, "client_id": CLIENT, "auth_addr": login.url}
    # Each way the store signs in: an app's secret, a workload identity (AKS), the managed identity endpoint.
    for auth, extra in [("client-secret", {"client_secret_file": str(secret)}),
                        ("workload-identity", {"federated_token_file": str(fed)}),
                        ("managed-identity", {"auth_addr": "http://127.0.0.1:8080", "tenant_id": "", "client_id": ""})]:
        ring = keyring(tmp_path, f"az-{auth}", {**base, "auth": auth, **extra})
        st = custom_store({"CAIRN_STORE_KEYRING_FILE": ring, "CAIRN_STORE_CA_FILE": ca})
        s3 = st.client()
        bucket = f"az-{auth}"
        sse_bucket(s3, bucket, f"az-{auth}")
        body = f"ledger under Azure, {auth} ".encode() + uuid.uuid4().hex.encode()
        s3.put_object(Bucket=bucket, Key="ledger.csv", Body=body)
        assert s3.get_object(Bucket=bucket, Key="ledger.csv")["Body"].read() == body, auth
        assert not disk_contains(st, body)
    assert {x.get("client_secret") or x.get("client_assertion") for x in login.seen} == {"app-secret", "eyJ.federated.token"}
    # A new key version: new objects use it, old ones still open with the version that wrapped them.
    call("POST", "/keys/cairn-c/create", {"kty": "RSA", "key_size": 2048, "key_ops": OPS})
    time.sleep(1.2)                               # the store looks the current version up again after cache_seconds
    s3.put_object(Bucket=bucket, Key="after-rotation.csv", Body=b"new version")
    assert s3.get_object(Bucket=bucket, Key="ledger.csv")["Body"].read() == body
    assert s3.get_object(Bucket=bucket, Key="after-rotation.csv")["Body"].read() == b"new version"
    # The customer disables the version that wrapped the first object: it can no longer be read.
    call("PATCH", f"/keys/cairn-c/{v1}", {"attributes": {"enabled": False}})
    time.sleep(1.2)
    with pytest.raises(ClientError) as ei:
        s3.get_object(Bucket=bucket, Key="ledger.csv")
    assert code(ei) == "KMS.DisabledException"
    assert s3.get_object(Bucket=bucket, Key="after-rotation.csv")["Body"].read() == b"new version"


# ---------------------------------------------------------------------------- Google Cloud KMS
@pytest.fixture(scope="module")
def floci():
    if not _image(FLOCI):
        pytest.skip(f"needs {FLOCI}")
    port = _free_port()
    name = f"cairn-floci-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"{port}:4588", FLOCI], check=True, capture_output=True)
    try:
        _wait(port, "floci-gcp")
        url = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                requests.get(url, timeout=2)
                break
            except requests.RequestException:
                time.sleep(0.5)
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _gcp_key(url, ring, key):
    base = f"{url}/v1/projects/acme/locations/global/keyRings"
    requests.post(f"{base}?keyRingId={ring}", json={}).raise_for_status()
    requests.post(f"{base}/{ring}/cryptoKeys?cryptoKeyId={key}", json={"purpose": "ENCRYPT_DECRYPT"}).raise_for_status()
    return f"projects/acme/locations/global/keyRings/{ring}/cryptoKeys/{key}"


def test_google_cloud_kms(custom_store, tmp_path, floci):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = _gcp_key(floci, "cairn", "customer-d")
    # A service account key: the store signs a JWT with it (RS256), which the token endpoint checks with
    # the public key before it answers.
    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()

    def check(path, form):
        if form.get("grant_type") != "urn:ietf:params:oauth:grant-type:jwt-bearer":
            return False
        head, claims, sig = form["assertion"].split(".")
        pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
        try:
            pk.public_key().verify(pad(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
        except Exception:  # noqa: BLE001
            return False
        c = json.loads(pad(claims))
        return (json.loads(pad(head))["alg"] == "RS256" and c["iss"] == "cairn-store@acme.iam.gserviceaccount.com"
                and c["scope"] == "https://www.googleapis.com/auth/cloudkms" and c["exp"] > time.time())

    tokens = TokenServer(check)
    sa = tmp_path / "service-account.json"
    sa.write_text(json.dumps({"type": "service_account", "client_email": "cairn-store@acme.iam.gserviceaccount.com",
                              "private_key_id": "k1", "private_key": pem, "token_uri": tokens.url + "/token"}))
    ring = keyring(tmp_path, "customer-d", {"type": "gcp-kms", "addr": floci, "key": key, "auth": "service-account",
                                            "credentials_file": str(sa)})
    st = custom_store({"CAIRN_STORE_KEYRING_FILE": ring})
    s3 = st.client()
    sse_bucket(s3, "acme-d", "customer-d")
    body = b"customer-d's ledger " + uuid.uuid4().hex.encode()
    s3.put_object(Bucket="acme-d", Key="ledger.csv", Body=body)
    s3.copy_object(Bucket="acme-d", Key="copy.csv", CopySource={"Bucket": "acme-d", "Key": "ledger.csv"})
    assert s3.get_object(Bucket="acme-d", Key="ledger.csv")["Body"].read() == body
    assert s3.get_object(Bucket="acme-d", Key="copy.csv")["Body"].read() == body
    assert not disk_contains(st, body)
    assert tokens.seen and all(s["grant_type"].endswith("jwt-bearer") for s in tokens.seen)
    # A token file (e.g. kept fresh by a sidecar) works too; a keyring naming another key opens nothing.
    tok = tmp_path / "gcp-token"
    tok.write_text("tok\n")
    other = _gcp_key(floci, "other", "elsewhere")
    ring2 = keyring(tmp_path, "customer-d", {"type": "gcp-kms", "addr": floci, "key": other, "auth": "token-file",
                                             "token_file": str(tok)})
    st.stop()
    st.extra_env["CAIRN_STORE_KEYRING_FILE"] = ring2          # the same data, the keyring now names another key
    st.start()
    s3 = st.client()
    with pytest.raises(ClientError) as ei:
        s3.get_object(Bucket="acme-d", Key="ledger.csv")       # Cloud KMS refuses: not that key's ciphertext
    assert code(ei) == "KMS.DisabledException"
    s3.put_object(Bucket="acme-d", Key="new.csv", Body=b"under the token file")
    assert s3.get_object(Bucket="acme-d", Key="new.csv")["Body"].read() == b"under the token file"


# ---------------------------------------------------------------------------- KMIP (HSMs)
@pytest.fixture(scope="module")
def kmip(tmp_path_factory):
    if not kmip_fixture.server_bin():
        pytest.skip("needs PyKMIP (pykmip-server)")
    d = tmp_path_factory.mktemp("kmip")
    pki = kmip_fixture.make_pki(d, ("cairn-store", "intruder"))
    proc, port = kmip_fixture.start_server(d, pki)
    try:
        yield port, pki
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:            # PyKMIP's server can ignore SIGTERM while a connection is open
            proc.kill()
            proc.wait(10)


def test_kmip(custom_store, tmp_path, kmip):
    from kmip.core import enums

    port, pki = kmip
    owner = kmip_fixture.client(port, pki)
    uid = kmip_fixture.make_key(owner)
    cfg = {"type": "kmip", "addr": f"127.0.0.1:{port}", "key": uid, "ca_file": pki["ca"],
           "client_cert_file": pki["cairn-store"][0], "client_key_file": pki["cairn-store"][1]}
    st = custom_store({"CAIRN_STORE_KEYRING_FILE": keyring(tmp_path, "customer-e", cfg)})
    s3 = st.client()
    sse_bucket(s3, "acme-e", "customer-e")
    body = b"customer-e's ledger, kept by an HSM " + uuid.uuid4().hex.encode()
    s3.put_object(Bucket="acme-e", Key="ledger.csv", Body=body)
    assert s3.get_object(Bucket="acme-e", Key="ledger.csv")["Body"].read() == body
    assert not disk_contains(st, body)
    # Another client certificate may not use the key (the KMIP server's access control): nothing is written.
    cfg2 = {**cfg, "client_cert_file": pki["intruder"][0], "client_key_file": pki["intruder"][1]}
    st2 = custom_store({"CAIRN_STORE_KEYRING_FILE": keyring(tmp_path, "customer-e2", {**cfg2})})
    s3b = st2.client()
    sse_bucket(s3b, "acme-e2", "customer-e2")
    with pytest.raises(ClientError) as ei:
        s3b.put_object(Bucket="acme-e2", Key="k", Body=b"x")
    assert code(ei) == "KMS.DisabledException"
    # The customer revokes the key: reads stop (after the 1-second cache).
    owner.revoke(enums.RevocationReasonCode.KEY_COMPROMISE, uid, compromise_occurrence_date=int(time.time()))
    time.sleep(1.2)
    with pytest.raises(ClientError) as ei:
        s3.get_object(Bucket="acme-e", Key="ledger.csv")
    assert code(ei) == "KMS.DisabledException"
    owner.close()
