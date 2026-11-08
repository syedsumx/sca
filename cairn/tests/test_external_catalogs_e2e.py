"""External Iceberg catalogs, governed: a second, real Cairn Catalog and Cairn Store stand for another
organisation's catalog. An operator registers it with a reference to its OAuth client secret; Cairn adds it to a
real Trino (dynamic catalogs) with no credential in Trino, catalogs its tables, and Trino's access control governs
it like the lakehouse: one table readable with its labelled column masked (and a row filter), one refused, writes
refused (read-only). Cairn's token broker is the only holder of the secret.

Needs docker and the store and catalog binaries (CAIRN_STORE_BIN, CAIRN_CATALOG_BIN)."""

import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
from urllib.parse import parse_qs

import httpx
import pytest

from test_trino_e2e import EMAIL, KEY, _ready, cairn, lake, port, trino, wait  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, CAIRN_STORE_BIN and CAIRN_CATALOG_BIN")

PARTNER_TOKEN = "partner-catalog-" + "t" * 24            # what the partner's catalog accepts
CLIENT_ID, CLIENT_SECRET = "cairn-at-partner", "partner-client-secret-" + "c" * 16
ADMIN = {"X-Cairn-Actor": "urn:li:corpuser:admin"}
ANA = {"X-Cairn-Actor": "urn:li:corpuser:ana"}
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,partner.sales.orders,PROD)"
PAY = "urn:li:dataset:(urn:li:dataPlatform:iceberg,partner.hr.pay,PROD)"


class IdP:
    """The partner's identity provider: client credentials -> the catalog's token (a stand-in for theirs)."""

    def __init__(self):
        idp = self
        self.calls: list[dict] = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers.get("content-length") or 0)).decode()).items()}
                idp.calls.append(form)
                ok = form.get("client_id") == CLIENT_ID and form.get("client_secret") == CLIENT_SECRET \
                    and form.get("grant_type") == "client_credentials"
                out = json.dumps({"access_token": PARTNER_TOKEN, "token_type": "bearer", "expires_in": 600} if ok
                                 else {"error": "invalid_client"}).encode()
                self.send_response(200 if ok else 401)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass
        self.port = port()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}/oauth/token"


@pytest.fixture(scope="module")
def partner():
    """Another organisation's lakehouse: its own store and catalog, with tables in two namespaces."""
    from pyiceberg.catalog.rest import RestCatalog
    import pyarrow as pa

    d = tempfile.mkdtemp(prefix="partner-")
    sp, cp = port(), port()
    key = ("partner-store", "partner-store-secret-" + "s" * 12)
    procs = [subprocess.Popen([os.environ["CAIRN_STORE_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **os.environ, "CAIRN_STORE_ACCESS_KEY": key[0], "CAIRN_STORE_SECRET_KEY": key[1], "CAIRN_STORE_DATA": f"{d}/s",
        "CAIRN_STORE_ADDR": f"0.0.0.0:{sp}", "CAIRN_STORE_BUCKETS": "partner", "CAIRN_STORE_FSYNC": "false",
        "CAIRN_STORE_STS_SECRET": "partner-sts-" + "s" * 32,
        "CAIRN_STORE_META": ""})]
    wait(sp)
    procs.append(subprocess.Popen([os.environ["CAIRN_CATALOG_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **os.environ, "CAIRN_CATALOG_ADDR": f"0.0.0.0:{cp}", "CAIRN_CATALOG_DATA": f"{d}/c", "CAIRN_CATALOG_TOKEN": PARTNER_TOKEN,
        "CAIRN_CATALOG_S3_ENDPOINT": f"http://127.0.0.1:{sp}", "CAIRN_CATALOG_S3_ACCESS_KEY": key[0],
        "CAIRN_CATALOG_S3_SECRET_KEY": key[1], "CAIRN_CATALOG_WAREHOUSE": "s3://partner/warehouse",
        "CAIRN_CATALOG_FSYNC": "false", "CAIRN_CATALOG_VEND_ROLE": "partner-tables"}))   # scoped keys per table
    wait(cp)
    try:
        cat = RestCatalog("partner", uri=f"http://127.0.0.1:{cp}", token=PARTNER_TOKEN)
        cat.create_namespace("sales")
        cat.create_namespace("hr")
        orders = pa.table({"id": pa.array([1, 2, 3], pa.int64()),
                           "email": ["ann@partner.com", "raj@example.org", "li@partner.com"], "region": ["EU", "US", "EU"]})
        cat.create_table("sales.orders", orders.schema).append(orders)
        pay = pa.table({"name": ["ann"], "salary": pa.array([100], pa.int64())})
        cat.create_table("hr.pay", pay.schema).append(pay)
        yield {"catalog": f"http://127.0.0.1:{cp}", "store": f"http://127.0.0.1:{sp}"}
    finally:
        for p in reversed(procs):
            p.terminate()
            p.wait(15)
        shutil.rmtree(d, ignore_errors=True)


def test_an_external_catalog_is_queried_through_trino_and_governed(trino, cairn, partner):  # noqa: F811
    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    idp = IdP()
    ext = s.external_catalogs
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    ext.trino_connection, ext.broker_url = "lakehouse", url
    os.environ["CAIRN_EXTCAT_PARTNER"] = CLIENT_SECRET
    try:
        body = {"name": "partner", "uri": partner["catalog"], "auth": "oauth2", "client_id": CLIENT_ID,
                "credential_ref": "env:CAIRN_EXTCAT_PARTNER", "token_url": idp.url, "store_endpoint": partner["store"]}
        # Only operators register; never with a credential in the request; never pointing at Cairn's own secrets.
        assert httpx.post(f"{url}/v1/external-catalogs", json=body, headers=ANA, timeout=30).status_code == 403
        r = httpx.post(f"{url}/v1/external-catalogs", json={**body, "client_secret": CLIENT_SECRET}, headers=ADMIN, timeout=30)
        assert r.status_code == 422 and CLIENT_SECRET not in r.text
        r = httpx.post(f"{url}/v1/external-catalogs", json={**body, "credential_ref": "env:CAIRN_TRINO_ACCESS_KEY"},
                       headers=ADMIN, timeout=30)
        assert r.status_code == 422
        r = httpx.post(f"{url}/v1/external-catalogs", json={**body, "name": "lake"}, headers=ADMIN, timeout=30)
        assert r.status_code == 409                       # one of Cairn's own catalogs
        os.environ["CAIRN_EXTCAT_WRONG"] = "not-the-secret"
        r = httpx.post(f"{url}/v1/external-catalogs", json={**body, "credential_ref": "env:CAIRN_EXTCAT_WRONG"},
                       headers=ADMIN, timeout=30)
        assert r.status_code == 502 and "HTTP 401" in r.text, r.text   # the IdP refused it

        r = httpx.post(f"{url}/v1/external-catalogs", json=body, headers=ADMIN, timeout=180)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["trino"] == {"state": "attached"}, out
        assert out["sync"]["tables"] == 2 and out["sync"]["namespaces"] == 2, out
        assert out["writable"] is False and CLIENT_SECRET not in r.text and PARTNER_TOKEN not in r.text
        listed = httpx.get(f"{url}/v1/external-catalogs", headers=ADMIN, timeout=30)
        assert [c["name"] for c in listed.json()["catalogs"]] == ["partner"] and CLIENT_SECRET not in listed.text
        assert any(c.get("client_secret") == CLIENT_SECRET for c in idp.calls)   # the secret went to its IdP only

        # Trino holds no credential: the catalog refers to its own key for Cairn's broker.
        props = open(f"{trino.dir}/catalog/partner.properties").read()
        assert CLIENT_SECRET not in props and PARTNER_TOKEN not in props and KEY not in props
        assert "${ENV\\:CAIRN_TRINO_ACCESS_KEY}" in props or "${ENV:CAIRN_TRINO_ACCESS_KEY}" in props
        # The broker answers Trino's key only.
        bad = httpx.post(f"{url}/v1/external-catalogs/oauth/tokens", timeout=30, data={
            "grant_type": "client_credentials", "client_id": "partner", "client_secret": "guess-" + "g" * 24})
        assert bad.status_code == 401 and PARTNER_TOKEN not in bad.text
        good = httpx.post(f"{url}/v1/external-catalogs/oauth/tokens", timeout=30, data={
            "grant_type": "client_credentials", "client_id": "partner", "client_secret": KEY})
        assert good.status_code == 200 and good.json()["access_token"] == PARTNER_TOKEN

        # In Cairn's catalog under the names access control uses; a column labelled in Cairn survives a sync.
        schema = s.graph.get_aspect(ORDERS, "schemaMetadata")
        assert [(f["fieldPath"], f["nativeDataType"]) for f in schema["fields"]] == [
            ("id", "long"), ("email", "string"), ("region", "string")]
        assert s.graph.get_aspect(PAY, "datasetProperties")["customProperties"]["external_catalog"] == "partner"
        fields = [dict(f, glossaryTerms={"terms": [{"urn": EMAIL}]}) if f["fieldPath"] == "email" else f
                  for f in schema["fields"]]
        s.graph.aspects[(ORDERS, "schemaMetadata")] = {**schema, "fields": fields}
        again = httpx.post(f"{url}/v1/external-catalogs/partner/sync", headers=ADMIN, timeout=120)
        assert again.status_code == 200 and again.json()["removed"] == 0, again.text
        email = next(f for f in s.graph.get_aspect(ORDERS, "schemaMetadata")["fields"] if f["fieldPath"] == "email")
        assert email["glossaryTerms"]["terms"] == [{"urn": EMAIL}]

        # Governed in Trino like the lakehouse.
        q = "SELECT id, email, region FROM partner.sales.orders ORDER BY id"
        assert [r["email"] for r in trino(q)] == ["ann@partner.com", "raj@example.org", "li@partner.com"]
        assert [r["email"] for r in trino(q, user="ana")] == ["a•••@•••.com", "r•••@•••.org", "l•••@•••.com"]
        assert [r["id"] for r in trino(q, user="bob")] == [1, 3]                    # bob's row filter
        with pytest.raises(RuntimeError, match="Access Denied"):
            trino("SELECT * FROM partner.hr.pay", user="ana")
        assert trino("SELECT name FROM partner.hr.pay") == [{"name": "ann"}]
        # Read-only: no writes, table or schema changes, even for the admin.
        for stmt in ("INSERT INTO partner.sales.orders VALUES (4, 'x@y.z', 'EU')", "DELETE FROM partner.sales.orders",
                     "CREATE TABLE partner.sales.copy AS SELECT 1 AS x", "CREATE SCHEMA partner.scratch"):
            with pytest.raises(RuntimeError, match="Access Denied"):
                trino(stmt)
        # Nobody but Cairn's catalog administrator adds or drops catalogs.
        with pytest.raises(RuntimeError, match="Access Denied"):
            trino("DROP CATALOG partner")
        assert httpx.post(f"{url}/v1/trino/token", headers={"X-Cairn-Actor": "urn:li:corpuser:cairn-catalogs"},
                          timeout=30).status_code == 403

        # A restarted coordinator loses it: Cairn adds it again.
        ext._trino('DROP CATALOG IF EXISTS "partner"')
        assert ext.reconcile() == {"added": ["partner"]}
        assert len(trino(q, user="ana")) == 3

        # Removed: gone from Trino, its datasets marked removed.
        gone = httpx.delete(f"{url}/v1/external-catalogs/partner", headers=ADMIN, timeout=120)
        assert gone.status_code == 200 and gone.json() == {"name": "partner", "trino_dropped": True, "datasets_removed": 2}
        assert s.graph.get_aspect(ORDERS, "status") == {"removed": True}
        with pytest.raises(RuntimeError):
            trino(q)
    finally:
        os.environ.pop("CAIRN_EXTCAT_PARTNER", None)
        os.environ.pop("CAIRN_EXTCAT_WRONG", None)
        idp.server.shutdown()
