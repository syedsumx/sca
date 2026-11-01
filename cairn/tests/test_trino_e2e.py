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

import pytest

from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target

TRINO = "trinodb/trino:476"
KEY = "trino-e2e-" + "k" * 24
OAUTH_SECRET = "trino-oauth-" + "o" * 24
STORE_KEY = ("cairn-catalog", "catalog-store-secret")
CATALOG_TOKEN = "admin-" + "x" * 24
EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
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
    os.environ["CAIRN_TRINO_CATALOGS"] = json.dumps({"lake": {"platform": "iceberg", "database": ""}})
    os.environ["CAIRN_TRINO_OAUTH_SECRET"] = OAUTH_SECRET
    os.environ["CAIRN_TRINO_OAUTH_REDIRECT_URIS"] = "https://127.0.0.1/oauth2/callback"  # the real port is set below
    try:
        graph = InMemoryGraph()
        graph.aspects[(ORDERS, "schemaMetadata")] = {"fields": [
            {"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}}, {"fieldPath": "region"}]}
        graph.aspects[(SALARIES, "schemaMetadata")] = {"fields": [{"fieldPath": "name"}, {"fieldPath": "salary"}]}
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
        f"s3.aws-access-key={STORE_KEY[0]}", f"s3.aws-secret-key={STORE_KEY[1]}", ""]))
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
    os.chmod(d, 0o755)
    name = f"cairn-trino-acl-{os.getpid()}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "host",
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
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
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
    up = "urn:li:dataset:(urn:li:dataPlatform:trino,lake.sales.orders,PROD)"
    down = "urn:li:dataset:(urn:li:dataPlatform:trino,lake.sales.by_region,PROD)"
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
