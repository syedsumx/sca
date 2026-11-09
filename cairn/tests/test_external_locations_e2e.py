"""Tables on the customer's own storage (L132) under storage credentials and external locations (L133), end to end.

Real Azurite (Microsoft's Azure Storage emulator, over HTTPS on port 443 with production-style host names, as Trino's
Azure file system requires), a real Cairn Store standing in for the customer's S3 (its STS stands in for AWS STS), real
Cairn Catalog asking a real Cairn API, and real Trino 484 acting for each person (``iceberg.rest-catalog.session=USER``:
the catalog gives Trino a token per person, and every table, creation and vend is decided by Cairn for that person).

Azurite serves the blob API only (no hierarchical namespace, no DFS endpoint), so the Azure location is a whole
container and its SAS tokens are container SAS: per-table narrowing within a container (directory SAS, ``sr=d``)
needs ADLS Gen2 and is checked here only by its signature (catalog unit tests). Needs docker and the store and catalog
binaries; skipped when port 443 is taken."""

import base64
import datetime
import hashlib
import hmac
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from urllib.parse import quote

import httpx
import pytest

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, TRINO, _ready, port, wait

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, CAIRN_STORE_BIN and CAIRN_CATALOG_BIN")

AZURITE = os.environ.get("CAIRN_AZURITE_IMAGE", "mcr.microsoft.com/azure-storage/azurite:3.35.0")
ACCOUNT = "cairnlake"
SUFFIX = "azurite.test"            # hosts <account>.blob.azurite.test and <account>.dfs.azurite.test
TRINO_TOKEN = "trino-acts-for-people-" + "t" * 24
STS = "ext-sts-secret-" + "s" * 32
_TV = int(TRINO.rsplit(":", 1)[-1]) if TRINO.rsplit(":", 1)[-1].isdigit() else 484
AZURE_ON = "fs.azure.enabled=true" if _TV >= 484 else "fs.native-azure.enabled=true"
S3_ON = "fs.s3.enabled=true" if _TV >= 484 else "fs.native-s3.enabled=true"

if _ready():
    from test_spark_e2e import ANA, BOB, people  # noqa: F401  (fixtures)
    from test_trino_e2e import cairn  # noqa: F401  (fixture)


def _free(p: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)    # a closed connection's TIME_WAIT is not a listener
        try:
            s.bind(("127.0.0.1", p))
            return True
        except OSError:
            return False


def _tls(d: str) -> None:
    """A CA (ca.pem) and Azurite's certificate for 127.0.0.1 and *.blob/*.dfs.azurite.test."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cairn-test-ca")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
          .not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    sans = [x509.DNSName(f"*.blob.{SUFFIX}"), x509.DNSName(f"*.dfs.{SUFFIX}"), x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "azurite")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    open(f"{d}/ca.pem", "wb").write(ca.public_bytes(serialization.Encoding.PEM))
    open(f"{d}/azurite.pem", "wb").write(cert.public_bytes(serialization.Encoding.PEM))
    open(f"{d}/azurite-key.pem", "wb").write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                               serialization.NoEncryption()))
    for f in ("ca.pem", "azurite.pem", "azurite-key.pem"):
        os.chmod(f"{d}/{f}", 0o644)


class Azure:
    """Azurite over its path-style address, with an account SAS (the administrator's own access)."""

    def __init__(self, key: bytes, ca: str):
        self.key, self.base = key, f"https://127.0.0.1:443/{ACCOUNT}"
        import ssl

        self.http = httpx.Client(verify=ssl.create_default_context(cafile=ca), timeout=30)

    def _account_sas(self) -> str:
        st = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        se = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sp, ss, srt, sv = "racwdl", "b", "sco", "2021-08-06"
        to_sign = "\n".join([ACCOUNT, sp, ss, srt, st, se, "", "https", sv, "", ""])
        sig = base64.b64encode(hmac.new(self.key, to_sign.encode(), hashlib.sha256).digest()).decode()
        return f"sv={sv}&ss={ss}&srt={srt}&sp={sp}&st={quote(st)}&se={quote(se)}&spr=https&sig={quote(sig, safe='')}"

    def make_container(self, c: str) -> None:
        r = self.http.put(f"{self.base}/{c}?restype=container&{self._account_sas()}", headers={"x-ms-version": "2021-08-06"})
        assert r.status_code in (201, 409), r.text

    def blobs(self, c: str, prefix: str = "", sas: str = "") -> list[str]:
        r = self.http.get(f"{self.base}/{c}?restype=container&comp=list&prefix={quote(prefix)}&{sas or self._account_sas()}",
                          headers={"x-ms-version": "2021-08-06"})
        assert r.status_code == 200, r.text
        import re
        return re.findall(r"<Name>([^<]+)</Name>", r.text)

    def with_sas(self, method: str, c: str, path: str, sas: str, body: bytes = b"") -> int:
        h = {"x-ms-version": "2021-08-06"}
        if method == "PUT":
            h["x-ms-blob-type"] = "BlockBlob"
        return self.http.request(method, f"{self.base}/{c}/{quote(path)}?{sas}", content=body, headers=h).status_code


@pytest.fixture(scope="module")
def world(cairn, people):  # noqa: F811
    if not _free(443):
        pytest.skip("port 443 is taken (Trino's Azure file system reaches storage on 443 only)")
    import boto3
    from cairn.governance.abac import Policy, PolicyBundle, Target

    url, s = cairn
    d = tempfile.mkdtemp(prefix="ext-loc-")
    os.chmod(d, 0o755)
    _tls(d)
    account_key = os.urandom(64)
    keyfile = f"{d}/azure-account-key"                     # the storage credential's secret: a file the catalog reads
    open(keyfile, "w").write(base64.b64encode(account_key).decode())
    with open(f"{d}/azurite.env", "w") as fh:              # the emulator's account, never on a command line
        fh.write(f"AZURITE_ACCOUNTS={ACCOUNT}:{base64.b64encode(account_key).decode()}\n")
    names = {"azurite": f"pt-azurite-{os.getpid()}", "trino": f"pt-trino-ext-{os.getpid()}"}
    for n in names.values():
        subprocess.run(["docker", "rm", "-f", n], capture_output=True, timeout=60)
    subprocess.run(["docker", "run", "-d", "--rm", "--name", names["azurite"], "--network", "host", "--env-file", f"{d}/azurite.env",
                    "-v", f"{d}:/certs:ro", AZURITE, "azurite-blob", "--blobHost", "127.0.0.1", "--blobPort", "443",
                    "--cert", "/certs/azurite.pem", "--key", "/certs/azurite-key.pem", "--inMemoryPersistence",
                    "--skipApiVersionCheck", "--loose", "--oauth", "basic"], check=True, capture_output=True, timeout=300)
    procs: list = []
    try:
        wait(443, 60)
        az = Azure(account_key, f"{d}/ca.pem")
        for c in ("lake", "other", "spn"):
            az.make_container(c)
        # Cairn Store with STS: the managed lakehouse, and a bucket standing in for the customer's S3.
        sp, cp = port(), port()
        procs.append(subprocess.Popen([os.environ["CAIRN_STORE_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
            **os.environ, "CAIRN_STORE_ACCESS_KEY": STORE_KEY[0], "CAIRN_STORE_SECRET_KEY": STORE_KEY[1],
            "CAIRN_STORE_DATA": f"{d}/s", "CAIRN_STORE_ADDR": f"0.0.0.0:{sp}", "CAIRN_STORE_BUCKETS": "lakehouse,customer-s3",
            "CAIRN_STORE_FSYNC": "false", "CAIRN_STORE_META": "", "CAIRN_STORE_STS_SECRET": STS}))
        wait(sp)
        store = f"http://127.0.0.1:{sp}"
        kw = {"endpoint_url": store, "aws_access_key_id": STORE_KEY[0], "aws_secret_access_key": STORE_KEY[1],
              "region_name": "us-east-1"}
        me = boto3.client("sts", **kw).get_caller_identity()["Arn"]
        iam = boto3.client("iam", **kw)
        iam.create_role(RoleName="customer-tables", AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Principal": {"AWS": me}, "Action": "sts:AssumeRole"}]}))
        iam.put_role_policy(RoleName="customer-tables", PolicyName="customer", PolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:*",
                                                    "Resource": ["arn:aws:s3:::customer-s3", "arn:aws:s3:::customer-s3/*"]}]}))
        role_arn = iam.get_role(RoleName="customer-tables")["Role"]["Arn"]
        s3_secret = f"{d}/customer-s3-secret"
        open(s3_secret, "w").write(STORE_KEY[1])
        tokens = f"{d}/tokens.json"
        json.dump([{"token": TRINO_TOKEN, "name": "trino", "delegate": True}], open(tokens, "w"))
        log = open(f"{d}/catalog.log", "w")
        procs.append(subprocess.Popen([os.environ["CAIRN_CATALOG_BIN"]], stdout=log, stderr=subprocess.STDOUT, env={
            **os.environ, "CAIRN_CATALOG_ADDR": f"0.0.0.0:{cp}", "CAIRN_CATALOG_DATA": f"{d}/c", "CAIRN_CATALOG_TOKEN": CATALOG_TOKEN,
            "CAIRN_CATALOG_TOKENS_FILE": tokens, "CAIRN_CATALOG_S3_ENDPOINT": store, "CAIRN_CATALOG_S3_ACCESS_KEY": STORE_KEY[0],
            "CAIRN_CATALOG_S3_SECRET_KEY": STORE_KEY[1], "CAIRN_CATALOG_WAREHOUSE": "s3://lakehouse/warehouse",
            "CAIRN_CATALOG_FSYNC": "false", "CAIRN_CATALOG_VEND_ROLE": "cairn-catalog-tables",
            "CAIRN_CATALOG_JWKS_URL": f"{url}/v1/trino/jwks.json", "CAIRN_CATALOG_CAIRN_URL": url,
            "CAIRN_CATALOG_CAIRN_TOKEN": "catalog-service-token-" + "c" * 24, "CAIRN_CATALOG_LOCATIONS": "cairn",
            "CAIRN_CATALOG_LOCATIONS_REFRESH_SECONDS": "1", "CAIRN_CATALOG_STORAGE_CA_FILE": f"{d}/ca.pem",
            "CAIRN_CATALOG_LOG_LEVEL": "debug"}))
        wait(cp)
        catalog = f"http://127.0.0.1:{cp}"

        # The administrator registers the credentials and locations in Cairn, and grants them.
        admin = {"X-Cairn-Actor": "urn:li:corpuser:admin"}

        def api(method, path, body=None, h=admin, ok=(200, 201, 204)):
            r = httpx.request(method, f"{url}{path}", json=body, headers=h, timeout=30)
            assert r.status_code in ok, (path, r.status_code, r.text)
            return r.json() if r.content else None
        api("POST", "/v1/storage/credentials", {"name": "lake-azure", "provider": "azure", "secret_ref": f"file:{keyfile}",
                                                "config": {"account": ACCOUNT, "endpoint": f"https://127.0.0.1:443/{ACCOUNT}",
                                                           "hierarchical_namespace": False}})
        api("POST", "/v1/storage/credentials", {"name": "customer-s3", "provider": "s3", "secret_ref": f"file:{s3_secret}",
                                                "config": {"role_arn": role_arn, "endpoint": store, "sts_endpoint": store,
                                                           "access_key_id": STORE_KEY[0]}})
        az_url = f"abfss://lake@{ACCOUNT}.dfs.{SUFFIX}"
        api("POST", "/v1/storage/locations", {"name": "azure-lake", "url": az_url, "credential": "lake-azure"})
        api("POST", "/v1/storage/locations", {"name": "customer-tables", "url": "s3://customer-s3/tables", "credential": "customer-s3"})
        # An Entra service principal: its client secret in a file, a user delegation key, user delegation SAS.
        entra = EntraStandIn()
        sp_secret = f"{d}/entra-client-secret"
        open(sp_secret, "w").write(entra.secret)
        api("POST", "/v1/storage/credentials", {"name": "lake-azure-sp", "provider": "azure", "secret_ref": f"file:{sp_secret}",
                                                "config": {"account": ACCOUNT, "endpoint": f"https://127.0.0.1:443/{ACCOUNT}",
                                                           "hierarchical_namespace": False, "auth": "service_principal",
                                                           "tenant_id": entra.tenant, "client_id": "cairn-catalog",
                                                           "authority_host": f"http://127.0.0.1:{entra.port}"}})
        api("POST", "/v1/storage/locations",
            {"name": "azure-sp", "url": f"abfss://spn@{ACCOUNT}.dfs.{SUFFIX}", "credential": "lake-azure-sp"})
        api("PUT", "/v1/storage/locations/azure-sp/grants",
            {"principal": "urn:li:corpuser:ana", "privileges": ["CREATE_TABLE", "READ_FILES", "WRITE_FILES"]})
        for loc in ("azure-lake", "customer-tables"):
            api("PUT", f"/v1/storage/locations/{loc}/grants",
                {"principal": "urn:li:corpuser:ana", "privileges": ["CREATE_TABLE", "READ_FILES", "WRITE_FILES"]})
        # Table privileges: ana's on both namespaces; bob's too on the Azure one, so his refusal there comes from the
        # location alone.
        bundle = s.pdp.bundle
        bundle = bundle() if callable(bundle) else bundle
        tables = ["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE", "WRITE_DATASET", "MANAGE_DATASET"]
        s.pdp.set_bundle(PolicyBundle(policies=[*bundle.policies,
            Policy(id="ana-ext", effect="allow", actions=tables, target=Target(resource_urns=["*ext_az.*", "*ext_s3.*", "*ext_sp.*"]),
                   condition={"attr": "subject.urn", "op": "eq", "value": "urn:li:corpuser:ana"}),
            Policy(id="bob-ext", effect="allow", actions=tables, target=Target(resource_urns=["*ext_az.*"]),
                   condition={"attr": "subject.urn", "op": "eq", "value": "urn:li:corpuser:bob"})]))
        # The catalog loads the registrations (every second here).
        from pyiceberg.catalog.rest import RestCatalog
        cat = RestCatalog("admin", uri=catalog, token=CATALOG_TOKEN, **{"adls.blob-storage-authority": "127.0.0.1:443"})
        end = time.time() + 30
        while True:
            try:
                cat.create_namespace("ext_az", {"location": f"{az_url}/ext_az"})
                break
            except Exception:
                assert time.time() < end, open(f"{d}/catalog.log").read()[-3000:]
                time.sleep(0.5)
        cat.create_namespace("ext_s3", {"location": "s3://customer-s3/tables/ext_s3"})
        cat.create_namespace("ext_sp", {"location": f"abfss://spn@{ACCOUNT}.dfs.{SUFFIX}/ext_sp"})

        # Trino, acting for each person through the catalog, with no storage keys of its own.
        trino = _start_trino(d, catalog, store, names["trino"])
        yield {"url": url, "s": s, "api": api, "catalog": catalog, "admin": cat, "az": az, "az_url": az_url, "store": store,
               "trino": trino, "dir": d, "s3kw": kw, "entra": entra}
    finally:
        for p in reversed(procs):
            p.terminate()
            p.wait(15)
        for n in names.values():
            subprocess.run(["docker", "rm", "-f", n], capture_output=True, timeout=60)
        shutil.rmtree(d, ignore_errors=True)


class EntraStandIn:
    """Entra ID's token endpoint for one service principal (client credentials). Its access tokens carry what Azurite
    (``--oauth basic``) checks: issuer, audience, lifetime, the principal's object and tenant ids."""

    def __init__(self):
        import http.server
        import threading

        me = self
        self.tenant, self.secret, self.calls = "cairn-tenant", "entra-secret-" + uuid.uuid4().hex, []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                from urllib.parse import parse_qs

                form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["content-length"])).decode()).items()}
                me.calls.append(form.get("client_id"))
                ok = (self.path == f"/{me.tenant}/oauth2/v2.0/token" and form.get("grant_type") == "client_credentials"
                      and form.get("client_id") == "cairn-catalog" and form.get("client_secret") == me.secret
                      and form.get("scope") == "https://storage.azure.com/.default")
                now = int(time.time())
                b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
                token = ".".join([b64({"alg": "RS256", "typ": "JWT"}), b64({
                    "aud": "https://storage.azure.com", "iss": f"https://sts.windows.net/{me.tenant}/", "iat": now,
                    "nbf": now - 60, "exp": now + 3600, "oid": "0b1c2d3e-0000-4000-8000-00000000c41d", "tid": me.tenant}), "c2ln"])
                body = json.dumps({"access_token": token, "token_type": "Bearer", "expires_in": 3600} if ok
                                  else {"error": "invalid_client"}).encode()
                self.send_response(200 if ok else 401)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.port = port()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def _start_trino(d: str, catalog: str, store: str, name: str):
    tp = port()
    os.makedirs(f"{d}/trino/catalog")
    os.chmod(f"{d}/trino", 0o777)
    # Trino trusts the storage's CA: a truststore made with the image's own keytool.
    subprocess.run(["docker", "run", "--rm", "--entrypoint", "keytool", "-v", f"{d}:/w", "-v", f"{d}/trino:/out", TRINO,
                    "-importcert", "-noprompt", "-alias", "storage-ca", "-file", "/w/ca.pem", "-keystore", "/out/truststore.p12",
                    "-storetype", "PKCS12", "-storepass", "changeit"], check=True, capture_output=True, timeout=300)
    jvm = subprocess.run(["docker", "run", "--rm", "--entrypoint", "cat", TRINO, "/etc/trino/jvm.config"],
                         check=True, capture_output=True, text=True, timeout=300).stdout
    open(f"{d}/trino/jvm.config", "w").write(jvm.rstrip("\n") + "\n-Djavax.net.ssl.trustStore=/etc/trino/truststore.p12\n"
                                            "-Djavax.net.ssl.trustStorePassword=changeit\n-Djavax.net.ssl.trustStoreType=PKCS12\n")
    open(f"{d}/trino/config.properties", "w").write(
        f"coordinator=true\nnode-scheduler.include-coordinator=true\nhttp-server.http.port={tp}\ndiscovery.uri=http://127.0.0.1:{tp}\n")
    open(f"{d}/trino/catalog/ext.properties", "w").write("\n".join([
        "connector.name=iceberg", "iceberg.catalog.type=rest", f"iceberg.rest-catalog.uri={catalog}",
        "iceberg.rest-catalog.security=OAUTH2", f"iceberg.rest-catalog.oauth2.token={TRINO_TOKEN}",
        "iceberg.rest-catalog.session=USER", "iceberg.rest-catalog.vended-credentials-enabled=true",
        AZURE_ON, "azure.auth-type=DEFAULT", f"azure.endpoint={SUFFIX}",
        S3_ON, f"s3.endpoint={store}", "s3.region=us-east-1", "s3.path-style-access=true", ""]))
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "host",
                    "--add-host", f"{ACCOUNT}.blob.{SUFFIX}:127.0.0.1", "--add-host", f"{ACCOUNT}.dfs.{SUFFIX}:127.0.0.1",
                    "-v", f"{d}/trino/catalog:/etc/trino/catalog:ro", "-v", f"{d}/trino/config.properties:/etc/trino/config.properties:ro",
                    "-v", f"{d}/trino/jvm.config:/etc/trino/jvm.config:ro", "-v", f"{d}/trino/truststore.p12:/etc/trino/truststore.p12:ro",
                    TRINO], check=True, capture_output=True, timeout=600)
    end = time.time() + 240
    while "SERVER STARTED" not in subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr:
        assert time.time() < end, "Trino did not start:\n" + subprocess.run(["docker", "logs", "--tail", "60", name],
                                                                             capture_output=True, text=True).stderr
        time.sleep(1)

    def sql(statement: str, user: str):
        out = subprocess.run(["docker", "exec", name, "trino", "--server", f"http://127.0.0.1:{tp}", "--user", user, "--debug",
                              "--catalog", "ext", "--output-format", "JSON", "--execute", statement],
                             capture_output=True, text=True, timeout=300)
        if out.returncode != 0:
            # The first line says what failed; the causes (the catalog's own words) follow with --debug.
            causes = [ln.strip() for ln in out.stderr.splitlines() if "failed" in ln or "Exception:" in ln]
            raise RuntimeError("\n".join(causes[:12]) or out.stderr.strip()[-2000:])
        return [json.loads(x) for x in out.stdout.splitlines() if x.strip()]
    return sql


def _lake_token(url: str, pat: str) -> str:
    r = httpx.post(f"{url}/v1/lakehouse/token", headers={"Authorization": f"Bearer {pat}"}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _entries(s, action: str) -> list[dict]:
    return [e for e in s.audit.entries("storage", limit=2000) if e["action"] == action]


def test_a_person_with_the_grant_creates_writes_and_reads_tables_on_azure_and_s3_through_trino(world):
    sql, az = world["trino"], world["az"]
    before = {e["id"] for e in _entries(world["s"], "storage.vend")}
    sql("CREATE TABLE ext_az.orders AS SELECT * FROM (VALUES (1, 'EU'), (2, 'US')) t(id, region)", "ana")
    sql("INSERT INTO ext_az.orders VALUES (3, 'APAC')", "ana")
    assert sql("SELECT id, region FROM ext_az.orders ORDER BY id", "ana") == [
        {"id": 1, "region": "EU"}, {"id": 2, "region": "US"}, {"id": 3, "region": "APAC"}]
    # The table's files are in the customer's Azure container, metadata and data.
    loc = world["admin"].load_table("ext_az.orders").location()
    assert loc.startswith(f"{world['az_url']}/ext_az/orders-"), loc
    prefix = loc.split(f"{SUFFIX}/", 1)[1]
    files = az.blobs("lake", prefix + "/")
    assert any(f.endswith(".metadata.json") for f in files) and any(f.endswith(".parquet") for f in files), files
    # And on the customer's S3, under the location's prefix.
    sql("CREATE TABLE ext_s3.events AS SELECT * FROM (VALUES (10, 'click')) t(id, kind)", "ana")
    sql("INSERT INTO ext_s3.events VALUES (11, 'view')", "ana")
    assert sql("SELECT count(*) AS n FROM ext_s3.events", "ana") == [{"n": 2}]
    import boto3
    keys = [o["Key"] for o in boto3.client("s3", **world["s3kw"]).list_objects_v2(Bucket="customer-s3")["Contents"]]
    assert keys and all(k.startswith("tables/ext_s3/events-") for k in keys), keys
    # Every hand-out was decided and recorded for ana.
    vends = [e for e in _entries(world["s"], "storage.vend") if e["id"] not in before]
    anas = [e for e in vends if e["actor"] == "urn:li:corpuser:ana"]
    assert anas and all(e["details"]["allowed"] and e["details"]["catalog_principal"] == "user:ana via trino" for e in anas)
    assert {e["subject"] for e in anas} == {"urn:li:externalLocation:azure-lake", "urn:li:externalLocation:customer-tables"}
    # The administrator's own loads (the catalog's static token) are recorded as that token.
    assert {e["actor"] for e in vends} <= {"urn:li:corpuser:ana", "catalog-token:admin"}


def test_someone_without_the_grant_is_refused(world):
    sql = world["trino"]
    with pytest.raises(RuntimeError, match="CREATE_TABLE on external location azure-lake"):
        sql("CREATE TABLE ext_az.bobs AS SELECT 1 AS x", "bob")
    refused = [e for e in _entries(world["s"], "storage.create_table") if not e["details"]["allowed"]]
    assert refused and refused[0]["actor"] == "urn:li:corpuser:bob"
    # Without table privileges at all, bob does not even see the S3 namespace's tables.
    with pytest.raises(RuntimeError):
        sql("CREATE TABLE ext_s3.bobs AS SELECT 1 AS x", "bob")


def test_a_table_outside_every_registered_location_is_refused(world):
    import pyarrow as pa
    from pyiceberg.exceptions import ForbiddenError

    admin, sql = world["admin"], world["trino"]
    schema = pa.schema([("x", pa.int64())])
    for where in (f"abfss://other@{ACCOUNT}.dfs.{SUFFIX}/t", f"abfss://lake@nobody.dfs.{SUFFIX}/t", "gs://somewhere/t"):
        with pytest.raises(ForbiddenError, match="not in a registered external location"):
            admin.create_table(f"ext_az.out_{uuid.uuid4().hex[:6]}", schema=schema, location=where)
    with pytest.raises(RuntimeError, match="not in a registered external location"):
        sql(f"CREATE TABLE ext_az.outside (x int) WITH (location = 'abfss://other@{ACCOUNT}.dfs.{SUFFIX}/outside')", "ana")
    with pytest.raises(ForbiddenError, match="not in a registered external location"):
        admin.create_namespace("nowhere", {"location": f"abfss://other@{ACCOUNT}.dfs.{SUFFIX}/ns"})
    assert world["az"].blobs("other") == []


def test_vended_access_reaches_its_own_place_only(world):
    url, catalog, az = world["url"], world["catalog"], world["az"]
    token = _lake_token(url, ANA)
    h = {"Authorization": f"Bearer {token}"}
    t = httpx.get(f"{catalog}/v1/namespaces/ext_az/tables/orders", headers=h, timeout=30).json()
    cfg = t["config"]
    sas = cfg["adls.sas-token"]
    assert t["storage-credentials"][0]["prefix"] == t["metadata"]["location"]
    assert f"adls.sas-token.{ACCOUNT}.dfs.{SUFFIX}" in cfg and cfg["adls.refresh-credentials-endpoint"].endswith("/credentials")
    loc_path = t["metadata"]["location"].split(f"{SUFFIX}/", 1)[1]
    files = az.blobs("lake", loc_path + "/", sas=sas)
    assert files and az.with_sas("GET", "lake", files[0], sas) == 200
    # Not another container (another place on the account), with any operation.
    assert az.with_sas("PUT", "other", "stolen.txt", sas, b"x") == 403
    assert az.with_sas("GET", "other", "anything", sas) == 403
    # A read-only hand-out (READ_FILES, by path) reads and cannot write or delete.
    ro = httpx.get(f"{catalog}/v1/locations/credentials", params={"url": f"{world['az_url']}/{loc_path}", "access": "read"},
                   headers=h, timeout=30)
    assert ro.status_code == 200, ro.text
    ro_sas = ro.json()["config"]["adls.sas-token"]
    assert "sp=rl" in ro_sas and az.with_sas("GET", "lake", files[0], ro_sas) == 200
    assert az.with_sas("PUT", "lake", f"{loc_path}/data/x.txt", ro_sas, b"x") == 403
    assert az.with_sas("DELETE", "lake", files[0], ro_sas) == 403
    # On S3 the keys are for the one table's prefix: not the bucket's other tables, not the rest of it.
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    t = httpx.get(f"{catalog}/v1/namespaces/ext_s3/tables/events", headers=h, timeout=30).json()
    c = t["config"]
    s3 = boto3.client("s3", endpoint_url=c["s3.endpoint"], aws_access_key_id=c["s3.access-key-id"],
                      aws_secret_access_key=c["s3.secret-access-key"], aws_session_token=c["s3.session-token"],
                      region_name="us-east-1", config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}))
    prefix = t["metadata"]["location"].removeprefix("s3://customer-s3/")
    assert s3.list_objects_v2(Bucket="customer-s3", Prefix=prefix + "/")["KeyCount"] > 0
    for call in (lambda: s3.list_objects_v2(Bucket="customer-s3", Prefix="tables/"),
                 lambda: s3.put_object(Bucket="customer-s3", Key="tables/ext_s3/other/x", Body=b"x"),
                 lambda: s3.list_objects_v2(Bucket="lakehouse")):
        with pytest.raises(ClientError) as ei:
            call()
        assert ei.value.response["Error"]["Code"] == "AccessDenied"


def test_a_service_principal_credential_hands_out_user_delegation_sas(world):
    """The catalog signs in to Entra as the service principal (its secret from the file), gets a user delegation
    key from the storage account, and signs with it: Trino writes and reads as ana, and a path's SAS reaches its
    container only."""
    sql, az = world["trino"], world["az"]
    sql("CREATE TABLE ext_sp.visits AS SELECT * FROM (VALUES (1, 'home'), (2, 'cart')) t(id, page)", "ana")
    assert sql("SELECT count(*) AS n FROM ext_sp.visits", "ana") == [{"n": 2}]
    assert any(f.endswith(".metadata.json") for f in az.blobs("spn", "ext_sp/visits-"))
    assert "cairn-catalog" in world["entra"].calls
    h = {"Authorization": f"Bearer {_lake_token(world['url'], ANA)}"}
    r = httpx.get(f"{world['catalog']}/v1/locations/credentials", headers=h, timeout=30,
                  params={"url": f"abfss://spn@{ACCOUNT}.dfs.{SUFFIX}/drop", "access": "write"})
    assert r.status_code == 200, r.text
    sas = r.json()["config"]["adls.sas-token"]
    assert "skoid=" in sas and "sktid=cairn-tenant" in sas                  # a user delegation SAS
    assert az.with_sas("PUT", "spn", "drop/a.txt", sas, b"hello") == 201
    assert az.with_sas("GET", "spn", "drop/a.txt", sas) == 200
    assert az.with_sas("GET", "lake", "anything", sas) == 403


def test_pyiceberg_reads_the_s3_table_with_the_persons_token(world):
    from pyiceberg.catalog.rest import RestCatalog

    ana = RestCatalog("ana", uri=world["catalog"], token=_lake_token(world["url"], ANA))
    assert sorted(ana.load_table("ext_s3.events").scan().to_arrow().column("id").to_pylist()) == [10, 11]


def test_a_revoked_grant_stops_new_vends(world):
    url, catalog, api, s = world["url"], world["catalog"], world["api"], world["s"]
    token = _lake_token(url, ANA)
    h = {"Authorization": f"Bearer {token}"}
    path = {"url": f"{world['az_url']}/ext_az", "access": "read"}
    assert httpx.get(f"{catalog}/v1/locations/credentials", params=path, headers=h, timeout=30).status_code == 200
    api("DELETE", "/v1/storage/locations/azure-lake/grants?principal=urn:li:corpuser:ana&privilege=READ_FILES")
    r = httpx.get(f"{catalog}/v1/locations/credentials", params=path, headers=h, timeout=30)
    assert r.status_code == 403 and "READ_FILES" in r.text, r.text
    # CREATE_TABLE too: ana's next table is refused at once.
    api("DELETE", "/v1/storage/locations/azure-lake/grants?principal=urn:li:corpuser:ana&privilege=CREATE_TABLE")
    with pytest.raises(RuntimeError, match="CREATE_TABLE"):
        world["trino"]("CREATE TABLE ext_az.after_revoke AS SELECT 1 AS x", "ana")
    # The table's own grant: renewing the table's credentials is refused once it is gone (the catalog keeps a
    # decision for up to 15 seconds).
    from cairn.governance.abac import PolicyBundle

    renew = f"{catalog}/v1/namespaces/ext_az/tables/orders/credentials"
    assert httpx.get(renew, headers=h, timeout=30).status_code == 200
    bundle = s.pdp.bundle
    s.pdp.set_bundle(PolicyBundle(policies=[p for p in bundle.policies if p.id != "ana-ext"]))
    end = time.time() + 30
    while httpx.get(renew, headers=h, timeout=30).status_code != 403:
        assert time.time() < end, "the table's credentials were still renewed after the grant was revoked"
        time.sleep(1)
    with pytest.raises(RuntimeError):
        world["trino"]("SELECT * FROM ext_az.orders", "ana")
    # A location that is removed stops everything in it, for everyone.
    api("DELETE", "/v1/storage/locations/customer-tables")
    time.sleep(2)
    from pyiceberg.exceptions import ForbiddenError

    with pytest.raises(ForbiddenError, match="not in a registered external location"):
        world["admin"].load_table("ext_s3.events")
    assert any(not e["details"]["allowed"] for e in _entries(s, "storage.vend"))


def test_the_registrations_hold_together(world):
    api = world["api"]

    def refused(method, path, body, status):
        r = httpx.request(method, f"{world['url']}{path}", json=body, headers={"X-Cairn-Actor": "urn:li:corpuser:admin"}, timeout=30)
        assert r.status_code == status, (r.status_code, r.text)
        return r.json()["detail"]
    # Overlapping locations, Cairn's own storage, secrets in place of references, a credential still in use.
    assert "overlaps external location azure-lake" in refused(
        "POST", "/v1/storage/locations", {"name": "again", "url": world["az_url"] + "/", "credential": "lake-azure"},
        409)
    assert "Cairn's own storage" in refused(
        "POST", "/v1/storage/locations", {"name": "mine", "url": "s3://lakehouse/warehouse/x", "credential": "customer-s3"}, 409)
    assert "never stored" in refused("POST", "/v1/storage/credentials", {
        "name": "leaky", "provider": "azure", "config": {"account": ACCOUNT, "account_key": "c2VjcmV0"}}, 422)
    assert "reference" in refused("POST", "/v1/storage/credentials", {
        "name": "leaky", "provider": "azure", "config": {"account": ACCOUNT}, "secret_ref": "c2VjcmV0c2VjcmV0"}, 422)
    assert "used by" in refused("DELETE", "/v1/storage/credentials/lake-azure", None, 409)
    # People see locations they hold a privilege on, never the credentials.
    bob = {"X-Cairn-Actor": "urn:li:corpuser:bob"}
    assert httpx.get(f"{world['url']}/v1/storage/credentials", headers=bob, timeout=10).status_code == 403
    assert api("GET", "/v1/storage/locations", h=bob)["locations"] == []
    cfg = httpx.get(f"{world['url']}/v1/storage/catalog-config", headers=bob, timeout=10)
    assert cfg.status_code == 403
