"""A real Trino asks a real Cairn API before every action (Trino's OPA access-control plugin), on tables in
Cairn Catalog and Cairn Store: tables a user may not see are hidden and refused, labelled columns come back
masked, row filters apply, writes need WRITE_DATASET, and an admin sees everything as it is.

Needs docker and the store and catalog binaries (CAIRN_STORE_BIN, CAIRN_CATALOG_BIN)."""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time

import httpx
import pytest

from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target

TRINO = "trinodb/trino:476"
KEY = "trino-e2e-" + "k" * 24
OAUTH_SECRET = "trino-oauth-" + "o" * 24
STORE_KEY = ("cairn-catalog", "catalog-store-secret")
CATALOG_TOKEN = "admin-" + "x" * 24
EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,crm.sales.customers,PROD)"
SALARIES = "urn:li:dataset:(urn:li:dataPlatform:iceberg,hr.salaries,PROD)"


def _ready():
    if not all(os.environ.get(v) for v in ("CAIRN_STORE_BIN", "CAIRN_CATALOG_BIN")) or not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, CAIRN_STORE_BIN and CAIRN_CATALOG_BIN")


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(p, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", p), 0.3).close()
            return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"nothing on {p}")


@pytest.fixture(scope="module")
def lake():
    d = tempfile.mkdtemp()
    sp, cp = port(), port()
    procs = [subprocess.Popen([os.environ["CAIRN_STORE_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **os.environ, "CAIRN_STORE_ACCESS_KEY": STORE_KEY[0], "CAIRN_STORE_SECRET_KEY": STORE_KEY[1], "CAIRN_STORE_DATA": f"{d}/s",
        "CAIRN_STORE_ADDR": f"0.0.0.0:{sp}", "CAIRN_STORE_BUCKETS": "lakehouse,trino-exchange", "CAIRN_STORE_FSYNC": "false",
        "CAIRN_STORE_META": ""})]
    wait(sp)
    procs.append(subprocess.Popen([os.environ["CAIRN_CATALOG_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **os.environ, "CAIRN_CATALOG_ADDR": f"0.0.0.0:{cp}", "CAIRN_CATALOG_DATA": f"{d}/c", "CAIRN_CATALOG_TOKEN": CATALOG_TOKEN,
        "CAIRN_CATALOG_S3_ENDPOINT": f"http://127.0.0.1:{sp}", "CAIRN_CATALOG_S3_ACCESS_KEY": STORE_KEY[0],
        "CAIRN_CATALOG_S3_SECRET_KEY": STORE_KEY[1], "CAIRN_CATALOG_WAREHOUSE": "s3://lakehouse/warehouse",
        "CAIRN_CATALOG_FSYNC": "false"}))
    wait(cp)
    # On every interface: the Helm test's containers reach them through the Docker host.
    yield {"store": f"http://127.0.0.1:{sp}", "catalog": f"http://127.0.0.1:{cp}", "store_port": sp, "catalog_port": cp}
    for p in reversed(procs):
        p.terminate()
        p.wait(15)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module")
def cairn(tmp_path_factory):
    """The Cairn API, with Trino access on, in a thread."""
    import uvicorn

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.services import build_services

    tmp = tmp_path_factory.mktemp("cairn")
    os.environ["CAIRN_TRINO_ACCESS_KEY"] = KEY
    os.environ["CAIRN_TRINO_CATALOGS"] = json.dumps({"lake": {"platform": "iceberg", "database": ""},
                                                     "crm": {"platform": "postgres", "database": "crm"}})
    os.environ["CAIRN_TRINO_OAUTH_SECRET"] = OAUTH_SECRET
    os.environ["CAIRN_TRINO_OAUTH_REDIRECT_URIS"] = "https://127.0.0.1/oauth2/callback"  # the real port is set below
    try:
        graph = InMemoryGraph()
        graph.aspects[(ORDERS, "schemaMetadata")] = {"fields": [
            {"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}}, {"fieldPath": "region"}]}
        graph.aspects[(SALARIES, "schemaMetadata")] = {"fields": [{"fieldPath": "name"}, {"fieldPath": "salary"}]}
        graph.aspects[(CUSTOMERS, "schemaMetadata")] = {"fields": [
            {"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}}, {"fieldPath": "region"}]}
        settings = Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-secret", dev_trust_actor_header=True,
                            gateway={"key_dir": str(tmp / "keys")})
        s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    finally:
        os.environ.pop("CAIRN_TRINO_ACCESS_KEY")
        os.environ.pop("CAIRN_TRINO_CATALOGS")
        os.environ.pop("CAIRN_TRINO_OAUTH_SECRET")
        os.environ.pop("CAIRN_TRINO_OAUTH_REDIRECT_URIS")
    sub = lambda u: {"attr": "subject.urn", "op": "eq", "value": f"urn:li:corpuser:{u}"}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition=sub("admin")),
        Policy(id="sales", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
               target=Target(resource_urns=["*sales.*"])),
        Policy(id="mask-pii", effect="obligation", actions=["VIEW_DATASET_PROFILE"],
               obligations=[Obligation(type="mask_columns", params={"labels": ["PII"], "strategy": "partial"})],
               condition={"attr": "subject.urn", "op": "neq", "value": "urn:li:corpuser:admin"}),
        Policy(id="bob-eu", effect="obligation", actions=["VIEW_DATASET_PROFILE"], target=Target(resource_urns=["*sales.*"]),
               obligations=[Obligation(type="row_filter", params={"expression": "region = 'EU'"})], condition=sub("bob")),
    ]))
    p = port()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="0.0.0.0", port=p, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    wait(p)
    yield f"http://127.0.0.1:{p}", s
    server.should_exit = True
    t.join(10)


def write_tls(d):
    """A self-signed certificate for 127.0.0.1: tls.pem (key and certificate, for Trino), ca.pem (for clients)."""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cairn-trino")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                                                        x509.DNSName("localhost")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    open(f"{d}/ca.pem", "wb").write(pem)
    open(f"{d}/tls.pem", "wb").write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()) + pem)
    os.chmod(f"{d}/tls.pem", 0o644)


@pytest.fixture(scope="module")
def trino(lake, cairn):
    cairn_url, services = cairn
    d = tempfile.mkdtemp(prefix="trino-")
    os.makedirs(f"{d}/catalog")
    tp = port()
    open(f"{d}/catalog/lake.properties", "w").write("\n".join([
        "connector.name=iceberg", "iceberg.catalog.type=rest", f"iceberg.rest-catalog.uri={lake['catalog']}",
        "iceberg.rest-catalog.security=OAUTH2", f"iceberg.rest-catalog.oauth2.token={CATALOG_TOKEN}",
        "fs.native-s3.enabled=true", f"s3.endpoint={lake['store']}", "s3.region=us-east-1", "s3.path-style-access=true",
        f"s3.aws-access-key={STORE_KEY[0]}", f"s3.aws-secret-key={STORE_KEY[1]}",
        # Table maintenance is tested with no retention floor (production keeps Trino's 7 days).
        "iceberg.expire-snapshots.min-retention=0s", "iceberg.remove-orphan-files.min-retention=0s", ""]))
    base = f"{cairn_url}/v1/trino/{KEY}"
    open(f"{d}/access-control.properties", "w").write("\n".join([
        "access-control.name=opa", f"opa.policy.uri={base}/allow", f"opa.policy.batched-uri={base}/batch",
        f"opa.policy.row-filters-uri={base}/row-filters", f"opa.policy.column-masking-uri={base}/column-mask", ""]))
    open(f"{d}/event-listener.properties", "w").write("\n".join([
        "event-listener.name=openlineage", "openlineage-event-listener.transport.type=HTTP",
        f"openlineage-event-listener.transport.url={cairn_url}",
        f"openlineage-event-listener.transport.endpoint=/v1/trino/{KEY}/lineage",
        f"openlineage-event-listener.trino.uri=http://127.0.0.1:{tp}", ""]))
    open(f"{d}/usage-listener.properties", "w").write("\n".join([
        "event-listener.name=http", "http-event-listener.log-completed=true",
        f"http-event-listener.connect-ingest-uri={cairn_url}/v1/trino/{KEY}/queries", ""]))
    # Clients sign in over TLS with Cairn's tokens; plain HTTP is only for Trino's own nodes.
    tls = port()
    services.trino_oauth.prefixes = [f"https://127.0.0.1:{tls}/oauth2/callback"]
    write_tls(d)
    open(f"{d}/config.properties", "w").write("\n".join([
        "coordinator=true", "node-scheduler.include-coordinator=true", f"http-server.http.port={tp}",
        f"discovery.uri=http://127.0.0.1:{tp}", "http-server.https.enabled=true", f"http-server.https.port={tls}",
        "http-server.https.keystore.path=/etc/trino/tls.pem",
        f"http-server.authentication.jwt.key-file={cairn_url}/v1/trino/jwks.json",
        "http-server.authentication.jwt.required-issuer=cairn", "http-server.authentication.jwt.required-audience=trino",
        "internal-communication.shared-secret=" + "s" * 32,
        # Sign-in through Cairn (OAuth2) for the web UI and clients' external authentication.
        "http-server.authentication.type=OAUTH2,JWT", "web-ui.authentication.type=OAUTH2",
        "http-server.authentication.oauth2.issuer=cairn", "http-server.authentication.oauth2.oidc.discovery=false",
        f"http-server.authentication.oauth2.auth-url={cairn_url}/v1/trino/oauth/authorize",
        f"http-server.authentication.oauth2.token-url={cairn_url}/v1/trino/oauth/token",
        f"http-server.authentication.oauth2.jwks-url={cairn_url}/v1/trino/jwks.json",
        "http-server.authentication.oauth2.client-id=trino", f"http-server.authentication.oauth2.client-secret={OAUTH_SECRET}",
        "http-server.authentication.oauth2.scopes=trino",
        "event-listener.config-files=/etc/trino/event-listener.properties,/etc/trino/usage-listener.properties", ""]))
    # Another database through Trino (federation): PostgreSQL, its password from the environment.
    pg, pgp = f"cairn-trino-pg-{os.getpid()}", port()
    subprocess.run(["docker", "run", "-d", "--rm", "--name", pg, "-e", "POSTGRES_PASSWORD=crm-secret", "-e", "POSTGRES_DB=crm",
                    "-p", f"127.0.0.1:{pgp}:5432", "postgres:16-alpine"], check=True, capture_output=True, timeout=300)
    end = time.time() + 90
    while subprocess.run(["docker", "exec", pg, "pg_isready", "-U", "postgres", "-d", "crm"], capture_output=True).returncode:
        assert time.time() < end, "PostgreSQL did not start"
        time.sleep(1)
    time.sleep(2)
    subprocess.run(["docker", "exec", pg, "psql", "-U", "postgres", "-d", "crm", "-v", "ON_ERROR_STOP=1", "-c",
                    "CREATE SCHEMA sales; CREATE SCHEMA hr; "
                    "CREATE TABLE sales.customers (id int, email text, region text); "
                    "INSERT INTO sales.customers VALUES (1, 'ann@acme.com', 'EU'), (2, 'raj@example.org', 'US'); "
                    "CREATE TABLE hr.pay (name text, salary int); INSERT INTO hr.pay VALUES ('ann', 100);"],
                   check=True, capture_output=True, timeout=60)
    open(f"{d}/catalog/crm.properties", "w").write("\n".join([
        "connector.name=postgresql", f"connection-url=jdbc:postgresql://127.0.0.1:{pgp}/crm",
        "connection-user=postgres", "connection-password=${ENV:CAIRN_FED_CRM_PASSWORD}", ""]))
    os.chmod(d, 0o755)
    name = f"cairn-trino-acl-{os.getpid()}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "host", "-e", "CAIRN_FED_CRM_PASSWORD=crm-secret",
                    "-v", f"{d}/catalog:/etc/trino/catalog:ro", "-v", f"{d}/config.properties:/etc/trino/config.properties:ro",
                    "-v", f"{d}/access-control.properties:/etc/trino/access-control.properties:ro",
                    "-v", f"{d}/event-listener.properties:/etc/trino/event-listener.properties:ro",
                    "-v", f"{d}/usage-listener.properties:/etc/trino/usage-listener.properties:ro",
                    "-v", f"{d}/tls.pem:/etc/trino/tls.pem:ro", TRINO],
                   check=True, capture_output=True, timeout=600)
    end = time.time() + 240
    while "SERVER STARTED" not in subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr:
        assert time.time() < end, "Trino did not start"
        time.sleep(1)

    def sql(statement, user="admin", token=None, as_user=None):
        token = token if token is not None else services.trino_tokens.mint(f"urn:li:corpuser:{user}")["token"]
        out = subprocess.run(["docker", "exec", "-e", "LANG=C.UTF-8", name, "trino", "--server", f"https://127.0.0.1:{tls}", "--insecure",
                              *(["--access-token", token] if token else []), "--user", as_user or user,
                              "--catalog", "lake", "--output-format", "JSON", "--execute", statement],
                             capture_output=True, text=True, timeout=300)
        if out.returncode != 0:
            why = [ln for ln in out.stderr.splitlines() if "failed:" in ln or "Error" in ln]
            raise RuntimeError(why[0] if why else out.stderr.strip()[-500:] or out.stdout)
        return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]

    sql("CREATE SCHEMA lake.sales")
    sql("CREATE SCHEMA lake.hr")
    sql("CREATE TABLE lake.sales.orders AS SELECT * FROM (VALUES (1, 'jane@acme.com', 'EU'), (2, 'bo@example.org', 'US'), "
        "(3, 'li@acme.com', 'EU')) AS t(id, email, region)")
    sql("CREATE TABLE lake.hr.salaries AS SELECT 'jane' AS name, 100 AS salary")
    sql.port, sql.tls, sql.dir = tp, tls, d
    yield sql
    subprocess.run(["docker", "rm", "-f", name, pg], capture_output=True, timeout=60)
    shutil.rmtree(d, ignore_errors=True)


def test_admin_sees_everything_as_it_is(trino):
    rows = trino("SELECT id, email FROM sales.orders ORDER BY id")
    assert [r["email"] for r in rows] == ["jane@acme.com", "bo@example.org", "li@acme.com"]
    assert trino("SELECT salary FROM hr.salaries") == [{"salary": 100}]


def test_labelled_columns_are_masked_and_hidden_tables_refused(trino):
    rows = trino("SELECT id, email, region FROM sales.orders ORDER BY id", user="ana")
    assert [r["email"] for r in rows] == ["j•••@•••.com", "b•••@•••.org", "l•••@•••.com"]
    assert [r["region"] for r in rows] == ["EU", "US", "EU"]  # unlabelled: as it is
    # A filter on the masked column sees the masked values, so nothing leaks through WHERE either.
    assert trino("SELECT count(*) AS n FROM sales.orders WHERE email = 'jane@acme.com'", user="ana") == [{"n": 0}]
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino("SELECT * FROM hr.salaries", user="ana")
    assert trino("SHOW TABLES FROM hr", user="ana") == []
    assert {r["Table"] for r in trino("SHOW TABLES FROM sales", user="ana")} == {"orders"}


def test_row_filters_and_writes(trino):
    assert [r["id"] for r in trino("SELECT id FROM sales.orders ORDER BY id", user="bob")] == [1, 3]
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino("INSERT INTO sales.orders VALUES (4, 'x@y.z', 'EU')", user="ana")  # no WRITE_DATASET
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino("DROP TABLE sales.orders", user="ana")
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino("CREATE SCHEMA lake.mine", user="ana")
    trino("INSERT INTO sales.orders VALUES (4, 'x@y.z', 'EU')")  # the admin may
    assert trino("SELECT count(*) AS n FROM sales.orders", user="ana") == [{"n": 4}]


def test_queries_that_write_become_lineage(trino, cairn):
    _, s = cairn
    trino("CREATE TABLE lake.sales.by_region AS SELECT region, count(*) AS orders FROM lake.sales.orders GROUP BY region")
    trino("INSERT INTO lake.sales.by_region SELECT region, count(*) FROM lake.sales.orders WHERE id > 100 GROUP BY region")
    # Datasets as the catalog names them (this test maps the catalog "lake" to the iceberg platform).
    up = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
    down = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.by_region,PROD)"
    job = f"urn:li:dataJob:(urn:li:dataFlow:(trino,127.0.0.1:{trino.port},PROD),trino:lake.sales.by_region)"
    end = time.time() + 30
    while time.time() < end and not s.graph.get_aspect(job, "dataJobInputOutput"):
        time.sleep(0.5)
    io = s.graph.get_aspect(job, "dataJobInputOutput")
    assert io and io["inputDatasets"] == [up] and io["outputDatasets"] == [down], io
    # Column lineage: by_region.region comes from orders.region.
    pairs = {(tuple(f["upstreams"]), tuple(f["downstreams"])) for f in io["fineGrainedLineages"]}
    assert ((f"urn:li:schemaField:({up},region)",), (f"urn:li:schemaField:({down},region)",)) in pairs, pairs
    info = s.graph.get_aspect(job, "dataJobInfo")
    assert "lake.sales.orders" in info["customProperties"]["sql"]
    # Both queries are runs of the one job (named by what they write, not by query id).
    runs = [u for (u, a), v in s.graph.aspects.items() if a == "dataProcessInstanceRelationships" and v["parentTemplate"] == job]
    end = time.time() + 30
    while len(runs) < 2 and time.time() < end:
        time.sleep(0.5)
        runs = [u for (u, a), v in s.graph.aspects.items() if a == "dataProcessInstanceRelationships" and v["parentTemplate"] == job]
    assert len(runs) == 2


def test_the_sql_editor_runs_on_trino_as_the_signed_in_user(trino, cairn):
    """Cairn's SQL editor and Ask-your-data on a Trino connection with run_as_user: Cairn's checks first,
    then Trino's, as that user, so Trino's row filters apply to Cairn's queries too."""
    from cairn.dataaccess.service import DataAccessError
    from cairn.quality.collectors import ConnectionRegistry

    _, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    q = "SELECT id, email, region FROM sales.orders ORDER BY id"
    ana = s.data_access.query("urn:li:corpuser:ana", q, connection="lakehouse")
    assert [r[1] for r in ana["rows"]][:3] == ["j•••@•••.com", "b•••@•••.org", "l•••@•••.com"] and "email" in ana["masked_columns"]
    # Bob's row filter lives in Trino: Cairn's own SQL editor never applied row filters; now it does.
    bob = s.data_access.query("urn:li:corpuser:bob", q, connection="lakehouse")
    assert {r[2] for r in bob["rows"]} == {"EU"}
    # Cairn refuses first what Cairn knows the caller may not read.
    with pytest.raises(DataAccessError, match="may not query hr.salaries"):
        s.data_access.query("urn:li:corpuser:ana", "SELECT * FROM hr.salaries", connection="lakehouse")
    # The admin sees the values as they are.
    adm = s.data_access.query("urn:li:corpuser:admin", q, connection="lakehouse")
    assert adm["rows"][0][1] == "jane@acme.com" and adm["masked_columns"] == []

    # The SQL workspace runs the same way, over HTTP: masked for ana, filtered by Trino for bob, refused
    # where Cairn knows better; each person's history, and a shared query each runs as themselves.
    url, _ = cairn
    ana_h, bob_h = {"X-Cairn-Actor": "urn:li:corpuser:ana"}, {"X-Cairn-Actor": "urn:li:corpuser:bob"}
    r = httpx.post(f"{url}/sql/run", json={"sql": q, "connection": "lakehouse"}, headers=ana_h, timeout=60)
    assert r.status_code == 200 and "email" in r.json()["masked_columns"], r.text
    assert httpx.post(f"{url}/sql/run", json={"sql": "SELECT * FROM hr.salaries", "connection": "lakehouse"},
                      headers=ana_h, timeout=60).status_code == 403
    assert [h["status"] for h in httpx.get(f"{url}/sql/history", headers=ana_h, timeout=10).json()][:2] == ["error", "ok"]
    saved = httpx.post(f"{url}/sql/saved", json={"name": "orders", "connection": "lakehouse", "sql": q, "shared": True},
                       headers=ana_h, timeout=10).json()
    bob_run = httpx.post(f"{url}/sql/saved/{saved['id']}/run", headers=bob_h, timeout=60).json()
    assert {r[2] for r in bob_run["rows"]} == {"EU"}            # bob's row filter, though ana saved it
    names = [t["name"] for t in httpx.get(f"{url}/sql/schema", params={"connection": "lakehouse"}, headers=ana_h,
                                          timeout=10).json()]
    assert "lake.hr.salaries" not in names, names


def test_trino_only_believes_cairns_tokens(trino, cairn):
    """Who the Trino user is comes from Cairn's signed token: no token, a token signed with another key, an
    expired one, or a token for one user claiming another are all refused."""
    import base64

    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.dataaccess.trino_auth import TrinoTokens

    with pytest.raises(RuntimeError, match="401|Unauthorized|Authentication"):
        trino("SELECT 1", user="admin", token="")
    other = create_engine("sqlite://")
    init_db(other)
    forged = TrinoTokens(other, KEY, "cairn").mint("urn:li:corpuser:admin")["token"]
    with pytest.raises(RuntimeError, match="401|Unauthorized|Authentication"):
        trino("SELECT 1", token=forged)
    # The right key's token, edited to say another user: the signature no longer matches.
    _, s = cairn
    good = s.trino_tokens.mint("urn:li:corpuser:ana")["token"]
    head, body, sig = good.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=="))
    claims["sub"] = "admin"
    edited = ".".join([head, base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode(), sig])
    with pytest.raises(RuntimeError, match="401|Unauthorized|Authentication"):
        trino("SELECT 1", token=edited)
    # Ana's token, claiming to be the admin: Trino asks Cairn (ImpersonateUser) and Cairn says no.
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino("SELECT salary FROM hr.salaries", user="ana", as_user="admin")
    assert trino("SELECT count(*) AS n FROM sales.orders", user="ana")[0]["n"] >= 3
    # Plain HTTP refuses sign-in altogether (tokens only travel over TLS).
    out = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-H", f"Authorization: Bearer {good}",
                          "-H", "X-Trino-User: ana", "-X", "POST", "-d", "SELECT 1", f"http://127.0.0.1:{trino.port}/v1/statement"],
                         capture_output=True, text=True, timeout=30)
    assert out.stdout in ("401", "403"), out.stdout


def test_reads_become_usage(trino, cairn):
    """Who read what, from Trino's HTTP event listener: each dataset's usage statistics count the queries,
    the users and the columns read."""
    _, s = cairn
    trino("SELECT id, region FROM sales.orders", user="ana")
    trino("SELECT region FROM sales.orders WHERE region = 'EU'", user="bob")
    urn = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
    end = time.time() + 30
    usage = {}
    while time.time() < end:
        rows = s.graph.get_timeseries(urn, "datasetUsageStatistics", 1)
        usage = rows[0] if rows else {}
        users = {u["user"] for u in usage.get("userCounts") or []}
        if {"urn:li:corpuser:ana", "urn:li:corpuser:bob"} <= users:
            break
        time.sleep(0.5)
    users = {u["user"]: u["count"] for u in usage.get("userCounts") or []}
    assert users.get("urn:li:corpuser:ana", 0) >= 1 and users.get("urn:li:corpuser:bob", 0) >= 1, usage
    fields = {f["fieldPath"]: f["count"] for f in usage["fieldCounts"]}
    assert fields["region"] >= 2 and "id" in fields
    # A refused read is not usage.
    before = usage["totalSqlQueries"]
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino("SELECT * FROM hr.salaries", user="ana")
    time.sleep(3)
    hr = "urn:li:dataset:(urn:li:dataPlatform:iceberg,hr.salaries,PROD)"
    assert all(u["user"] != "urn:li:corpuser:ana" for r in s.graph.get_timeseries(hr, "datasetUsageStatistics", 5)
               for u in r.get("userCounts") or [])
    assert s.graph.get_timeseries(urn, "datasetUsageStatistics", 1)[0]["totalSqlQueries"] >= before


MASK_CASES = [("varchar", "'jane@acme.com'"), ("varchar(20)", "'Johnathan'"), ("char(3)", "'abc'"), ("tinyint", "TINYINT '37'"),
              ("smallint", "SMALLINT '-1234'"), ("integer", "37"), ("bigint", "BIGINT '123456789'"),
              ("decimal(10,2)", "DECIMAL '98765.43'"), ("decimal(38,0)", "DECIMAL '12345678901234567890'"),
              ("real", "REAL '0.0345'"), ("double", "DOUBLE '-4567.8'"), ("date", "DATE '1985-07-23'"),
              ("timestamp(3)", "TIMESTAMP '2026-03-14 15:09:26.535'"),
              ("timestamp(6) with time zone", "TIMESTAMP '2026-03-14 15:09:26.535897 Europe/Paris'"),
              ("time(3)", "TIME '15:09:26.535'"), ("boolean", "true"), ("varbinary", "X'0102'"), ("array(integer)", "ARRAY[1, 2]")]


def test_masks_keep_every_type(trino):
    """Every strategy on every column type runs on Trino, keeps the column's exact type (Trino refuses a
    mask that does not) and keeps NULL as NULL; numbers keep one significant figure, dates the month."""
    from cairn.dataaccess.trino import mask_expression

    strategies = ("partial", "hash", "redact", "null")
    got = {}
    for typ, lit in MASK_CASES:
        cols = ", ".join(f"typeof({mask_expression('c', typ, st)}) = typeof(c) AS t_{st}, "
                         f"({mask_expression('c', typ, st)}) IS NULL AS n_{st}" for st in strategies)
        if not typ.startswith(("array", "varbinary", "boolean")):
            cols += ", " + ", ".join(f"CAST({mask_expression('c', typ, st)} AS varchar) AS v_{st}" for st in strategies)
        rows = trino(f"SELECT c IS NULL AS is_null, {cols} FROM (VALUES CAST({lit} AS {typ}), CAST(NULL AS {typ})) AS x(c) "
                     "ORDER BY is_null")
        value, null = rows
        assert all(value[f"t_{st}"] and null[f"t_{st}"] for st in strategies), (typ, value)
        assert all(null[f"n_{st}"] for st in strategies), (typ, null)  # NULL stays NULL
        got[typ] = value
    assert got["integer"]["v_partial"] == "30" and got["bigint"]["v_partial"] == "100000000"
    assert got["decimal(10,2)"]["v_partial"] == "90000.00" and got["decimal(10,2)"]["v_redact"] == "0.00"
    assert got["date"]["v_partial"] == "1985-07-01" and got["date"]["v_redact"] == "1970-01-01"
    assert got["timestamp(3)"]["v_partial"].startswith("2026-03-01 00:00:00")
    assert got["varchar"]["v_partial"] == "j•••@•••.com"
    assert got["boolean"]["n_partial"] and got["array(integer)"]["n_hash"]  # no meaningful mask: NULL
    # A hash is stable (the same value masks the same, so joins still match) and hides the value.
    again = trino(f"SELECT CAST({mask_expression('c', 'integer', 'hash')} AS varchar) AS v FROM (VALUES 37) AS x(c)")
    assert again[0]["v"] == got["integer"]["v_hash"] != "37"


def test_numbers_and_dates_are_masked_for_the_caller(trino, cairn):
    """A labelled salary and birth date come back masked, not NULL, and keep their types."""
    _, s = cairn
    trino("CREATE TABLE lake.sales.people AS SELECT * FROM (VALUES (1, DECIMAL '98765.43', DATE '1985-07-23'), "
          "(2, DECIMAL '51234.00', DATE '1990-02-11')) AS t(id, salary, born)")
    s.graph.aspects[("urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.people,PROD)", "schemaMetadata")] = {"fields": [
        {"fieldPath": "id"}, {"fieldPath": "salary", "glossaryTerms": {"terms": [{"urn": EMAIL}]}},
        {"fieldPath": "born", "glossaryTerms": {"terms": [{"urn": EMAIL}]}}]}
    rows = trino("SELECT id, CAST(salary AS varchar) AS salary, CAST(born AS varchar) AS born, typeof(salary) AS t "
                 "FROM sales.people ORDER BY id", user="ana")
    assert [(r["salary"], r["born"]) for r in rows] == [("90000.00", "1985-07-01"), ("50000.00", "1990-02-01")]
    assert rows[0]["t"] == "decimal(7,2)"
    admin = trino("SELECT CAST(salary AS varchar) AS salary FROM sales.people ORDER BY id")
    assert admin[0]["salary"] == "98765.43"


def test_trino_tables_are_synced_into_the_catalog(trino, cairn):
    """Cairn reads the connection's tables as its crawler (structure only) and writes them to the catalog
    under the URNs access control uses; dropped tables are marked removed; the SQL editor can then query
    a table nobody catalogued by hand."""
    from cairn.dataaccess.service import DataAccessError
    from cairn.quality.collectors import ConnectionRegistry

    _, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    trino("CREATE TABLE lake.sales.returns (id BIGINT, reason VARCHAR, amount DECIMAL(10,2), day DATE) "
          "COMMENT 'Customer returns'")
    trino("INSERT INTO lake.sales.returns VALUES (1, 'damaged', 9.99, DATE '2026-10-01')")
    trino("CREATE TABLE lake.sales.scratch AS SELECT 1 AS x")
    out = s.trino_catalog.sync("lakehouse", "urn:li:corpuser:admin")
    assert out["catalog"] == "lake" and out["tables"] >= 4 and out["removed"] == 0, out
    urn = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.returns,PROD)"
    schema = s.graph.get_aspect(urn, "schemaMetadata")
    assert [(f["fieldPath"], f["nativeDataType"]) for f in schema["fields"]] == [
        ("id", "bigint"), ("reason", "varchar"), ("amount", "decimal(10,2)"), ("day", "date")]
    assert schema["fields"][2]["type"] == {"type": {"com.linkedin.schema.NumberType": {}}}
    props = s.graph.get_aspect(urn, "datasetProperties")
    assert props["description"] == "Customer returns" and props["customProperties"]["trino_table"] == "returns"
    assert s.graph.get_aspect(urn, "subTypes") == {"typeNames": ["Table"]}
    # Hidden from ana, but the crawler saw it: structure is catalogued, its rows were never read.
    assert s.graph.get_aspect("urn:li:dataset:(urn:li:dataPlatform:iceberg,hr.salaries,PROD)", "schemaMetadata")
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino("SELECT * FROM hr.salaries", user="cairn-catalog", token=s.trino_tokens.mint("urn:li:corpuser:cairn-catalog")["token"])
    # The SQL editor queries the synced table.
    rows = s.data_access.query("urn:li:corpuser:ana", "SELECT id, reason FROM sales.returns", connection="lakehouse")["rows"]
    assert rows == [[1, "damaged"]]
    # A dropped table is marked removed on the next sync.
    trino("DROP TABLE lake.sales.scratch")
    again = s.trino_catalog.sync("lakehouse", "urn:li:corpuser:admin")
    assert again["removed"] == 1
    assert s.graph.get_aspect("urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.scratch,PROD)", "status") == {"removed": True}
    with pytest.raises(DataAccessError, match="not in the catalog"):  # Cairn refuses before Trino is asked
        s.data_access.query("urn:li:corpuser:ana", "SELECT * FROM sales.scratch", connection="lakehouse")


def test_trino_web_ui_and_clients_sign_in_through_cairn(trino, cairn):
    """Single sign-on: Trino's web UI sends a person to Cairn and back (OAuth2); a client without a token is
    told where to sign in (external authentication) and then gets a Cairn token from Trino."""
    import re

    import requests
    import urllib3

    urllib3.disable_warnings()
    base = f"https://127.0.0.1:{trino.tls}"
    browser = requests.Session()
    browser.verify, browser.trust_env = False, False
    browser.headers["X-Cairn-Actor"] = "urn:li:corpuser:ana"   # signed in to Cairn (dev header stands in for the session)
    r = browser.get(f"{base}/ui/", timeout=60)
    assert r.status_code == 200 and "trino" in r.text.lower(), (r.status_code, r.url)
    hops = [h.headers.get("location", "") for h in r.history]
    assert any("/v1/trino/oauth/authorize" in h for h in hops) and any("/oauth2/callback" in h for h in hops), hops
    info = browser.get(f"{base}/ui/api/cluster", timeout=30)
    assert info.status_code == 200 and info.json()["nodeVersion"]["version"] == "476"
    # Without a Cairn session the UI sends the browser to Cairn's sign-in.
    stranger = requests.Session()
    stranger.verify, stranger.trust_env = False, False
    r = stranger.get(f"{base}/ui/", timeout=60, allow_redirects=True)
    assert "/login?redirect_uri=" in (r.url + "".join(h.headers.get("location", "") for h in r.history))
    # A client (CLI, JDBC, Python) with external authentication: 401 names where to sign in and where to wait.
    r = requests.post(f"{base}/v1/statement", data="SELECT 1", headers={"X-Trino-User": "ana"}, verify=False, timeout=30)
    assert r.status_code == 401
    challenge = r.headers["WWW-Authenticate"]
    redirect = re.search(r'x_redirect_server="([^"]+)"', challenge).group(1)
    token_server = re.search(r'x_token_server="([^"]+)"', challenge).group(1)
    assert browser.get(redirect, timeout=60).status_code == 200   # the person signs in in their browser
    token = requests.get(token_server, verify=False, timeout=30).json()["token"]
    r = requests.post(f"{base}/v1/statement", data="SELECT current_user AS u",
                      headers={"Authorization": f"Bearer {token}", "X-Trino-User": "ana"}, verify=False, timeout=30)
    assert r.status_code == 200, r.text
    # The token is Cairn's, for ana: she is ana to Trino, and Cairn's policies apply to her.
    assert trino("SELECT current_user AS u", user="ana", token=token) == [{"u": "ana"}]
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino("SELECT * FROM hr.salaries", user="ana", token=token)


def test_table_maintenance_compacts_expires_and_removes_orphans(trino, cairn, lake):
    """Cairn's table maintenance on real Trino and Cairn Catalog: five small inserts become one file, old
    snapshots expire, a stray file in the table's location is deleted, and each step is recorded."""
    import boto3
    from cairn.pipelines.maintenance import TableMaintenance
    from cairn.quality.collectors import ConnectionRegistry

    _, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    definition = s.data_access.registry.definitions()["lakehouse"]

    def execute(sql):  # as the maintenance user (here the admin), the way Cairn runs it
        return s.data_access.run_on_trino(definition, "urn:li:corpuser:admin", sql, 100_000)[1]
    for stmt in ["CREATE SCHEMA IF NOT EXISTS lake.maint", "CREATE TABLE lake.maint.events (x integer)"] + \
            [f"INSERT INTO lake.maint.events VALUES ({i})" for i in range(5)]:
        execute(stmt)
    files = trino('SELECT file_path FROM lake.maint."events$files"')
    assert len(files) == 5 and len(trino('SELECT snapshot_id FROM lake.maint."events$snapshots"')) >= 5
    bucket, key = files[0]["file_path"].removeprefix("s3://").split("/", 1)
    stray = key.rsplit("/", 1)[0] + "/stray-0000.parquet"
    s3 = boto3.client("s3", endpoint_url=lake["store"], aws_access_key_id=STORE_KEY[0], aws_secret_access_key=STORE_KEY[1],
                      region_name="us-east-1")
    s3.put_object(Bucket=bucket, Key=stray, Body=b"not referenced by any snapshot")
    time.sleep(2)

    m = TableMaintenance(s.engine, execute, "lake", s.audit)
    m.set_policy("maint", {"optimize": True, "expire_days": 0, "orphan_days": 0, "every_hours": 24}, "urn:li:corpuser:admin")
    assert m.policy_for("maint", "events")["scope"] == "maint"
    done = m.maintain("maint", "events")
    assert [(r["operation"], r["status"]) for r in done] == [("optimize", "ok"), ("expire_snapshots", "ok"),
                                                               ("remove_orphan_files", "ok")], done
    assert len(trino('SELECT file_path FROM lake.maint."events$files"')) == 1
    assert len(trino('SELECT snapshot_id FROM lake.maint."events$snapshots"')) == 1
    assert sorted(r["x"] for r in trino("SELECT x FROM lake.maint.events")) == [0, 1, 2, 3, 4]
    left = [o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix=key.rsplit("/", 1)[0]).get("Contents", [])]
    assert stray not in left, left
    assert {r["operation"] for r in m.runs("maint.events")} == {"optimize", "expire_snapshots", "remove_orphan_files"}
    assert m.run_due() == 0            # just done: not due for 24 hours
    assert any(e["action"] == "maintenance.policy_set" for e in s.audit.entries("lakehouse", limit=20))


def test_other_databases_are_queried_through_trino_under_cairns_policies(trino):
    """Federation: a PostgreSQL catalog in Trino, named to Cairn as postgres datasets (CAIRN_TRINO_CATALOGS).
    Cairn decides its tables like the lakehouse's: masks, row filters and refusals apply in place."""
    assert [r["email"] for r in trino("SELECT email FROM crm.sales.customers ORDER BY id")] == ["ann@acme.com", "raj@example.org"]
    ana = trino("SELECT id, email, region FROM crm.sales.customers ORDER BY id", user="ana")
    assert [r["email"] for r in ana] == ["a•••@•••.com", "r•••@•••.org"], ana
    bob = trino("SELECT id, region FROM crm.sales.customers ORDER BY id", user="bob")
    assert [r["region"] for r in bob] == ["EU"]
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino("SELECT * FROM crm.hr.pay", user="ana")
    # It joins with the lakehouse in one query, both sides governed.
    j = trino("SELECT o.id, c.email FROM lake.sales.orders o JOIN crm.sales.customers c ON o.id = c.id ORDER BY o.id", user="ana")
    assert [r["email"] for r in j] == ["a•••@•••.com", "r•••@•••.org"]


def test_derived_tables_refresh_incrementally_and_in_order(trino, cairn):
    """Derived tables on real Trino and Cairn Catalog: append by watermark, a full rebuild chained on it,
    and a merge by key; refreshed when the tables they read change, each run as its owner."""
    from cairn.pipelines.changes import DatasetChanges
    from cairn.pipelines.derived import DerivedError, DerivedTables
    from cairn.quality.collectors import ConnectionRegistry

    _, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    definition = s.data_access.registry.definitions()["lakehouse"]

    def run(owner, sql):
        return s.data_access.run_on_trino(definition, owner, sql, 100_000)[1]
    admin = "urn:li:corpuser:admin"
    for stmt in ["CREATE SCHEMA IF NOT EXISTS lake.shop",
                 "CREATE TABLE lake.shop.orders (id integer, region varchar, amount double, seq integer)",
                 "INSERT INTO lake.shop.orders VALUES (1, 'EU', 10.0, 1), (2, 'US', 5.0, 2), (3, 'EU', -1.0, 3)"]:
        run(admin, stmt)
    changes = DatasetChanges(s.engine)
    dt = DerivedTables(s.engine, run, "lake", s.audit, changes)
    clean = dt.create({"name": "orders_clean", "target": "shop.orders_clean", "mode": "append", "watermark": "seq",
                       "on_change": True, "sql": "SELECT id, region, amount, seq FROM lake.shop.orders WHERE amount > 0"},
                      admin, admin)
    assert clean["upstreams"] == [dt.urn("shop", "orders")]
    by_region = dt.create({"name": "by_region", "target": "shop.by_region", "mode": "full", "on_change": True,
                           "sql": "SELECT region, sum(amount) AS total FROM shop.orders_clean GROUP BY region"}, admin, admin)
    latest = dt.create({"name": "latest", "target": "shop.latest", "mode": "merge", "key": ["id"], "watermark": "seq",
                        "every_minutes": 60, "sql": "SELECT id, region, amount, seq FROM lake.shop.orders"}, admin, admin)
    assert [dt.refresh(d["id"])["kind"] for d in (clean, by_region, latest)] == ["create", "create", "create"]
    assert sorted(r["id"] for r in trino("SELECT id FROM shop.orders_clean")) == [1, 2]
    assert {r["region"]: r["total"] for r in trino("SELECT * FROM shop.by_region")} == {"EU": 10.0, "US": 5.0}

    # New rows and an update upstream, written by someone who records the change (a pipe, a stream, dbt...).
    run(admin, "INSERT INTO lake.shop.orders VALUES (4, 'US', 7.0, 4), (5, 'EU', 1.0, 5)")
    run(admin, "UPDATE lake.shop.orders SET amount = 12.0, seq = 6 WHERE id = 1")
    changes.record([dt.urn("shop", "orders")], "test")
    n = dt.run_due()
    assert n >= 2                              # orders_clean (its upstream changed), then by_region (it did)
    assert dt.ordered()[0]["name"] == "latest" and [d["name"] for d in dt.ordered()].index("orders_clean") < \
        [d["name"] for d in dt.ordered()].index("by_region")
    assert sorted((r["id"], r["amount"]) for r in trino("SELECT id, amount FROM shop.orders_clean")) == \
        [(1, 10.0), (1, 12.0), (2, 5.0), (4, 7.0), (5, 1.0)]       # append: rows above the watermark, none twice
    assert {r["region"]: r["total"] for r in trino("SELECT * FROM shop.by_region")} == {"EU": 23.0, "US": 12.0}
    time.sleep(3)                              # Trino's own lineage events (changes too) arrive
    dt.run_due()
    assert dt.run_due() == 0                   # nothing changed since; latest is not due for an hour
    assert dt.refresh(latest["id"])["kind"] == "merge"
    assert sorted((r["id"], r["amount"]) for r in trino("SELECT id, amount FROM shop.latest")) == \
        [(1, 12.0), (2, 5.0), (3, -1.0), (4, 7.0), (5, 1.0)]       # merge: the update replaced, new rows inserted
    assert {r["trigger"] for r in dt.runs(by_region["id"])} == {"change", "manual"}
    assert dt.refresh(by_region["id"], full=True)["kind"] == "full"

    # A run is its owner's: ana may not read hr.salaries, so her derived table's run fails, recorded.
    ana = dt.create({"name": "pay", "target": "shop.pay", "every_minutes": 5, "sql": "SELECT * FROM lake.hr.salaries"},
                    "urn:li:corpuser:ana", "urn:li:corpuser:ana")
    r = dt.refresh(ana["id"])
    assert r["status"] == "error" and "Access Denied" in r["error"], r
    assert trino("SHOW TABLES FROM shop LIKE 'pay'") == []
    # Bad definitions are refused.
    for body, msg in [({"sql": "DROP TABLE lake.shop.orders"}, "one SELECT"),
                      ({"sql": "SELECT 1; SELECT 2"}, "one SELECT"),
                      ({"sql": "SELECT * FROM lake.shop.x2", "target": "shop.x2"}, "its own target"),
                      ({"mode": "append"}, "watermark"), ({"mode": "merge"}, "key columns"),
                      ({"every_minutes": None, "on_change": False}, "every_minutes, on_change"),
                      ({"watermark": "seq; drop", "mode": "append"}, "column names")]:
        with pytest.raises(DerivedError, match=msg):
            dt.create({"name": "x", "target": "shop.x", "sql": "SELECT 1 AS one", "every_minutes": 5, **body}, admin, admin)
    with pytest.raises(DerivedError, match="exists"):
        dt.create({"name": "other", "target": "shop.latest", "sql": "SELECT 1 AS one", "every_minutes": 5}, admin, admin)
