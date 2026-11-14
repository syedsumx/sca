"""Snowflake and Databricks tables in place (L151), against the open-source components their services are built on:

* **Snowflake Open Catalog** is managed Apache Polaris: a real Polaris (``CAIRN_POLARIS_IMAGE``, default
  apache/polaris:1.8.0) with its tables in a Cairn Store of their own, vending per-table keys through the store's
  STS. Cairn registers it through the ``snowflake_open_catalog`` preset with a read-only Polaris principal whose
  secret is in a secret scope, adds it to a real Trino, catalogues its tables as ``snowflake`` datasets with an
  owner and a domain, and Trino reads them in place as each person: masked, filtered, refused, never written. A
  lakehouse table made from one has the Snowflake dataset upstream.
* **Databricks Unity Catalog** through its Iceberg REST endpoint: a real Unity Catalog OSS
  (``CAIRN_UNITY_CATALOG_IMAGE``, default unitycatalog/unitycatalog:v0.7.0) with an Iceberg table made through
  that endpoint; registered with the ``databricks_unity`` preset, catalogued as ``databricks`` datasets, listed
  through Trino and governed (its files are on the Unity Catalog server's own disk, which Trino does not read).
* **Databricks Delta Sharing**: a real Delta table written with the ``deltalake`` package into a Cairn Store bucket,
  served as a provider serves it (the Delta Sharing protocol's version and query, with pre-signed store URLs), and
  read by Cairn as a recipient (``delta_sharing`` SaaS sync) into a governed lakehouse table; the shared table is
  catalogued as a ``databricks`` dataset upstream of it. (The protocol's reference server signs URLs only for AWS,
  Azure and Google storage endpoints, so it cannot serve a table on a local store; Cairn's own Delta Sharing server
  is checked against the official client in test_delta_sharing_e2e.py.)

Needs docker and the store and catalog binaries (CAIRN_STORE_BIN, CAIRN_CATALOG_BIN)."""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from test_trino_e2e import (CATALOG_TOKEN, EMAIL, KEY, STORE_KEY, _ready, cairn, lake, port, trino,  # noqa: F401
                            wait)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, CAIRN_STORE_BIN and CAIRN_CATALOG_BIN")

POLARIS = os.environ.get("CAIRN_POLARIS_IMAGE", "apache/polaris:1.8.0")
UNITY = os.environ.get("CAIRN_UNITY_CATALOG_IMAGE", "unitycatalog/unitycatalog:v0.7.0")
ADMIN = {"X-Cairn-Actor": "urn:li:corpuser:admin"}
ANA = {"X-Cairn-Actor": "urn:li:corpuser:ana"}
ROOT = ("root", "polaris-root-" + "r" * 20)
STORE = ("snow-store", "snow-store-secret-" + "s" * 16)
SNOW_ORDERS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,snow.sales.orders,PROD)"
SNOW_PAY = "urn:li:dataset:(urn:li:dataPlatform:snowflake,snow.hr.pay,PROD)"
OWNER, DOMAIN = "urn:li:corpGroup:sales-eng", "urn:li:domain:sales"


def _docker(*args, check=True, timeout=600):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=check)


@pytest.fixture(scope="module")
def scopes(cairn, tmp_path_factory):  # noqa: F811
    """Secret scopes for the operator (the cairn fixture runs without them): where warehouse credentials live."""
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.identity.secret_scopes import SecretScopes

    old = os.environ.get("CAIRN_SECRETS_KEY")
    os.environ["CAIRN_SECRETS_KEY"] = base64.b64encode(b"w" * 32).decode()
    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('scopes')}/s.db")
    init_db(engine)
    ss = SecretScopes(engine, lambda a: [], lambda a: False)
    _, s = cairn
    ext = s.external_catalogs
    before = ext.read_secret
    ext.read_secret = ss.read
    yield ss
    ext.read_secret = before
    if old is None:
        os.environ.pop("CAIRN_SECRETS_KEY", None)
    else:
        os.environ["CAIRN_SECRETS_KEY"] = old


def _connect(trino, cairn):  # noqa: F811
    """Cairn adds external catalogs to the test's Trino and signs it in through its own broker."""
    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    s.external_catalogs.trino_connection, s.external_catalogs.broker_url = "lakehouse", url
    return url, s


# ---- Snowflake Open Catalog (Apache Polaris) ------------------------------------------------------------------
@pytest.fixture(scope="module")
def polaris():
    """Polaris with its own Cairn Store (STS on): a catalog ``snow`` whose tables Polaris vends per-table keys for,
    a read-only principal for Cairn, and two tables."""
    import boto3
    import pyarrow as pa
    from pyiceberg.catalog.rest import RestCatalog

    d = tempfile.mkdtemp(prefix="polaris-")
    sp, pp, mp = port(), port(), port()
    name = f"cairn-polaris-{os.getpid()}"
    store = subprocess.Popen([os.environ["CAIRN_STORE_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **os.environ, "CAIRN_STORE_ACCESS_KEY": STORE[0], "CAIRN_STORE_SECRET_KEY": STORE[1], "CAIRN_STORE_DATA": f"{d}/s",
        "CAIRN_STORE_ADDR": f"0.0.0.0:{sp}", "CAIRN_STORE_BUCKETS": "snowflake", "CAIRN_STORE_FSYNC": "false",
        "CAIRN_STORE_STS_SECRET": "polaris-sts-" + "s" * 32, "CAIRN_STORE_META": ""})
    wait(sp)
    try:
        # The role Polaris assumes (with a session policy per table) when it vends keys.
        iam = boto3.client("iam", endpoint_url=f"http://127.0.0.1:{sp}", aws_access_key_id=STORE[0],
                           aws_secret_access_key=STORE[1], region_name="us-east-1")
        iam.create_role(RoleName="polaris", AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Principal": {"AWS": "*"}, "Action": "sts:AssumeRole"}]}))
        iam.put_role_policy(RoleName="polaris", PolicyName="tables", PolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:*",
                                                     "Resource": ["arn:aws:s3:::snowflake", "arn:aws:s3:::snowflake/*"]}]}))
        _docker("rm", "-f", name, check=False)
        _docker("run", "-d", "--rm", "--name", name, "--network", "host",
                "-e", f"POLARIS_BOOTSTRAP_CREDENTIALS=POLARIS,{ROOT[0]},{ROOT[1]}",
                "-e", f"QUARKUS_HTTP_PORT={pp}", "-e", f"QUARKUS_MANAGEMENT_PORT={mp}",
                "-e", f"AWS_ACCESS_KEY_ID={STORE[0]}", "-e", f"AWS_SECRET_ACCESS_KEY={STORE[1]}", "-e", "AWS_REGION=us-east-1",
                POLARIS)
        end = time.time() + 180
        while True:
            try:
                if httpx.get(f"http://127.0.0.1:{mp}/q/health/ready", timeout=5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.time() < end, "Polaris did not start:\n" + _docker("logs", name, check=False).stdout[-3000:]
            time.sleep(2)
        base = f"http://127.0.0.1:{pp}/api"
        tok = httpx.post(f"{base}/catalog/v1/oauth/tokens", data={
            "grant_type": "client_credentials", "client_id": ROOT[0], "client_secret": ROOT[1],
            "scope": "PRINCIPAL_ROLE:ALL"}, timeout=30).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}

        def ok(r):
            assert r.status_code in (200, 201, 204), r.text
            return r.json() if r.content else {}
        ok(httpx.post(f"{base}/management/v1/catalogs", headers=h, timeout=30, json={"catalog": {
            "name": "snow", "type": "INTERNAL", "properties": {"default-base-location": "s3://snowflake/tables"},
            "storageConfigInfo": {"storageType": "S3", "roleArn": "arn:aws:iam::000000000000:role/polaris",
                                  "allowedLocations": ["s3://snowflake/tables/"], "region": "us-east-1",
                                  "endpoint": f"http://127.0.0.1:{sp}", "stsEndpoint": f"http://127.0.0.1:{sp}",
                                  "pathStyleAccess": True, "kmsUnavailable": True}}}))
        # The writer (root, as the warehouse's own jobs) and a read-only principal for Cairn.
        ok(httpx.put(f"{base}/management/v1/principal-roles/service_admin/catalog-roles/snow", headers=h, timeout=30,
                     json={"catalogRole": {"name": "catalog_admin"}}))
        ok(httpx.put(f"{base}/management/v1/catalogs/snow/catalog-roles/catalog_admin/grants", headers=h, timeout=30,
                     json={"grant": {"type": "catalog", "privilege": "CATALOG_MANAGE_CONTENT"}}))
        reader = ok(httpx.post(f"{base}/management/v1/principals", headers=h, timeout=30,
                               json={"principal": {"name": "cairn-reader"}}))["credentials"]
        ok(httpx.post(f"{base}/management/v1/principal-roles", headers=h, timeout=30,
                      json={"principalRole": {"name": "cairn"}}))
        ok(httpx.put(f"{base}/management/v1/principals/cairn-reader/principal-roles", headers=h, timeout=30,
                     json={"principalRole": {"name": "cairn"}}))
        ok(httpx.post(f"{base}/management/v1/catalogs/snow/catalog-roles", headers=h, timeout=30,
                      json={"catalogRole": {"name": "reader"}}))
        for priv in ("CATALOG_READ_PROPERTIES", "NAMESPACE_LIST", "NAMESPACE_READ_PROPERTIES", "TABLE_LIST",
                     "TABLE_READ_PROPERTIES", "TABLE_READ_DATA"):
            ok(httpx.put(f"{base}/management/v1/catalogs/snow/catalog-roles/reader/grants", headers=h, timeout=30,
                         json={"grant": {"type": "catalog", "privilege": priv}}))
        ok(httpx.put(f"{base}/management/v1/principal-roles/cairn/catalog-roles/snow", headers=h, timeout=30,
                     json={"catalogRole": {"name": "reader"}}))
        cat = RestCatalog("snow", uri=f"{base}/catalog", credential=f"{ROOT[0]}:{ROOT[1]}", scope="PRINCIPAL_ROLE:ALL",
                          warehouse="snow", **{"header.X-Iceberg-Access-Delegation": "vended-credentials"})
        cat.create_namespace("sales")
        cat.create_namespace("hr")
        orders = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "email": ["ann@acme.com", "raj@example.org", "li@acme.com"],
                           "region": ["EU", "US", "EU"]})
        cat.create_table("sales.orders", orders.schema).append(orders)
        pay = pa.table({"name": ["ann"], "salary": pa.array([100], pa.int64())})
        cat.create_table("hr.pay", pay.schema).append(pay)
        yield {"uri": f"{base}/catalog", "store": f"http://127.0.0.1:{sp}", "reader": reader}
    finally:
        _docker("rm", "-f", name, check=False)
        store.terminate()
        store.wait(15)
        shutil.rmtree(d, ignore_errors=True)


def test_snowflake_open_catalog_tables_are_read_in_place_and_governed(trino, cairn, polaris, scopes):  # noqa: F811
    url, s = _connect(trino, cairn)
    admin = "urn:li:corpuser:admin"
    secret = polaris["reader"]["clientSecret"]
    scopes.create(admin, {"name": "snowflake-open-catalog"})
    scopes.put(admin, "snowflake-open-catalog", "client-secret", secret)
    body = {"name": "snow", "preset": "snowflake_open_catalog", "uri": polaris["uri"], "warehouse": "snow",
            "client_id": polaris["reader"]["clientId"], "credential_ref": "scope:snowflake-open-catalog/client-secret",
            "store_endpoint": polaris["store"], "owner": OWNER, "domain": DOMAIN}
    try:
        r = httpx.post(f"{url}/v1/external-catalogs", json={k: v for k, v in body.items() if k != "warehouse"},
                       headers=ADMIN, timeout=30)
        assert r.status_code == 422 and "warehouse" in r.text           # Open Catalog names its catalog
        r = httpx.post(f"{url}/v1/external-catalogs", json=body, headers=ADMIN, timeout=180)
        assert r.status_code == 200, r.text
        out = r.json()
        assert (out["preset"], out["platform"], out["trino"]) == ("snowflake_open_catalog", "snowflake", {"state": "attached"}), out
        assert out["sync"]["tables"] == 2 and out["sync"]["namespaces"] == 2, out
        assert secret not in r.text
        # Trino holds no credential: Polaris's secret stays in the scope; Trino asks Cairn's broker with its own key.
        props = open(f"{trino.dir}/catalog/snow.properties").read()
        assert secret not in props and KEY not in props and "warehouse=snow" in props

        # In Cairn Core as Snowflake datasets, with their schema, owner and domain.
        schema = s.graph.get_aspect(SNOW_ORDERS, "schemaMetadata")
        assert [(f["fieldPath"], f["nativeDataType"]) for f in schema["fields"]] == [
            ("id", "long"), ("email", "string"), ("region", "string")]
        assert s.graph.get_aspect(SNOW_ORDERS, "ownership")["owners"] == [{"owner": OWNER, "type": "DATAOWNER"}]
        assert s.graph.get_aspect(SNOW_PAY, "domains") == {"domains": [DOMAIN]}
        props = s.graph.get_aspect(SNOW_ORDERS, "datasetProperties")["customProperties"]
        assert props["external_catalog_preset"] == "snowflake_open_catalog" and props["iceberg_location"].startswith("s3://snowflake/")
        fields = [dict(f, glossaryTerms={"terms": [{"urn": EMAIL}]}) if f["fieldPath"] == "email" else f
                  for f in schema["fields"]]
        s.graph.aspects[(SNOW_ORDERS, "schemaMetadata")] = {**schema, "fields": fields}

        # Read in place through Trino, as each person: zero copy, the lakehouse's policies.
        q = "SELECT id, email, region FROM snow.sales.orders ORDER BY id"
        assert [r["email"] for r in trino(q)] == ["ann@acme.com", "raj@example.org", "li@acme.com"]
        assert [r["email"] for r in trino(q, user="ana")] == ["a•••@•••.com", "r•••@•••.org", "l•••@•••.com"]
        assert [r["id"] for r in trino(q, user="bob")] == [1, 3]
        with pytest.raises(RuntimeError, match="Access Denied"):
            trino("SELECT * FROM snow.hr.pay", user="ana")
        assert trino("SELECT name FROM snow.hr.pay") == [{"name": "ann"}]
        for stmt in ("INSERT INTO snow.sales.orders VALUES (4, 'x@y.z', 'EU')", "DELETE FROM snow.sales.orders",
                     "CREATE TABLE snow.sales.copy AS SELECT 1 AS x"):
            with pytest.raises(RuntimeError, match="Access Denied"):
                trino(stmt)                                          # read-only, even for the admin

        # A lakehouse table made from it: the Snowflake dataset is its upstream in the graph.
        trino("CREATE TABLE lake.sales.snow_eu AS SELECT id, region FROM snow.sales.orders WHERE region = 'EU'")
        job = f"urn:li:dataJob:(urn:li:dataFlow:(trino,127.0.0.1:{trino.port},PROD),trino:lake.sales.snow_eu)"
        end = time.time() + 30
        while time.time() < end and not s.graph.get_aspect(job, "dataJobInputOutput"):
            time.sleep(0.5)
        io = s.graph.get_aspect(job, "dataJobInputOutput")
        assert io and io["inputDatasets"] == [SNOW_ORDERS], io
        trino("DROP TABLE lake.sales.snow_eu")
    finally:
        httpx.delete(f"{url}/v1/external-catalogs/snow", headers=ADMIN, timeout=120)
    assert s.graph.get_aspect(SNOW_ORDERS, "status") == {"removed": True}


# ---- Snowflake live through Trino's Snowflake connector ---------------------------------------------------------
def test_a_live_snowflake_catalog_is_added_to_trino_with_a_credential_file(trino, cairn, scopes):  # noqa: F811
    """No Snowflake emulator exists: the catalog Cairn makes is checked on a real Trino (its Snowflake connector takes
    the configuration; the user and password reach it only as a file), and its governance in test_warehouse_presets.py."""
    from cairn.dataaccess.external_catalogs import CredentialFiles

    url, s = _connect(trino, cairn)
    ext = s.external_catalogs
    admin = "urn:li:corpuser:admin"
    password = "sf-pass\\w0rd=" + "q" * 16
    scopes.create(admin, {"name": "snowflake-live"})
    scopes.put(admin, "snowflake-live", "password", password)
    # A directory Trino mounts (Kubernetes: a Secret the chart mounts at /etc/trino/federation).
    os.makedirs(f"{trino.dir}/catalog/credentials", exist_ok=True)
    before = ext.credentials
    ext.credentials = CredentialFiles(f"{trino.dir}/catalog/credentials", "/etc/trino/catalog/credentials", 0o644)
    body = {"name": "sfl", "preset": "snowflake_federated", "uri": "https://cairn-ci-test.snowflakecomputing.com",
            "database": "ANALYTICS", "warehouse": "REPORTING_WH", "role": "CAIRN_READER", "client_id": "cairn_reader",
            "credential_ref": "scope:snowflake-live/password", "owner": OWNER, "domain": DOMAIN}
    try:
        r = httpx.post(f"{url}/v1/external-catalogs", json=body, headers=ADMIN, timeout=180)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["trino"]["state"] == "attached", out["trino"]      # Trino 484's Snowflake connector took it
        assert "error" in out["sync"] and password not in r.text       # no account to catalogue here
        assert "sfl" in {row["Catalog"] for row in trino("SHOW CATALOGS")}
        stored = open(f"{trino.dir}/catalog/sfl.properties").read()   # what Trino keeps of the catalog
        assert "connector.name=snowflake" in stored and "snowflake.database=ANALYTICS" in stored
        assert "credential-provider.type=FILE" in stored and "/etc/trino/catalog/credentials/sfl.properties" in stored
        assert password not in stored and "cairn_reader" not in stored
        creds = open(f"{trino.dir}/catalog/credentials/sfl.properties").read()
        assert creds.startswith("connection-user=cairn_reader\n") and "connection-password=sf-pass\\\\w0rd=" in creds
        # A query goes to Snowflake (here: an account that is not there); whatever comes back carries no password.
        with pytest.raises(RuntimeError) as e:
            trino("SELECT * FROM sfl.public.orders")
        assert password not in str(e.value)
        # Removed: gone from Trino, and so is its credential file.
        gone = httpx.delete(f"{url}/v1/external-catalogs/sfl", headers=ADMIN, timeout=120)
        assert gone.status_code == 200 and gone.json()["trino_dropped"] is True
        assert not os.path.exists(f"{trino.dir}/catalog/credentials/sfl.properties")
        assert "sfl" not in {row["Catalog"] for row in trino("SHOW CATALOGS")}
    finally:
        httpx.delete(f"{url}/v1/external-catalogs/sfl", headers=ADMIN, timeout=120)
        ext.credentials = before


# ---- Databricks Unity Catalog (OSS) ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def unity():
    """Unity Catalog OSS with a catalog, two schemas and Iceberg tables made through its Iceberg REST endpoint."""
    from pyiceberg.catalog.rest import RestCatalog
    from pyiceberg.schema import Schema
    from pyiceberg.types import LongType, NestedField, StringType

    p = port()
    name = f"cairn-unity-{os.getpid()}"
    _docker("rm", "-f", name, check=False)
    _docker("run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{p}:8080", UNITY)
    base = f"http://127.0.0.1:{p}/api/2.1/unity-catalog"
    try:
        end = time.time() + 240
        while True:
            try:
                if httpx.get(f"{base}/catalogs", timeout=5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.time() < end, "Unity Catalog did not start:\n" + _docker("logs", name, check=False).stdout[-3000:]
            time.sleep(2)
        r = httpx.post(f"{base}/catalogs", json={"name": "lakehouse_uc", "storage_root": "file:///tmp/cairn-uc"}, timeout=30)
        assert r.status_code == 200, r.text
        for sch in ("sales", "hr"):
            r = httpx.post(f"{base}/schemas", json={"name": sch, "catalog_name": "lakehouse_uc"}, timeout=30)
            assert r.status_code == 200, r.text
        cat = RestCatalog("uc", uri=f"{base}/iceberg", warehouse="lakehouse_uc", token="not-checked")
        cat.create_table("sales.orders", Schema(NestedField(1, "id", LongType(), required=False),
                                                NestedField(2, "email", StringType(), required=False),
                                                NestedField(3, "region", StringType(), required=False)))
        cat.create_table("hr.pay", Schema(NestedField(1, "name", StringType(), required=False),
                                          NestedField(2, "salary", LongType(), required=False)))
        yield {"uri": f"{base}/iceberg"}
    finally:
        _docker("rm", "-f", name, check=False)


def test_unity_catalog_tables_are_catalogued_as_databricks_and_governed(trino, cairn, unity, scopes):  # noqa: F811
    url, s = _connect(trino, cairn)
    admin = "urn:li:corpuser:admin"
    scopes.create(admin, {"name": "databricks"})
    scopes.put(admin, "databricks", "pat", "dapi-" + "d" * 32)          # a personal access token (OSS: not checked)
    body = {"name": "uc", "preset": "databricks_unity", "uri": unity["uri"], "warehouse": "lakehouse_uc",
            "auth": "token", "credential_ref": "scope:databricks/pat", "owner": OWNER, "domain": DOMAIN}
    orders = "urn:li:dataset:(urn:li:dataPlatform:databricks,uc.sales.orders,PROD)"
    try:
        r = httpx.post(f"{url}/v1/external-catalogs", json=body, headers=ADMIN, timeout=180)
        assert r.status_code == 200, r.text
        out = r.json()
        assert (out["platform"], out["trino"]["state"]) == ("databricks", "attached"), out
        assert out["sync"]["tables"] == 2, out
        assert "d" * 32 not in r.text
        schema = s.graph.get_aspect(orders, "schemaMetadata")
        assert [f["fieldPath"] for f in schema["fields"]] == ["id", "email", "region"]
        assert s.graph.get_aspect(orders, "ownership")["owners"][0]["owner"] == OWNER
        assert s.graph.get_aspect(orders, "domains") == {"domains": [DOMAIN]}
        # Through Trino: Unity Catalog's tables are listed for those who may see them, hidden from the rest.
        assert {r["Table"] for r in trino("SHOW TABLES FROM uc.sales")} == {"orders"}
        assert trino("SHOW TABLES FROM uc.hr", user="ana") == []
        assert {r["Table"] for r in trino("SHOW TABLES FROM uc.hr")} == {"pay"}
        with pytest.raises(RuntimeError, match="Access Denied"):
            trino("SELECT * FROM uc.hr.pay", user="ana")
        with pytest.raises(RuntimeError, match="Access Denied"):
            trino("INSERT INTO uc.sales.orders VALUES (1, 'a@b.c', 'EU')")
    finally:
        httpx.delete(f"{url}/v1/external-catalogs/uc", headers=ADMIN, timeout=120)


# ---- Databricks Delta Sharing (recipient) ---------------------------------------------------------------------
class Provider:
    """A Delta Sharing provider for one share: the protocol's version and query endpoints over a real Delta table
    (its log read with ``deltalake``), its Parquet files as pre-signed Cairn Store URLs."""

    def __init__(self, table_uri: str, storage: dict, s3, bucket: str, token: str):
        from deltalake import DeltaTable

        prov = self
        self.queries = 0

        class H(BaseHTTPRequestHandler):
            def _send(self, status, body, headers=None):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _table(self):
                if self.headers.get("authorization") != f"Bearer {token}":
                    self._send(401, {"errorCode": "UNAUTHENTICATED", "message": "no"})
                    return None
                if not self.path.startswith("/delta-sharing/shares/sales_share/schemas/sales/tables/orders/"):
                    self._send(404, {"errorCode": "RESOURCE_DOES_NOT_EXIST", "message": "no such table"})
                    return None
                return DeltaTable(table_uri, storage_options=storage)

            def do_GET(self):
                t = self._table()
                if t is not None and self.path.endswith("/version"):
                    self._send(200, b"", {"Delta-Table-Version": str(t.version())})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length") or 0))
                t = self._table()
                if t is None or not self.path.endswith("/query"):
                    return
                prov.queries += 1
                md = t.metadata()
                lines = [{"protocol": {"minReaderVersion": 1}},
                         {"metaData": {"id": md.id, "format": {"provider": "parquet"},
                                       "schemaString": t.schema().to_json(), "partitionColumns": md.partition_columns}}]
                import pyarrow as pa

                adds = pa.table(t.get_add_actions(flatten=True)).to_pylist()
                for a in adds:
                    key = f"{table_uri.split('/', 3)[3]}/{a['path']}"
                    url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=900)
                    parts = {k.removeprefix("partition."): v for k, v in a.items() if k.startswith("partition.")}
                    lines.append({"file": {"url": url, "id": a["path"], "partitionValues": parts, "size": a["size_bytes"],
                                           "stats": json.dumps({"numRecords": a.get("num_records")})}})
                self._send(200, "\n".join(json.dumps(x) for x in lines).encode(),
                           {"Delta-Table-Version": str(t.version()), "content-type": "application/x-ndjson"})

            def log_message(self, *a):
                pass
        self.port = port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.endpoint = f"http://127.0.0.1:{self.port}/delta-sharing"


def test_a_delta_sharing_table_is_read_as_a_recipient_into_a_governed_table(trino, cairn, lake, scopes, monkeypatch):  # noqa: F811
    import boto3
    import pyarrow as pa
    from botocore.config import Config
    from deltalake import write_deltalake

    from cairn.pipelines.saas import SaasSyncs

    _, s = cairn
    owner = "urn:li:corpuser:admin"
    storage = {"AWS_ENDPOINT_URL": lake["store"], "AWS_ACCESS_KEY_ID": STORE_KEY[0], "AWS_SECRET_ACCESS_KEY": STORE_KEY[1],
               "AWS_REGION": "us-east-1", "AWS_ALLOW_HTTP": "true", "AWS_S3_ALLOW_UNSAFE_RENAME": "true",
               "AWS_VIRTUAL_HOSTED_STYLE_REQUEST": "false"}
    uri = f"s3://lakehouse/databricks-share/orders-{os.getpid()}"
    first = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "email": ["ann@acme.com", "raj@example.org", "li@acme.com"],
                      "region": ["EU", "US", "EU"], "amount": pa.array([10.5, 20.0, 7.25], pa.float64())})
    write_deltalake(uri, first, storage_options=storage, partition_by=["region"])   # partition values live in the log
    s3 = boto3.client("s3", endpoint_url=lake["store"], aws_access_key_id=STORE_KEY[0], aws_secret_access_key=STORE_KEY[1],
                      region_name="us-east-1", config=Config(s3={"addressing_style": "path"}))
    token = "dss-" + "b" * 40
    provider = Provider(uri, storage, s3, "lakehouse", token)
    monkeypatch.setenv("CAIRN_SAAS_ALLOW_HTTP", "1")                       # the provider stand-in serves plain HTTP
    monkeypatch.setenv("CAIRN_SAAS_ALLOW_PRIVATE_NETWORKS", "true")       # ... on this machine's loopback
    profile = {"shareCredentialsVersion": 1, "endpoint": provider.endpoint, "bearerToken": token,
               "expirationTime": "2099-01-01T00:00:00.000Z"}
    scopes.create(owner, {"name": "databricks-share"})
    scopes.put(owner, "databricks-share", "profile", json.dumps(profile))
    syncs = SaasSyncs(s.engine, {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                                 "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                                 "s3.region": "us-east-1", "s3.path-style-access": "true"},
                      scopes.read, lambda who, urn: who == owner, graph=s.graph)
    target = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.shared_orders,PROD)"
    shared = "urn:li:dataset:(urn:li:dataPlatform:databricks,sales_share.sales.orders,PROD)"
    # Its email column is labelled in the catalog (before anyone touches it: Cairn keeps a table's labels briefly).
    s.graph.aspects[(target, "schemaMetadata")] = {"fields": [
        {"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}}, {"fieldPath": "region"}]}
    trino("DROP TABLE IF EXISTS lake.sales.shared_orders")
    try:
        syncs.save(owner, {"name": "shared-orders", "kind": "delta_sharing", "object": "sales_share.sales.orders",
                           "fields": ["id", "email", "region"], "target": "sales.shared_orders",
                           "secret_scope": "databricks-share"})
        got =syncs.run_now(owner, "shared-orders", wait=True)
        assert got["status"] == "ok" and got["rows"] == 3 and got["cursor"] == "0", got
        rows = trino("SELECT id, email, region FROM lake.sales.shared_orders ORDER BY id")
        assert rows == [{"id": 1, "email": "ann@acme.com", "region": "EU"}, {"id": 2, "email": "raj@example.org", "region": "US"},
                        {"id": 3, "email": "li@acme.com", "region": "EU"}]
        assert "amount" not in {c["Column"] for c in trino("DESCRIBE lake.sales.shared_orders")}   # only what was asked
        # In Cairn Core: the shared table as a Databricks dataset, upstream of the governed table.
        assert [f["fieldPath"] for f in s.graph.get_aspect(shared, "schemaMetadata")["fields"]] == ["id", "email", "region", "amount"]
        assert s.graph.get_aspect(shared, "datasetProperties")["customProperties"]["delta_sharing_version"] == "0"
        assert [u["dataset"] for u in s.graph.get_aspect(target, "upstreamLineage")["upstreams"]] == [shared]
        # Governed like any lakehouse table: masked for ana (the column is labelled), filtered for bob.
        assert [r["email"] for r in trino("SELECT email FROM lake.sales.shared_orders ORDER BY id", user="ana")] == \
            ["a•••@•••.com", "r•••@•••.org", "l•••@•••.com"]
        assert [r["id"] for r in trino("SELECT id FROM lake.sales.shared_orders ORDER BY id", user="bob")] == [1, 3]
        # Unchanged at the provider: nothing is read again. A new version replaces the table in one commit.
        assert syncs.run_now(owner, "shared-orders", wait=True)["rows"] == 0 and provider.queries == 2
        write_deltalake(uri, pa.table({"id": pa.array([4], pa.int64()), "email": ["mo@acme.com"], "region": ["US"],
                                       "amount": pa.array([1.0], pa.float64())}), mode="append", storage_options=storage,
                        partition_by=["region"])
        again = syncs.run_now(owner, "shared-orders", wait=True)
        assert again["rows"] == 4 and again["cursor"] == "1", again
        assert [r["id"] for r in trino("SELECT id FROM lake.sales.shared_orders ORDER BY id")] == [1, 2, 3, 4]
        # The provider takes the share away: the run fails, says so, and the table keeps its last good version.
        scopes.put(owner, "databricks-share", "profile", json.dumps({**profile, "bearerToken": "revoked-" + "x" * 20}))
        failed = syncs.run_now(owner, "shared-orders", wait=True)
        assert failed["status"] == "failed" and "HTTP 401" in failed["error"] and token not in failed["error"], failed
        assert len(trino("SELECT id FROM lake.sales.shared_orders")) == 4
    finally:
        provider.server.shutdown()
        trino("DROP TABLE IF EXISTS lake.sales.shared_orders")
