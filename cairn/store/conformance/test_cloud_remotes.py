"""Remote buckets on Azure Blob Storage and Google Cloud Storage, spoken natively (against the Azurite and
fake-gcs-server emulators in containers): backup sinks (backup, restore, delete), remote mounts (sync with
metadata, ranged reads, write-back) and storage tiers (transition, read, delete). Azure with Shared Key
and with a SAS; Azure blobs above 64 MiB go up as blocks. GCS with a service account key (a JWT signed by
the store, checked here by a token endpoint) and through the metadata server. Specs that do not fit their
kind are refused at startup. Needs docker."""

import base64
import datetime
import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

from conftest import ADMIN, REGION, Store, _free_port

AZURITE = "mcr.microsoft.com/azure-storage/azurite:3.33.0"
FAKE_GCS = "fsouza/fake-gcs-server:1.52.2"
ACCOUNT = "devstoreaccount1"
ACCOUNT_KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
MASTER = base64.b64encode(b"c" * 32).decode()
FAST = {"CAIRN_STORE_LIFECYCLE_INTERVAL": "1", "CAIRN_STORE_LIFECYCLE_DAY_SECONDS": "1"}


def admin(st, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = AWSRequest(method=method, url=f"{st.endpoint}/-/admin/v1/{path}", data=data)
    S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
    r = requests.request(method, req.url, data=data, headers=dict(req.headers), timeout=120)
    return r.status_code, (r.json() if r.content else None)


def wait_for(cond, timeout=30, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return
        except ClientError:
            pass
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {what}")


def docker_ok():
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


pytestmark = pytest.mark.skipif(not docker_ok(), reason="needs docker (Azurite, fake-gcs-server)")


def container(image, port, *args, inner):
    name = f"cairn-test-{os.getpid()}-{port}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:{inner}", image, *args],
                   check=True, capture_output=True, timeout=300)
    return name


def wait_port(port, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 1).close()
            return
        except OSError:
            time.sleep(0.2)
    raise AssertionError(f"port {port} never opened")


# --- Azure ------------------------------------------------------------------------------------------------

def account_sas(perms="rwdlac"):
    """An account SAS for the Blob service (as the Azure SDKs make them)."""
    sv, se = "2021-12-02", (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sts = "\n".join([ACCOUNT, perms, "b", "sco", "", se, "", "https,http", sv, "", ""])
    sig = base64.b64encode(hmac.new(base64.b64decode(ACCOUNT_KEY), sts.encode(), hashlib.sha256).digest()).decode()
    return urllib.parse.urlencode({"sv": sv, "ss": "b", "srt": "sco", "sp": perms, "se": se, "spr": "https,http", "sig": sig})


class Azure:
    def __init__(self, endpoint):
        self.endpoint, self.sas = endpoint, account_sas()

    def url(self, path, query=""):
        return f"{self.endpoint}/{path}?{query + '&' if query else ''}{self.sas}"

    def create(self, container):
        r = requests.put(self.url(container, "restype=container"), headers={"x-ms-version": "2021-12-02"}, timeout=30)
        assert r.status_code == 201, r.text

    def put(self, container, name, body, content_type="application/octet-stream", meta=None):
        h = {"x-ms-blob-type": "BlockBlob", "x-ms-version": "2021-12-02", "content-type": content_type}
        h.update({f"x-ms-meta-{k}": v for k, v in (meta or {}).items()})
        r = requests.put(self.url(f"{container}/{urllib.parse.quote(name)}"), data=body, headers=h, timeout=60)
        assert r.status_code == 201, r.text

    def head(self, container, name):
        return requests.head(self.url(f"{container}/{urllib.parse.quote(name)}"), headers={"x-ms-version": "2021-12-02"}, timeout=30)

    def get(self, container, name):
        r = requests.get(self.url(f"{container}/{urllib.parse.quote(name)}"), headers={"x-ms-version": "2021-12-02"}, timeout=60)
        assert r.status_code == 200, r.text
        return r.content

    def names(self, container, prefix=""):
        import xml.etree.ElementTree as ET

        q = urllib.parse.urlencode({"restype": "container", "comp": "list", "prefix": prefix})
        r = requests.get(self.url(container, q), headers={"x-ms-version": "2021-12-02"}, timeout=30)
        assert r.status_code == 200, r.text
        return sorted(e.text for e in ET.fromstring(r.content).iter("Name"))

    def block_count(self, container, name):
        import xml.etree.ElementTree as ET

        r = requests.get(self.url(f"{container}/{urllib.parse.quote(name)}", "comp=blocklist&blocklisttype=committed"),
                         headers={"x-ms-version": "2021-12-02"}, timeout=30)
        assert r.status_code == 200, r.text
        return len(list(ET.fromstring(r.content).iter("Block")))


@pytest.fixture(scope="module")
def azurite():
    port = _free_port()
    name = container(AZURITE, port, "azurite-blob", "--blobHost", "0.0.0.0", "--loose", "--skipApiVersionCheck", inner=10000)
    try:
        wait_port(port)
        time.sleep(1)
        yield Azure(f"http://127.0.0.1:{port}/{ACCOUNT}")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


def azure_spec(az, rid, container, prefix="", sas=False):
    spec = {"id": rid, "kind": "azure", "endpoint": az.endpoint, "bucket": container, "prefix": prefix, "access_key": ACCOUNT}
    spec.update({"sas": az.sas} if sas else {"secret_key": ACCOUNT_KEY})
    return spec


def test_azure_backup_restore_and_delete(azurite, custom_store, tmp_path, binary):
    azurite.create("backups")
    sinks = tmp_path / "sinks.json"
    sinks.write_text(json.dumps([azure_spec(azurite, "az", "backups", "cairn")]))
    src = custom_store({"CAIRN_STORE_BACKUP_SINKS_FILE": str(sinks), "CAIRN_STORE_MASTER_KEY": MASTER})
    s3 = src.client()
    s3.create_bucket(Bucket="data")
    huge = os.urandom(70 << 20)  # one blob file above 64 MiB: written as blocks
    bodies = {f"obj{i}": os.urandom(150_000 + i) for i in range(4)}
    bodies.update({f"tiny{i}": f"tiny {i}".encode() * 10 for i in range(20)})
    for k, b in bodies.items():
        s3.put_object(Bucket="data", Key=k, Body=b)
    s3.put_object(Bucket="data", Key="huge", Body=huge)
    code, rep = admin(src, "POST", "backups", {"name": "nightly", "sink": "az"})
    assert code == 201 and rep["objects"] == 25, rep
    stored = azurite.names("backups", "cairn/")
    assert "cairn/backups/nightly/manifest.json" in stored and any(n.startswith("cairn/blobs/") for n in stored)
    big = max((n for n in stored if n.startswith("cairn/blobs/")), key=lambda n: int(azurite.head("backups", n).headers["content-length"]))
    assert azurite.block_count("backups", big) == 5  # 70 MiB in 16 MiB blocks
    assert [b["name"] for b in admin(src, "GET", "backups?sink=az")[1]["backups"]] == ["nightly"]
    restored = tmp_path / "restored"
    out = subprocess.run([binary, "restore-backup", "az", "latest", str(restored)], capture_output=True, text=True, timeout=300,
                         env={**os.environ, "CAIRN_STORE_BACKUP_SINKS_FILE": str(sinks)})
    assert out.returncode == 0, out.stderr
    rst = Store(binary, str(restored), _free_port(), env={"CAIRN_STORE_META": "", "CAIRN_STORE_MASTER_KEY": MASTER}).start()
    try:
        r3 = rst.client()
        for k, b in bodies.items():
            assert r3.get_object(Bucket="data", Key=k)["Body"].read() == b
        got = r3.get_object(Bucket="data", Key="huge")["Body"].read()
        assert hashlib.sha256(got).digest() == hashlib.sha256(huge).digest()
    finally:
        rst.stop()
    code, out = admin(src, "DELETE", "backups/nightly?sink=az")
    assert code == 200 and out["blobs_deleted"] >= 1, out
    assert azurite.names("backups", "cairn/") == []


def test_azure_mount_with_sas_metadata_and_write_back(azurite, custom_store, tmp_path):
    azurite.create("upstream")
    for i in range(5):
        azurite.put("upstream", f"data/f{i}.csv", f"row {i}\n".encode() * 500, "text/csv", {"source": "erp"})
    azurite.put("upstream", "elsewhere.txt", b"outside the prefix")
    remotes = tmp_path / "remotes.json"
    remotes.write_text(json.dumps([azure_spec(azurite, "up", "upstream", "data/", sas=True),
                                   azure_spec(azurite, "up-key", "upstream", "data/")]))
    st = custom_store({"CAIRN_STORE_REMOTES_FILE": str(remotes), "CAIRN_STORE_MASTER_KEY": MASTER,
                       "CAIRN_STORE_REMOTE_SYNC_INTERVAL": "3600"})
    s3 = st.client()
    s3.create_bucket(Bucket="mirror")
    assert admin(st, "PUT", "buckets/mirror/remote", {"remote": "up", "write_back": True})[0] == 200
    code, rep = admin(st, "POST", "buckets/mirror/remote/sync")
    assert code == 200 and rep["added"] == 5, rep
    h = s3.head_object(Bucket="mirror", Key="f2.csv")
    assert h["ContentType"] == "text/csv" and h["Metadata"] == {"source": "erp"} and h["ContentLength"] == 3000
    body = b"row 3\n" * 500
    assert s3.get_object(Bucket="mirror", Key="f3.csv", Range="bytes=600-1199")["Body"].read() == body[600:1200]
    # Local writes go back to the container, with metadata Azure could not name as is.
    s3.put_object(Bucket="mirror", Key="new/out.json", Body=b'{"ok": 1}', ContentType="application/json",
                  Metadata={"with-dash": "yes", "plain": "1"})
    wait_for(lambda: azurite.head("upstream", "data/new/out.json").status_code == 200, what="write-back")
    hd = azurite.head("upstream", "data/new/out.json").headers
    assert hd["content-type"] == "application/json" and hd["x-ms-meta-plain"] == "1"
    assert azurite.get("upstream", "data/new/out.json") == b'{"ok": 1}'
    s3.delete_object(Bucket="mirror", Key="f0.csv")
    wait_for(lambda: azurite.head("upstream", "data/f0.csv").status_code == 404, what="deletion")
    # A second mount of the same container (with the account key) reads the metadata back under its own names.
    s3.create_bucket(Bucket="again")
    assert admin(st, "PUT", "buckets/again/remote", {"remote": "up-key"})[0] == 200
    code, rep = admin(st, "POST", "buckets/again/remote/sync")
    assert code == 200 and rep["added"] == 5, (rep, azurite.names("upstream"))
    assert s3.head_object(Bucket="again", Key="new/out.json")["Metadata"] == {"with-dash": "yes", "plain": "1"}
    assert sorted(o["Key"] for o in s3.list_objects_v2(Bucket="again")["Contents"]) == [
        "f1.csv", "f2.csv", "f3.csv", "f4.csv", "new/out.json"]


def test_azure_wrong_key_fails_loudly(azurite, custom_store, tmp_path):
    azurite.create("locked")
    sinks = tmp_path / "sinks.json"
    bad = {**azure_spec(azurite, "az", "locked"), "secret_key": base64.b64encode(b"x" * 64).decode()}
    sinks.write_text(json.dumps([bad]))
    src = custom_store({"CAIRN_STORE_BACKUP_SINKS_FILE": str(sinks)})
    src.client().create_bucket(Bucket="data")
    code, out = admin(src, "POST", "backups", {"name": "x", "sink": "az"})
    assert code == 502 and out["error"] == "BadGateway", out  # Azure refused the signature: nothing is pretended


# --- Google Cloud Storage ---------------------------------------------------------------------------------

class GCS:
    def __init__(self, endpoint):
        self.endpoint = endpoint

    def create(self, bucket):
        r = requests.post(f"{self.endpoint}/storage/v1/b", json={"name": bucket}, timeout=30)
        assert r.status_code in (200, 409), r.text

    def put(self, bucket, name, body, content_type="application/octet-stream", meta=None):
        r = requests.post(f"{self.endpoint}/upload/storage/v1/b/{bucket}/o", params={"uploadType": "resumable", "name": name},
                          json={"name": name, "contentType": content_type, "metadata": meta or {}}, timeout=30)
        assert r.status_code == 200, r.text
        r = requests.put(r.headers["location"], data=body, headers={"content-type": content_type}, timeout=60)
        assert r.status_code in (200, 201), r.text

    def obj(self, bucket, name):
        r = requests.get(f"{self.endpoint}/storage/v1/b/{bucket}/o/{urllib.parse.quote(name, safe='')}", timeout=30)
        return r.json() if r.status_code == 200 else None

    def get(self, bucket, name):
        url = f"{self.endpoint}/storage/v1/b/{bucket}/o/{urllib.parse.quote(name, safe='')}"
        r = requests.get(url, params={"alt": "media"}, timeout=60)
        assert r.status_code == 200, r.text
        return r.content

    def names(self, bucket, prefix=""):
        r = requests.get(f"{self.endpoint}/storage/v1/b/{bucket}/o", params={"prefix": prefix}, timeout=30)
        assert r.status_code == 200, r.text
        return sorted(i["name"] for i in r.json().get("items", []))


@pytest.fixture(scope="module")
def gcs():
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    name = container(FAKE_GCS, port, "-scheme", "http", "-port", "4443", "-public-host", f"127.0.0.1:{port}",
                     "-external-url", url, "-backend", "memory", inner=4443)
    try:
        wait_port(port)
        end = time.time() + 60
        while time.time() < end:
            try:
                if requests.get(f"{url}/storage/v1/b", timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.3)
        yield GCS(url)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


@pytest.fixture
def token_server():
    """An OAuth token endpoint: checks the store's RS256 JWT (a service account key's grant) or the
    metadata server's header, and hands out tokens."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, code, doc):
            body = json.dumps(doc).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = "/computeMetadata/v1/instance/service-accounts/default/token"
            ok = self.path == path and self.headers.get("metadata-flavor") == "Google"
            seen.append(("metadata", ok))
            self.reply(200 if ok else 403, {"access_token": "from-metadata", "expires_in": 3599} if ok else {})

        def do_POST(self):
            form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["content-length"])).decode())
            jwt = form["assertion"][0]
            head, claims, sig = jwt.split(".")
            pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
            try:
                key.public_key().verify(pad(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
                c = json.loads(pad(claims))
                ok = (form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"] and c["iss"] == "cairn@test.iam"
                      and c["aud"] == f"http://127.0.0.1:{port}/token" and "devstorage" in c["scope"] and c["exp"] > time.time())
            except Exception:
                ok = False
            seen.append(("jwt", ok))
            self.reply(200 if ok else 400, {"access_token": "from-jwt", "expires_in": 3599} if ok else {"error": "invalid_grant"})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"pem": pem, "port": port, "seen": seen}
    srv.shutdown()


def test_gcs_tier_with_a_service_account(gcs, token_server, custom_store, tmp_path):
    gcs.create("cold-tier")
    creds = tmp_path / "sa.json"
    creds.write_text(json.dumps({"type": "service_account", "client_email": "cairn@test.iam", "private_key": token_server["pem"],
                                 "token_uri": f"http://127.0.0.1:{token_server['port']}/token"}))
    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps([{"storage_class": "STANDARD_IA", "kind": "gcs", "endpoint": gcs.endpoint, "bucket": "cold-tier",
                                  "prefix": "lake/", "credentials_file": str(creds)}]))
    src = custom_store({**FAST, "CAIRN_STORE_TIERS_FILE": str(tiers), "CAIRN_STORE_MASTER_KEY": MASTER})
    s3 = src.client()
    s3.create_bucket(Bucket="data")
    small = b"cold data " * 1000
    big = os.urandom(3 << 20)
    s3.put_object(Bucket="data", Key="old/small.txt", Body=small)
    s3.put_object(Bucket="data", Key="old/big.bin", Body=big)
    s3.put_bucket_lifecycle_configuration(Bucket="data", LifecycleConfiguration={"Rules": [
        {"ID": "cold", "Status": "Enabled", "Filter": {"Prefix": "old/"}, "Transitions": [{"Days": 1, "StorageClass": "STANDARD_IA"}]}]})
    keys = ["old/small.txt", "old/big.bin"]
    wait_for(lambda: all(s3.head_object(Bucket="data", Key=k).get("StorageClass") == "STANDARD_IA" for k in keys), what="transition")
    stored = gcs.names("cold-tier")
    assert len(stored) >= 2 and all(n.startswith("lake/") for n in stored)
    assert all(b"cold data" not in gcs.get("cold-tier", n) for n in stored)  # encrypted at rest, also there
    assert s3.get_object(Bucket="data", Key="old/small.txt")["Body"].read() == small
    assert s3.get_object(Bucket="data", Key="old/big.bin", Range="bytes=1000000-1000999")["Body"].read() == big[1000000:1001000]
    # One signed grant served every call (the token is cached), and it checked out.
    assert token_server["seen"] and all(kind == "jwt" and ok for kind, ok in token_server["seen"])
    assert len(token_server["seen"]) == 1
    s3.delete_object(Bucket="data", Key="old/big.bin")
    s3.delete_object(Bucket="data", Key="old/small.txt")
    wait_for(lambda: gcs.names("cold-tier") == [], what="tier deletion")


def test_gcs_sink_and_mount_through_the_metadata_server(gcs, token_server, custom_store, tmp_path, binary):
    gcs.create("offsite")
    gcs.create("lake")
    for i in range(4):
        gcs.put("lake", f"raw/e{i}.json", json.dumps({"i": i}).encode() * 100, "application/json", {"team": "ingest"})
    specs = [{"id": "g", "kind": "gcs", "endpoint": gcs.endpoint, "bucket": "offsite", "prefix": "cairn", "auth": "metadata"}]
    sinks = tmp_path / "sinks.json"
    sinks.write_text(json.dumps(specs))
    remotes = tmp_path / "remotes.json"
    remotes.write_text(json.dumps([{"id": "lake", "kind": "gcs", "endpoint": gcs.endpoint, "bucket": "lake", "prefix": "raw/",
                                    "auth": "none"}]))
    env = {"GCE_METADATA_HOST": f"127.0.0.1:{token_server['port']}"}
    os.environ.update(env)  # the store inherits it; so does restore-backup below
    try:
        st = custom_store({"CAIRN_STORE_BACKUP_SINKS_FILE": str(sinks), "CAIRN_STORE_REMOTES_FILE": str(remotes),
                           "CAIRN_STORE_MASTER_KEY": MASTER, "CAIRN_STORE_REMOTE_SYNC_INTERVAL": "3600"})
        s3 = st.client()
        s3.create_bucket(Bucket="mirror")
        admin(st, "PUT", "buckets/mirror/remote", {"remote": "lake", "write_back": True})
        code, rep = admin(st, "POST", "buckets/mirror/remote/sync")
        assert code == 200 and rep["added"] == 4, rep
        h = s3.head_object(Bucket="mirror", Key="e1.json")
        assert h["ContentType"] == "application/json" and h["Metadata"] == {"team": "ingest"}
        whole = json.dumps({"i": 2}).encode() * 100
        assert s3.get_object(Bucket="mirror", Key="e2.json", Range="bytes=8-15")["Body"].read() == whole[8:16]
        s3.put_object(Bucket="mirror", Key="out/r.csv", Body=b"a,b\n", ContentType="text/csv", Metadata={"by-job": "nightly"})
        wait_for(lambda: gcs.obj("lake", "raw/out/r.csv") is not None, what="write-back")
        o = gcs.obj("lake", "raw/out/r.csv")
        assert o["contentType"] == "text/csv" and o["metadata"] == {"by-job": "nightly"}
        # A backup of everything to the GCS sink; the restore runs through the metadata server too.
        code, b = admin(st, "POST", "backups", {"name": "b1", "sink": "g"})
        assert code == 201, b
        assert "cairn/backups/b1/manifest.json" in gcs.names("offsite", "cairn/")
        restored = tmp_path / "restored"
        out = subprocess.run([binary, "restore-backup", "g", "b1", str(restored)], capture_output=True, text=True, timeout=300,
                             env={**os.environ, "CAIRN_STORE_BACKUP_SINKS_FILE": str(sinks)})
        assert out.returncode == 0, out.stderr
        assert token_server["seen"] and all(kind == "metadata" and ok for kind, ok in token_server["seen"])
        assert admin(st, "DELETE", "backups/b1?sink=g")[0] == 200
        assert gcs.names("offsite", "cairn/") == []
    finally:
        for k in env:
            os.environ.pop(k, None)


# --- Refusals ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("spec", [
    {"id": "x", "kind": "azure", "bucket": "box", "access_key": ACCOUNT},  # no key or SAS
    {"id": "x", "kind": "azure", "bucket": "box", "access_key": ACCOUNT, "secret_key": ACCOUNT_KEY, "sas": "sv=1"},
    {"id": "x", "kind": "azure", "bucket": "box", "access_key": ACCOUNT, "secret_key": ACCOUNT_KEY, "region": "eu-west-1"},
    {"id": "x", "kind": "gcs", "bucket": "b"},  # no auth
    {"id": "x", "kind": "gcs", "bucket": "b", "auth": "service_account", "credentials_file": "/nonexistent.json"},
    {"id": "x", "kind": "gcs", "bucket": "b", "auth": "none", "secret_key": "s"},
    {"id": "x", "kind": "hdfs", "bucket": "b"},
])
def test_specs_that_do_not_fit_their_kind_are_refused(spec, binary, tmp_path):
    f = tmp_path / "remotes.json"
    f.write_text(json.dumps([spec]))
    st = Store(binary, str(tmp_path / "d"), _free_port(), env={"CAIRN_STORE_REMOTES_FILE": str(f)})
    with pytest.raises(RuntimeError):
        st.start()
