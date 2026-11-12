"""Snowflake and Databricks tables in place (L151), without a cloud account: the warehouse presets of external
catalogs (what each one needs, the defaults it implies), the live Snowflake catalog Trino gets (its connector
properties, a credential file instead of a password, read from a secret scope as the registering operator), its
governance through the same HTTP endpoints Trino's access control calls (grants, masks, row filters, read-only,
refused when the decision cannot be made), lineage named as Snowflake datasets, the Kubernetes Secret target, and
the Delta Sharing recipient's profile and types.

The Iceberg presets against real catalogs (Apache Polaris for Snowflake Open Catalog, Unity Catalog OSS) and the
Delta Sharing reader on a real Delta table are in tests/test_warehouse_inplace_e2e.py."""

import base64
import http.server
import json
import os
import stat
import threading

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.dataaccess.external_catalogs import (CredentialFiles, CredentialSecret, ExternalCatalogError,
                                                ExternalCatalogs, properties_text)
from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target
from cairn.services import build_services

KEY = "trino-key-" + "k" * 24
ADMIN, ANA, BOB = (f"urn:li:corpuser:{u}" for u in ("admin", "ana", "bob"))
EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"
PASSWORD = "sn0w\\fl=ke:pässword " + "p" * 12            # a backslash, = and : and a non-ASCII letter
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,sfl.sales.orders,PROD)"
SF = {"name": "sfl", "preset": "snowflake_federated", "uri": "https://acme-analytics.snowflakecomputing.com",
      "database": "ANALYTICS", "warehouse": "REPORTING_WH", "role": "CAIRN_READER", "client_id": "cairn_reader",
      "credential_ref": "scope:snowflake/password", "owner": "urn:li:corpGroup:sales-eng", "domain": "urn:li:domain:sales"}


@pytest.fixture
def client(settings, graph, monkeypatch, tmp_path):
    monkeypatch.setenv("CAIRN_TRINO_ACCESS_KEY", KEY)
    monkeypatch.setenv("CAIRN_TRINO_CATALOGS", '{"lakehouse": {"platform": "iceberg", "database": ""}}')
    monkeypatch.setenv("CAIRN_SECRETS_KEY", base64.b64encode(b"s" * 32).decode())
    monkeypatch.setenv("CAIRN_EXTERNAL_CATALOGS_CREDENTIALS_DIR", str(tmp_path / "creds"))
    monkeypatch.setenv("CAIRN_EXTERNAL_CATALOGS_TRINO_CREDENTIALS_DIR", "/etc/trino/federation")
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    sub = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition=sub(ADMIN)),
        Policy(id="see-sales", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*sales.*"])),
        Policy(id="mask-pii", effect="obligation", actions=["VIEW_DATASET_PROFILE"],
               obligations=[Obligation(type="mask_columns", params={"labels": ["PII"], "strategy": "partial"})],
               condition={"attr": "subject.urn", "op": "neq", "value": ADMIN}),
        Policy(id="eu-only", effect="obligation", actions=["VIEW_DATASET_PROFILE"], target=Target(resource_urns=["*sales.*"]),
               obligations=[Obligation(type="row_filter", params={"expression": "region = 'EU'"})], condition=sub(BOB)),
    ]))
    c = TestClient(create_api_app(s))
    c.services, c.creds = s, tmp_path / "creds"
    return c


def java_properties(text: str) -> dict:
    """What java.util.Properties.load reads from ``key=value`` lines (the escapes properties_text writes)."""
    out = {}
    for line in text.splitlines():
        k, _, v = line.partition("=")
        val, i, u16 = [], 0, []
        while i < len(v):
            ch = v[i]
            if ch == "\\" and v[i + 1] == "u":
                u16.append(int(v[i + 2:i + 6], 16))
                i += 6
                continue
            if u16:
                val.append(b"".join(x.to_bytes(2, "big") for x in u16).decode("utf-16-be"))
                u16 = []
            if ch == "\\":
                val.append({"n": "\n", "r": "\r", "t": "\t", "f": "\f"}.get(v[i + 1], v[i + 1]))
                i += 2
                continue
            val.append(ch)
            i += 1
        if u16:
            val.append(b"".join(x.to_bytes(2, "big") for x in u16).decode("utf-16-be"))
        out[k] = "".join(val)
    return out


def post(c, body, who=ADMIN):
    return c.post("/v1/external-catalogs", json=body, headers={"X-Cairn-Actor": who})


def ask(c, path, user, op, resource=None):
    body = {"input": {"context": {"identity": {"user": user, "groups": []}, "softwareStack": {"trinoVersion": "484"}},
                      "action": {"operation": op, **({"resource": resource} if resource else {})}}}
    r = c.post(f"/v1/trino/{KEY}/{path}", json=body)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def table(t, schema="sales", catalog="sfl", **kw):
    return {"table": {"catalogName": catalog, "schemaName": schema, "tableName": t, **kw}}


def column(t, col, schema="sales", catalog="sfl"):
    return {"column": {"catalogName": catalog, "schemaName": schema, "tableName": t, "columnName": col,
                       "columnType": "varchar"}}


def test_presets_say_what_they_need(client):
    c = client
    base = {"name": "snow", "uri": "https://acme-org.snowflakecomputing.com/polaris/api/catalog",
            "credential_ref": "env:CAIRN_EXTCAT_SNOW"}
    cases = [
        ({**base, "preset": "teradata"}, "preset is one of"),
        ({**base, "preset": "snowflake_open_catalog", "client_id": "x"}, "needs warehouse"),
        ({**base, "preset": "snowflake_open_catalog", "warehouse": "w", "auth": "token"}, "auth for preset snowflake_open_catalog"),
        ({**base, "preset": "snowflake_open_catalog", "warehouse": "w"}, "needs client_id"),
        ({**base, "preset": "snowflake_horizon", "warehouse": "DB"}, "session:role:<ROLE>"),
        ({**base, "preset": "databricks_unity", "uri": "https://dbc-1.cloud.databricks.com/api/2.1/unity-catalog/iceberg-rest"},
         "needs warehouse"),
        ({**base, "preset": "databricks_unity", "warehouse": "main", "database": "X"}, "database and role are for"),
        ({**SF, "uri": "https://evil.example.com"}, "snowflakecomputing.com"),
        ({**SF, "database": None}, "needs database"),
        ({**SF, "client_id": None}, "the Snowflake user"),
        ({**SF, "role": "R; DROP"}, "a Snowflake name"),
        ({**SF, "owner": "ana"}, "owner is urn:li:corpuser"),
        ({**SF, "domain": "sales"}, "domain is urn:li:domain"),
        ({**SF, "password": PASSWORD}, "never a credential"),
    ]
    for body, why in cases:
        r = post(c, body)
        assert r.status_code == 422 and why in r.text, (body, r.text)
        assert PASSWORD not in r.text
    assert post(c, SF, who=ANA).status_code == 403                  # operators only


def test_oauth_defaults_follow_the_preset():
    e = ExternalCatalogs.oauth_endpoint
    uc = {"preset": "databricks_unity", "uri": "https://dbc-1.cloud.databricks.com/api/2.1/unity-catalog/iceberg-rest"}
    assert e(uc) == ("https://dbc-1.cloud.databricks.com/oidc/v1/token", "all-apis")
    oc = {"preset": "snowflake_open_catalog", "uri": "https://o-a.snowflakecomputing.com/polaris/api/catalog"}
    assert e(oc) == ("https://o-a.snowflakecomputing.com/polaris/api/catalog/v1/oauth/tokens", "PRINCIPAL_ROLE:ALL")
    hz = {"preset": "snowflake_horizon", "uri": "https://a.snowflakecomputing.com/polaris/api/catalog",
          "oauth_scope": "session:role:ANALYST"}
    assert e(hz) == ("https://a.snowflakecomputing.com/polaris/api/catalog/v1/oauth/tokens", "session:role:ANALYST")
    assert e({"uri": "https://cat.example/iceberg", "token_url": "https://idp.example/t", "oauth_scope": "x"}) == \
        ("https://idp.example/t", "x")


def test_a_live_snowflake_catalog_gets_a_credential_file_never_a_password(client):
    c, s = client, client.services
    ext = s.external_catalogs
    # The password is in a secret scope the registering operator can read; nobody else can.
    s.secret_scopes.create(ADMIN, {"name": "snowflake"})
    s.secret_scopes.put(ADMIN, "snowflake", "password", PASSWORD)
    r = post(c, SF)
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["preset"], out["platform"], out["connector"], out["database"], out["role"]) == \
        ("snowflake_federated", "snowflake", "snowflake", "ANALYTICS", "CAIRN_READER")
    assert "error" in out["sync"]                       # no Trino here: catalogued once Trino has it
    assert PASSWORD not in r.text and PASSWORD not in c.get("/v1/external-catalogs", headers={"X-Cairn-Actor": ADMIN}).text

    row = ext._row("sfl")
    sql = ext.catalog_sql(row)
    assert sql.startswith('CREATE CATALOG "sfl" USING snowflake WITH (')
    props = ext.snowflake_properties(row)
    assert props == {"connection-url": "jdbc:snowflake://acme-analytics.snowflakecomputing.com",
                     "snowflake.account": "acme-analytics", "snowflake.database": "ANALYTICS",
                     "snowflake.warehouse": "REPORTING_WH", "snowflake.role": "CAIRN_READER",
                     "credential-provider.type": "FILE",
                     "connection-credential-file": "/etc/trino/federation/sfl.properties"}
    assert PASSWORD not in sql and "cairn_reader" not in sql          # the statement Trino records names a file

    # Attaching writes the file (as the operator who registered it), then Trino is asked: with no Trino it fails,
    # and the file stays for the next attempt (a mounted Secret reaches Trino a little later).
    ext.trino_connection = "nowhere"
    assert ext.attach("sfl", ADMIN)["state"] == "failed"
    f = c.creds / "sfl.properties"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o640
    text = f.read_text()
    assert text.startswith("connection-user=cairn_reader\nconnection-password=")
    assert PASSWORD not in text                                         # escaped as Java properties
    assert java_properties(text) == {"connection-user": "cairn_reader", "connection-password": PASSWORD}

    # The secret is gone from the scope: the next attach removes the sign-in (fail closed) and says why.
    s.secret_scopes.remove(ADMIN, "snowflake", "password")
    got = ext.attach("sfl", ADMIN)
    assert got["state"] == "failed" and "cannot be read" in got["error"] and not f.exists()
    assert PASSWORD not in json.dumps(got)

    # Removed: the registration and its file go.
    s.secret_scopes.put(ADMIN, "snowflake", "password", PASSWORD)
    ext._write_credentials(ext._row("sfl"))
    assert f.exists()
    assert c.delete("/v1/external-catalogs/sfl", headers={"X-Cairn-Actor": ADMIN}).status_code == 200
    assert not f.exists()
    audit = json.dumps(s.audit.entries(category="external_catalogs", limit=100), default=str)
    assert "external_catalog.registered" in audit and PASSWORD not in audit


def test_live_snowflake_tables_are_governed_like_the_lakehouse(client):
    c, s = client, client.services
    s.secret_scopes.create(ADMIN, {"name": "snowflake"})
    s.secret_scopes.put(ADMIN, "snowflake", "password", PASSWORD)
    assert post(c, SF).status_code == 200
    s.graph.aspects[(ORDERS, "schemaMetadata")] = {"fields": [
        {"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}}, {"fieldPath": "region"}]}
    # Named as Snowflake datasets: what policies, masks and lineage refer to.
    assert s.trino_access.dataset_name("sfl", "sales", "orders") == ("snowflake", "sfl.sales.orders", "PROD")
    assert ask(c, "allow", "ana", "SelectFromColumns", table("orders", columns=["id", "email"])) is True
    assert ask(c, "allow", "ana", "SelectFromColumns", table("pay", "hr", columns=["salary"])) is False
    mask = ask(c, "column-mask", "ana", "GetColumnMask", column("orders", "email"))
    assert mask and "expression" in mask, mask
    assert ask(c, "column-mask", "admin", "GetColumnMask", column("orders", "email")) is None
    assert ask(c, "row-filters", "bob", "GetRowFilters", table("orders")) == [{"expression": "region = 'EU'"}]
    # Read-only unless registered writable: no writes, even for the admin.
    for op in ("InsertIntoTable", "DeleteFromTable", "DropTable", "CreateTable"):
        assert ask(c, "allow", "admin", op, table("orders")) is False, op
    # A federated query's lineage lands on the Snowflake dataset.
    ev = {"inputs": [{"namespace": "trino://trino:8080", "name": "sfl.sales.orders"}], "outputs": []}
    assert s.trino_access.remap_lineage(ev)["inputs"][0] == {"namespace": "snowflake://", "name": "sfl.sales.orders"}

    # When the catalog's registration cannot be read, Trino is refused: no allow, and Trino's OPA plugin takes an
    # error answer as a denial.
    def broken(catalog):
        raise RuntimeError("database away")
    real = s.trino_access.external_catalog
    s.trino_access.external_catalog = broken
    quiet = TestClient(c.app, raise_server_exceptions=False)
    try:
        ctx = {"identity": {"user": "admin", "groups": []}}
        r = quiet.post(f"/v1/trino/{KEY}/allow", json={"input": {"context": ctx, "action": {
            "operation": "SelectFromColumns", "resource": table("orders", columns=["id"])}}})
        assert r.status_code != 200 or r.json()["result"] is False, r.text
        r = quiet.post(f"/v1/trino/{KEY}/row-filters", json={"input": {"context": {**ctx, "identity": {
            "user": "bob", "groups": []}}, "action": {"operation": "GetRowFilters", "resource": table("orders")}}})
        assert r.status_code != 200 or r.json()["result"] == [{"expression": "false"}], r.text
    finally:
        s.trino_access.external_catalog = real


def test_live_snowflake_is_refused_without_a_credential_target(client):
    c, s = client, client.services
    s.external_catalogs.credentials = None
    r = post(c, SF)
    assert r.status_code == 503 and "CREDENTIALS_DIR" in r.text


def test_properties_are_escaped_as_java_reads_them():
    text = properties_text({"connection-user": " lead", "connection-password": "a\\b\nc=d:é😀"})
    assert text == "connection-user=\\ lead\nconnection-password=a\\\\b\\nc=d:\\u00e9\\ud83d\\ude00\n"


def test_the_kubernetes_secret_is_patched_and_made_once(tmp_path):
    """Against a stand-in for the Kubernetes API (the cluster's own is tested by Helm in CI): a merge patch of one
    key, a create when the Secret is not there yet, a key removed with null, and errors that carry no value."""
    calls = []

    class H(http.server.BaseHTTPRequestHandler):
        def _go(self):
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            calls.append((self.command, self.path, self.headers.get("content-type"), self.headers.get("authorization"),
                          json.loads(body) if body else None))
            status = 404 if (self.command == "PATCH" and len(calls) == 1) else (201 if self.command == "POST" else 200)
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
        do_PATCH = do_POST = _go

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    (tmp_path / "token").write_text("sa-token")
    try:
        sink = CredentialSecret("cairn-trino-federation", "cairn", "/etc/trino/federation",
                                api=f"http://127.0.0.1:{srv.server_port}", token_file=str(tmp_path / "token"))
        assert sink.path("sfl") == "/etc/trino/federation/sfl.properties"
        sink.write("sfl", {"connection-user": "u", "connection-password": PASSWORD})
        sink.remove("sfl")
    finally:
        srv.shutdown()
    (m1, p1, t1, a1, b1), (m2, p2, _, _, b2), (m3, _, t3, _, b3) = calls
    assert (m1, p1, t1, a1) == ("PATCH", "/api/v1/namespaces/cairn/secrets/cairn-trino-federation",
                                "application/merge-patch+json", "Bearer sa-token")
    assert (m2, p2) == ("POST", "/api/v1/namespaces/cairn/secrets") and b2["metadata"]["name"] == "cairn-trino-federation"
    assert base64.b64decode(b2["data"]["sfl.properties"]).decode() == properties_text(
        {"connection-user": "u", "connection-password": PASSWORD})
    assert (m3, t3, b3) == ("PATCH", "application/merge-patch+json", {"data": {"sfl.properties": None}})
    with pytest.raises(ExternalCatalogError) as e:
        CredentialSecret("s", "ns", api="http://127.0.0.1:9", token_file=str(tmp_path / "token")).write(
            "x", {"connection-password": PASSWORD})
    assert PASSWORD not in str(e.value)


def test_credential_files_are_written_whole(tmp_path):
    sink = CredentialFiles(str(tmp_path / "d"), "/trino/creds", 0o600)
    sink.write("a", {"connection-user": "u", "connection-password": "p"})
    assert sink.path("a") == "/trino/creds/a.properties"
    assert stat.S_IMODE(os.stat(tmp_path / "d" / "a.properties").st_mode) == 0o600
    assert [p.name for p in (tmp_path / "d").iterdir()] == ["a.properties"]       # no temporary file left
    sink.remove("a")
    sink.remove("a")                                                                # gone already: fine


# ---- Delta Sharing (recipient) ---------------------------------------------------------------------------------
def _resolves_to(monkeypatch, ip: str) -> None:
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                                                          (ip, port))])


def test_delta_sharing_profiles_and_types(monkeypatch):
    from datetime import datetime, timedelta, timezone

    _resolves_to(monkeypatch, "93.184.216.34")

    import pyarrow as pa

    from cairn.pipelines.saas import SaasError, delta_sharing_profile, delta_type

    good = {"shareCredentialsVersion": 1, "endpoint": "https://sharing.example.com/delta-sharing/",
            "bearerToken": "tok-" + "t" * 20,
            "expirationTime": (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    assert delta_sharing_profile({"profile": json.dumps(good)}) == ("https://sharing.example.com/delta-sharing",
                                                                    good["bearerToken"])
    bad = [({**good, "expirationTime": "2020-01-01T00:00:00Z"}, "expired"),
           ({**good, "shareCredentialsVersion": 2, "type": "oauth_client_credentials"}, "bearer-token profiles"),
           ({**good, "endpoint": "http://sharing.example.com/x"}, "https://")]
    for prof, why in bad:
        with pytest.raises(SaasError, match=why) as e:
            delta_sharing_profile({"profile": json.dumps(prof)})
        assert good["bearerToken"] not in str(e.value)
    with pytest.raises(SaasError, match="not a Delta Sharing profile") as e:
        delta_sharing_profile({"profile": "bearerToken=" + good["bearerToken"]})
    assert good["bearerToken"] not in str(e.value)
    assert delta_type("long") == pa.int64() and delta_type("decimal(12,2)") == pa.decimal128(12, 2)
    assert delta_type("timestamp") == pa.timestamp("us", tz="UTC") and delta_type("timestamp_ntz") == pa.timestamp("us")
    assert delta_type({"type": "array", "elementType": "string", "containsNull": True}) == pa.list_(pa.string())
    with pytest.raises(SaasError, match="unsupported type"):
        delta_type("variant")


def test_a_delta_sharing_sync_checks_its_object_and_columns(tmp_path):
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.pipelines.saas import SaasError, SaasSyncs

    engine = create_engine(f"sqlite:///{tmp_path}/s.db")
    init_db(engine)
    syncs = SaasSyncs(engine, {}, lambda *a: "", lambda owner, urn: True)
    base = {"name": "shared-orders", "kind": "delta_sharing", "object": "sales_share.sales.orders", "target": "shared.orders",
            "secret_scope": "databricks-share"}
    saved = syncs.save(ADMIN, base)
    assert saved["fields"] == [] and saved["kind"] == "delta_sharing"
    for body, why in [({**base, "name": "x1", "object": "orders"}, "<share>.<schema>.<table>"),
                      ({**base, "name": "x2", "fields": ["id; drop"]}, "columns to read")]:
        with pytest.raises(SaasError, match=why):
            syncs.save(ADMIN, body)
    assert syncs.shared_urn("Sales_Share.sales.orders") == \
        "urn:li:dataset:(urn:li:dataPlatform:databricks,sales_share.sales.orders,PROD)"


def test_a_catalogs_prefix_keeps_its_path_segments_and_unsafe_ones_are_refused():
    """Unity Catalog's Iceberg REST prefix is ``catalogs/<name>``: its slash must reach the path as a slash (an
    escaped one is a 404), as the Iceberg clients send it. Odd characters are escaped; a prefix that would leave
    ``/v1/`` or carry a query is refused."""
    from cairn.dataaccess.external_catalogs import ExternalCatalogError, rest_prefix

    assert rest_prefix("snow") == "snow"                                       # Polaris / Open Catalog
    assert rest_prefix("catalogs/lakehouse_uc") == "catalogs/lakehouse_uc"     # Unity Catalog
    assert rest_prefix("my cat/x") == "my%20cat/x"
    for bad in ("../admin", "a/../b", "a?b=1", "a#b", "a//b", "a\\b"):
        with pytest.raises(ExternalCatalogError):
            rest_prefix(bad)


def test_delta_sharing_fetches_only_public_addresses_and_counts_what_it_receives(monkeypatch):
    """A provider names its data files' URLs: one pointing at the cloud metadata service or an internal address is
    refused before it is fetched (else its answer would land in a table the owner reads), and a file larger than it
    said is cut off at the limit by what arrives, not by the size the provider gave."""
    import httpx

    from cairn.pipelines.saas import SaasError, _public, delta_sharing_read

    _resolves_to(monkeypatch, "169.254.169.254")
    monkeypatch.setenv("CAIRN_SAAS_ALLOW_PRIVATE_NETWORKS", "true")
    with pytest.raises(SaasError, match="link-local") as e:
        _public("https://metadata.example.com/latest/meta-data/", "a data file's URL")
    assert e.value.status == 403                                          # never, even with private networks allowed
    monkeypatch.delenv("CAIRN_SAAS_ALLOW_PRIVATE_NETWORKS")
    for ip in ("10.0.0.7", "127.0.0.1", "::ffff:192.168.1.2"):
        _resolves_to(monkeypatch, ip)
        with pytest.raises(SaasError, match="private address"):
            _public("https://files.example.com/x.parquet", "a data file's URL")
    with pytest.raises(SaasError, match="without credentials"):
        _public("https://u:p@files.example.com/x", "a data file's URL")
    _resolves_to(monkeypatch, "93.184.216.34")
    assert _public("https://files.example.com/x.parquet?sig=1", "u").endswith("sig=1")

    meta = {"metaData": {"id": "t", "schemaString": json.dumps({"fields": [{"name": "id", "type": "long"}]})}}

    def provider(urls_and_sizes):
        def handle(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/version"):
                return httpx.Response(200, headers={"delta-table-version": "3"})
            if req.url.path.endswith("/query"):
                lines = [{"protocol": {"minReaderVersion": 1}}, meta] + [
                    {"file": {"url": u, "size": n, "partitionValues": {}}} for u, n in urls_and_sizes]
                return httpx.Response(200, headers={"delta-table-version": "3"},
                                      text="\n".join(json.dumps(x) for x in lines))
            assert "authorization" not in req.headers                     # the token never goes to the storage
            return httpx.Response(200, content=b"x" * 5000)
        return httpx.Client(transport=httpx.MockTransport(handle))

    prof = {"profile": json.dumps({"shareCredentialsVersion": 1, "endpoint": "https://sharing.example.com/ds",
                                   "bearerToken": "t" * 20})}
    with pytest.raises(SaasError, match="larger than") as e:               # said 10 bytes, sent 5000
        delta_sharing_read(provider([("https://files.example.com/a.parquet", 10)]), prof, "s.sc.t", [], None, 1000)
    assert e.value.status == 413
    _resolves_to(monkeypatch, "10.1.2.3")
    with pytest.raises(SaasError, match="private address"):
        delta_sharing_read(provider([("https://files.example.com/a.parquet", 10)]), prof, "s.sc.t", [], None, 10**6)
