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
import uuid
import pytest

from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target

TRINO = os.environ.get("CAIRN_TRINO_IMAGE", "trinodb/trino:476")   # the upgrade matrix tries newer ones
_TV = int(TRINO.rsplit(":", 1)[-1]) if TRINO.rsplit(":", 1)[-1].isdigit() else 476
# Settings Trino renamed (by 484; the old names stop it starting): what this image calls them.
S3_ON = "fs.s3.enabled=true" if _TV >= 484 else "fs.native-s3.enabled=true"
DELTA_CONDITIONAL = "delta.s3.transaction-log-conditional-writes.enabled=true" if _TV >= 484 else "s3.exclusive-create=true"
KEY = "trino-e2e-" + "k" * 24
OAUTH_SECRET = "trino-oauth-" + "o" * 24
STORE_KEY = ("cairn-catalog", "catalog-store-secret")
CATALOG_TOKEN = "admin-" + "x" * 24
EMAIL = "urn:li:glossaryTerm:Classification.PII.Email"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
CUSTOMERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,crm.sales.customers,PROD)"
SALARIES = "urn:li:dataset:(urn:li:dataPlatform:iceberg,hr.salaries,PROD)"
DELTA_ORDERS = "urn:li:dataset:(urn:li:dataPlatform:delta-lake,dsales.orders,PROD)"


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
                                                     "delta": {"platform": "delta-lake", "database": ""},
                                                     "crm": {"platform": "postgres", "database": "crm"}})
    os.environ["CAIRN_TRINO_OAUTH_SECRET"] = OAUTH_SECRET
    os.environ["CAIRN_TRINO_OAUTH_REDIRECT_URIS"] = "https://127.0.0.1/oauth2/callback"  # the real port is set below
    # AI functions in SQL go through Cairn's gateway to the model (a recording stand-in here).
    model = ModelStub()
    os.environ.update({"CAIRN_AI_SQL_UPSTREAM": f"http://127.0.0.1:{model.port}", "CAIRN_AI_SQL_PROVIDER": "openai",
                       "CAIRN_AI_SQL_API_KEY": "provider-key-held-by-cairn", "CAIRN_AI_SQL_MODELS": "stub-model"})
    try:
        graph = InMemoryGraph()
        graph.aspects[(ORDERS, "schemaMetadata")] = {"fields": [
            {"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}}, {"fieldPath": "region"}]}
        graph.aspects[(DELTA_ORDERS, "schemaMetadata")] = {"fields": [
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
        for k in ("CAIRN_AI_SQL_UPSTREAM", "CAIRN_AI_SQL_PROVIDER", "CAIRN_AI_SQL_API_KEY", "CAIRN_AI_SQL_MODELS"):
            os.environ.pop(k)
    s.model_stub = model
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
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="0.0.0.0", port=p, timeout_keep_alive=75, log_level="warning"))
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


class ModelStub:
    """An OpenAI-compatible chat endpoint that records what it is sent (for Trino's AI functions)."""

    def __init__(self):
        import http.server

        stub = self
        self.prompts: list[str] = []
        self.bodies: list[dict] = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                text = " ".join(str(m.get("content")) for m in body.get("messages") or [])
                stub.prompts.append(text)
                stub.bodies.append({"body": body, "headers": dict(self.headers)})
                out = json.dumps({"id": "c1", "object": "chat.completion", "created": 0, "model": body.get("model", ""),
                                  "choices": [{"index": 0, "finish_reason": "stop",
                                               "message": {"role": "assistant", "content": "positive"}}],
                                  "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
                self.send_response(200 if self.path == "/v1/chat/completions" else 404)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass
        self.port = port()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture(scope="module")
def trino(lake, cairn):
    cairn_url, services = cairn
    d = tempfile.mkdtemp(prefix="trino-")
    os.makedirs(f"{d}/catalog")
    tp = port()
    # Delta Lake tables in Cairn Store, their metastore beside them (a file metastore in the store).
    open(f"{d}/catalog/delta.properties", "w").write("\n".join([
        "connector.name=delta_lake", "hive.metastore=file", "hive.metastore.catalog.dir=s3://lakehouse/delta",
        S3_ON, f"s3.endpoint={lake['store']}", "s3.region=us-east-1", "s3.path-style-access=true",
        f"s3.aws-access-key={STORE_KEY[0]}", f"s3.aws-secret-key={STORE_KEY[1]}",
        "delta.register-table-procedure.enabled=true", DELTA_CONDITIONAL, ""]))
    # Stored functions: the Hive connector keeps them, in a file metastore in the store.
    open(f"{d}/catalog/routines.properties", "w").write("\n".join([
        "connector.name=hive", "hive.metastore=file", "hive.metastore.catalog.dir=s3://lakehouse/routines",
        S3_ON, f"s3.endpoint={lake['store']}", "s3.region=us-east-1", "s3.path-style-access=true",
        f"s3.aws-access-key={STORE_KEY[0]}", f"s3.aws-secret-key={STORE_KEY[1]}", ""]))
    # AI functions (Trino's ai connector) against a recording stand-in for the model's API.
    model = services.model_stub
    open(f"{d}/catalog/llm.properties", "w").write("\n".join([
        "connector.name=ai", "ai.provider=openai", "ai.model=stub-model", f"ai.openai.endpoint={cairn_url}/v1/trino/{KEY}/ai",
        "ai.openai.api-key=not-the-provider-key", ""]))      # Trino never has the provider's key
    open(f"{d}/catalog/lake.properties", "w").write("\n".join([
        "connector.name=iceberg", "iceberg.catalog.type=rest", f"iceberg.rest-catalog.uri={lake['catalog']}",
        "iceberg.rest-catalog.security=OAUTH2", f"iceberg.rest-catalog.oauth2.token={CATALOG_TOKEN}",
        S3_ON, f"s3.endpoint={lake['store']}", "s3.region=us-east-1", "s3.path-style-access=true",
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
                    "-e", "CAIRN_AI_API_KEY=stub-key",
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
            msg = why[0] if why else out.stderr.strip()[-500:] or out.stdout
            if "OPA backend" in msg:                   # what Trino got back from Cairn, for the report
                logs = subprocess.run(["docker", "logs", "--tail", "400", name], capture_output=True, text=True, timeout=60)
                msg += "\n" + "\n".join(ln for ln in (logs.stdout + logs.stderr).splitlines()
                                         if "opa" in ln.lower() or "OpaHttpClient" in ln or "Caused by" in ln)[-4000:]
            raise RuntimeError(msg)
        return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]

    # IF NOT EXISTS: the Spark tests bring their own lake with these tables in it.
    sql("CREATE SCHEMA IF NOT EXISTS lake.sales")
    sql("CREATE SCHEMA IF NOT EXISTS lake.hr")
    sql("CREATE TABLE IF NOT EXISTS lake.sales.orders AS SELECT * FROM (VALUES (1, 'jane@acme.com', 'EU'), (2, 'bo@example.org', 'US'), "
        "(3, 'li@acme.com', 'EU')) AS t(id, email, region)")
    sql("CREATE TABLE IF NOT EXISTS lake.hr.salaries AS SELECT 'jane' AS name, 100 AS salary")
    sql.port, sql.tls, sql.dir, sql.model = tp, tls, d, model
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



def test_queries_have_a_history_with_profiles_and_hints(trino, cairn):
    """Each finished query is kept with its statistics: its author sees it (and only theirs), operators
    see all, and a profile carries tuning hints drawn from its statistics and SQL."""
    url, s = cairn
    tag = uuid.uuid4().hex[:8]
    trino(f"SELECT * FROM sales.orders o, sales.orders p WHERE o.region <> '{tag}'", user="ana")
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino(f"SELECT salary FROM hr.salaries WHERE name <> '{tag}'", user="ana")
    trino(f"SELECT count(*) AS n FROM sales.orders WHERE region <> '{tag}'", user="bob")

    def mine(user):
        return httpx.get(f"{url}/v1/queries", params={"text": tag}, headers={"X-Cairn-Actor": f"urn:li:corpuser:{user}"},
                         timeout=10).json()["queries"]
    end = time.time() + 30
    while time.time() < end and len(mine("admin")) < 3:
        time.sleep(0.5)
    ana, everyone = mine("ana"), mine("admin")
    assert {q["user"] for q in ana} == {"ana"} and len(ana) == 2                     # her own only
    assert {q["user"] for q in everyone} == {"ana", "bob"} and len(everyone) == 3    # operators: all
    ok = next(q for q in ana if q["state"] == "FINISHED")
    assert ok["wall_ms"] > 0 and ok["output_rows"] >= 1 and ok["tables"] == \
        ["urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"]
    failed = next(q for q in ana if q["state"] == "FAILED")
    assert failed["error_code"] == "PERMISSION_DENIED"
    prof = httpx.get(f"{url}/v1/queries/{ok['query_id']}", headers={"X-Cairn-Actor": "urn:li:corpuser:ana"}, timeout=10).json()
    assert {"cross_join"} <= {h["kind"] for h in prof["hints"]}, prof["hints"]
    fp = httpx.get(f"{url}/v1/queries/{failed['query_id']}", headers={"X-Cairn-Actor": "urn:li:corpuser:ana"}, timeout=10).json()
    assert [h["kind"] for h in fp["hints"]] == ["failed_access"]
    # bob may not see ana's query.
    assert httpx.get(f"{url}/v1/queries/{ok['query_id']}", headers={"X-Cairn-Actor": "urn:li:corpuser:bob"},
                     timeout=10).status_code == 404

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
    # A column's classification survives the sync (its mask is decided on it).
    email = next(f for f in s.graph.get_aspect(ORDERS, "schemaMetadata")["fields"] if f["fieldPath"] == "email")
    assert email["glossaryTerms"]["terms"] == [{"urn": EMAIL}], email
    assert trino("SELECT email FROM sales.orders WHERE id = 1", user="ana") == [{"email": "j•••@•••.com"}]
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
    assert info.status_code == 200 and info.json()["nodeVersion"]["version"] == str(_TV)
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


def test_tables_are_clustered_by_what_their_queries_filter_on(trino, cairn):
    """Automatic clustering on real Trino: the table's queries (Trino's query history, sent to Cairn) filter on
    day and region, so Cairn sorts the table by them and rewrites its overlapping files in that order."""
    from cairn.pipelines.maintenance import TableMaintenance
    from cairn.quality.collectors import ConnectionRegistry

    _, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    definition = s.data_access.registry.definitions()["lakehouse"]

    def execute(sql):
        return s.data_access.run_on_trino(definition, "urn:li:corpuser:admin", sql, 100_000)[1]
    execute("CREATE SCHEMA IF NOT EXISTS lake.clus")
    execute("CREATE TABLE lake.clus.events (id integer, day date, region varchar, note varchar)")
    for i in range(6):        # each insert spans every day: six files that all overlap
        execute("INSERT INTO lake.clus.events VALUES " + ", ".join(
            f"({i * 100 + d}, DATE '2024-01-{d + 1:02d}', '{['EU', 'US', 'APAC'][d % 3]}', 'n{i}')" for d in range(20)))
    urn = s.trino_access.urn("lake", "clus", "events")
    since = int(time.time() * 1000) - 60_000
    already = len(s.query_history.reading(urn, since))
    for d in (3, 4, 5, 6, 7):
        trino(f"SELECT count(*) AS n FROM lake.clus.events WHERE day = DATE '2024-01-{d:02d}'")
    trino("SELECT count(*) AS n FROM lake.clus.events e WHERE e.region = 'EU' AND note LIKE 'n%'")
    trino("SELECT region, count(*) AS n FROM lake.clus.events GROUP BY region HAVING region <> 'US'")
    trino("SELECT count(*) AS n FROM lake.clus.events a JOIN lake.sales.orders o ON a.id = o.id")
    deadline = time.time() + 60
    while len(s.query_history.reading(urn, since)) < already + 8 and time.time() < deadline:
        time.sleep(1)
    assert len(s.query_history.reading(urn, since)) >= already + 8

    m = TableMaintenance(s.engine, execute, "lake", s.audit,
                         lambda schema, table, since_ms: s.query_history.reading(
                             s.trino_access.urn("lake", schema, table), since_ms))
    m.set_policy("clus.events", {"optimize": False, "cluster": "auto", "cluster_depth": 2}, "urn:li:corpuser:admin")
    assert len(trino('SELECT file_path FROM lake.clus."events$files"')) == 6
    [run] = m.maintain("clus", "events")
    assert run["operation"] == "cluster" and run["status"] == "ok", run
    assert run["detail"].startswith("sorted by day, region (was unsorted); rewritten, depth 6.0 -> 1.0"), run["detail"]
    ddl = trino("SHOW CREATE TABLE lake.clus.events")[0]["Create Table"]
    assert "sorted_by = ARRAY['day ASC NULLS FIRST','region ASC NULLS FIRST']" in ddl.replace(", ", ","), ddl
    assert len(trino("SELECT id FROM lake.clus.events")) == 120                  # nothing lost
    [state] = [c for c in m.clustering() if c["table_name"] == "clus.events"]
    assert state["columns"] == ["day", "region"] and state["chosen_by"] == "auto" and state["depth"] == 1.0
    assert state["evidence"]["filters"]["day"] >= 5 and state["evidence"]["filters"].get("id", 0) <= 1
    # Clustered already: checked, not rewritten again.
    [again] = m.maintain("clus", "events")
    assert again["detail"] == "sorted by day, region; depth 1.0, not rewritten", again
    # A list of columns the table does not have is an error, recorded.
    m.set_policy("clus.events", {"optimize": False, "cluster": ["nope"]}, "urn:li:corpuser:admin")
    [bad] = m.maintain("clus", "events")
    assert bad["status"] == "error" and "no column nope" in bad["detail"]
    with pytest.raises(Exception, match="cluster is"):
        m.set_policy("clus", {"cluster": "sometimes"}, "urn:li:corpuser:admin")


def test_workbooks_compile_spreadsheet_formulas_to_sql_run_as_the_viewer(trino, cairn):
    """A workbook (calculated columns, filters, grouping, a pivot) compiled to one query on real Trino and run as
    whoever looks: the admin sees the values, Ana's emails are masked inside formulas too, Bob sees only EU rows."""
    import httpx

    from cairn.quality.collectors import ConnectionRegistry
    from cairn.sqlworkspace.workbooks import WorkbookError

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    urn = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.wb_orders,PROD)"
    s.graph.aspects[(urn, "schemaMetadata")] = {"fields": [
        {"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}}, {"fieldPath": "region"},
        {"fieldPath": "amount"}, {"fieldPath": "discount"}, {"fieldPath": "quarter"}]}
    trino("CREATE TABLE lake.sales.wb_orders AS SELECT * FROM (VALUES "
          "(1, 'jane@acme.com', 'EU', 100e0, 1e-1, 'Q1'), (2, 'bo@example.org', 'US', 50e0, 0e0, 'Q1'), "
          "(3, 'li@acme.com', 'EU', 200e0, 2e-1, 'Q2'), (4, 'max@acme.com', 'US', 80e0, 0e0, 'Q2')) "
          "AS t(id, email, region, amount, discount, quarter)")
    admin, ana, bob = ("urn:li:corpuser:" + u for u in ("admin", "ana", "bob"))
    rows_el = {"source": {"table": "sales.wb_orders"},
               "columns": [{"name": "Net", "formula": "[amount] * (1 - [discount])"},
                           {"name": "Domain", "formula": 'Mid([email], Len(Left([email], 1)) + 1, 100)'},
                           {"name": "Size", "formula": 'If([Net] >= 90, "large", "small")'}],
               "filters": [{"column": "Net", "op": ">", "value": 0}], "sort": [{"column": "id"}]}
    out = s.workbooks.run(admin, "lakehouse", rows_el)
    cols = out["columns"]
    got = [dict(zip(cols, r)) for r in out["rows"]]
    assert [(g["id"], round(g["Net"], 2), g["Size"]) for g in got] == [(1, 90.0, "large"), (2, 50.0, "small"),
                                                                      (3, 160.0, "large"), (4, 80.0, "small")]
    assert got[0]["Domain"] == "ane@acme.com"
    # Ana's email is masked before any formula sees it: the masked value is what her formula works on.
    mine = [dict(zip(out_a := s.workbooks.run(ana, "lakehouse", rows_el)["columns"], r))
            for r in s.workbooks.run(ana, "lakehouse", rows_el)["rows"]]
    assert all("acme" not in (m["email"] or "") and "acme" not in (m["Domain"] or "") for m in mine), mine
    assert "Net" in out_a
    # Bob's row filter applies under the workbook too.
    assert {r[cols.index("region")] for r in s.workbooks.run(bob, "lakehouse", rows_el)["rows"]} == {"EU"}

    # Grouped, with a filter on the groups, and a pivot whose values are found in the data (as the viewer sees it).
    grouped = {**rows_el, "group_by": ["region"],
               "aggregates": [{"name": "Revenue", "formula": "Sum([Net])"}, {"name": "Orders", "formula": "Count()"},
                              {"name": "Average", "formula": "Round(Avg([Net]), 1)"}],
               "filters": [{"column": "Revenue", "op": ">", "value": 100, "on": "groups"}],
               "sort": [{"column": "Revenue", "desc": True}]}
    g = s.workbooks.run(admin, "lakehouse", grouped)
    assert [tuple(r) for r in g["rows"]] == [("EU", 250.0, 2, 125.0), ("US", 130.0, 2, 65.0)], g["rows"]
    pivot = {"source": {"table": "sales.wb_orders"}, "group_by": ["region"],
             "pivot": {"column": "quarter", "aggregate": "Sum([amount])"}, "sort": [{"column": "region"}]}
    p = s.workbooks.run(admin, "lakehouse", pivot)
    assert p["columns"] == ["region", "Q1", "Q2"] and [tuple(r) for r in p["rows"]] == [("EU", 100.0, 200.0), ("US", 50.0, 80.0)]
    assert s.workbooks.run(bob, "lakehouse", pivot)["rows"] == [["EU", 100.0, 200.0]]

    # A formula naming a column the table does not have is refused before anything runs.
    with pytest.raises(WorkbookError, match="no column"):
        s.workbooks.run(admin, "lakehouse", {**rows_el, "columns": [{"name": "X", "formula": "[salary] * 2"}]})

    # Over HTTP: compile shows the SQL; a saved workbook is the owner's, shared ones open for others.
    h = {"X-Cairn-Actor": ana}
    r = httpx.post(f"{url}/sql/workbooks/compile", json={"connection": "lakehouse", "element": grouped}, headers=h, timeout=60)
    assert r.status_code == 200 and "GROUP BY" in r.json()["sql"], r.text
    r = httpx.post(f"{url}/sql/workbooks/run", json={"connection": "lakehouse", "element": rows_el}, headers=h, timeout=60)
    # A column computed from a masked one is masked too.
    assert r.status_code == 200 and r.json()["masked_columns"] == ["email", "Domain"], r.text
    saved = httpx.post(f"{url}/sql/workbooks", json={"name": "Orders by size", "connection": "lakehouse", "shared": True,
                                                     "spec": {"elements": [rows_el, grouped]}}, headers=h, timeout=30)
    assert saved.status_code == 201, saved.text
    wid = saved.json()["id"]
    assert httpx.get(f"{url}/sql/workbooks/{wid}", headers={"X-Cairn-Actor": bob}, timeout=30).json()["name"] == "Orders by size"
    assert httpx.put(f"{url}/sql/workbooks/{wid}", json={"name": "Mine now", "connection": "lakehouse",
                                                          "spec": {"elements": [rows_el]}},
                     headers={"X-Cairn-Actor": bob}, timeout=30).status_code == 403
    assert httpx.delete(f"{url}/sql/workbooks/{wid}", headers=h, timeout=30).status_code == 204


def test_interactive_dashboards_filter_cross_filter_and_drill_as_the_viewer(trino, cairn):
    """Interactive dashboards on real Trino: workbook tiles filtered by a values control (whose choices are the
    data as the viewer sees it), cross-filtered by a click in another tile, and drilled from region to
    country; each viewer's row filter still applies underneath."""
    import httpx

    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    urn = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.db_orders,PROD)"
    s.graph.aspects[(urn, "schemaMetadata")] = {"fields": [{"fieldPath": c} for c in ("id", "region", "country", "amount")]}
    trino("CREATE TABLE lake.sales.db_orders AS SELECT * FROM (VALUES (1, 'EU', 'DE', 100e0), (2, 'EU', 'FR', 50e0), "
          "(3, 'EU', 'DE', 30e0), (4, 'US', 'US', 80e0), (5, 'APAC', 'JP', 20e0)) AS t(id, region, country, amount)")
    admin, bob = ({"X-Cairn-Actor": "urn:li:corpuser:" + u} for u in ("admin", "bob"))
    by_region = {"source": {"table": "sales.db_orders"}, "group_by": ["region"],
                 "aggregates": [{"name": "Revenue", "formula": "Sum([amount])"}], "sort": [{"column": "region"}]}
    rows = {"source": {"table": "sales.db_orders"}, "sort": [{"column": "id"}]}
    r = httpx.post(f"{url}/sql/dashboards", headers=admin, timeout=60, json={
        "name": "Revenue", "shared": True,
        "tiles": [{"element": by_region, "connection": "lakehouse", "title": "By region", "drill": ["region", "country"],
                   "chart": {"type": "bar", "x": "region", "y": ["Revenue"]}},
                  {"element": rows, "connection": "lakehouse", "title": "Orders", "chart": {"type": "table"}}],
        "controls": [{"id": "region", "label": "Region", "column": "region", "kind": "values",
                      "source": {"connection": "lakehouse", "table": "sales.db_orders"}},
                     {"id": "amount", "column": "amount", "kind": "range"}]})
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    # A drill whose top is not what the element is grouped by is refused when saving.
    bad = httpx.post(f"{url}/sql/dashboards", headers=admin, timeout=60, json={
        "name": "x", "tiles": [{"element": by_region, "connection": "lakehouse", "drill": ["country", "region"]}]})
    assert bad.status_code == 400 and "drill" in bad.text

    values = lambda h: httpx.get(f"{url}/sql/dashboards/{did}/controls/region/values", headers=h, timeout=60).json()  # noqa: E731
    assert values(admin) == ["APAC", "EU", "US"]
    assert values(bob) == ["EU"]                                        # the choices are what Bob may see

    def render(h, state):
        r = httpx.post(f"{url}/sql/dashboards/{did}/render", json=state, headers=h, timeout=120)
        assert r.status_code == 200, r.text
        return r.json()["tiles"]

    t = render(admin, {"controls": {"region": ["EU", "US"], "amount": [40, None]}})
    assert [tuple(x) for x in t[0]["result"]["rows"]] == [("EU", 150.0), ("US", 80.0)], t[0]
    assert [x[0] for x in t[1]["result"]["rows"]] == [1, 2, 4]
    # A click on EU in the chart cross-filters the orders, not the chart itself.
    t = render(admin, {"selection": [{"column": "region", "value": "EU", "from": 0}]})
    assert len(t[0]["result"]["rows"]) == 3 and [x[0] for x in t[1]["result"]["rows"]] == [1, 2, 3]
    # Drill into EU: the chart is now by country, within EU.
    t = render(admin, {"drill": {"0": ["EU"]}})
    assert t[0]["result"]["columns"] == ["country", "Revenue"] and t[0]["chart"]["x"] == "country"
    assert [tuple(x) for x in t[0]["result"]["rows"]] == [("DE", 130.0), ("FR", 50.0)], t[0]["result"]
    assert t[0]["level"] == {"path": ["EU"], "column": "country"}
    assert "error" in render(admin, {"drill": {"0": ["EU", "DE"]}})[0]          # no level below country
    # Bob's row filter applies under every control and click.
    t = render(bob, {"controls": {"region": ["EU", "US"]}})
    assert [x[0] for x in t[0]["result"]["rows"]] == ["EU"] and {x[1] for x in t[1]["result"]["rows"]} == {"EU"}


def test_metrics_are_defined_once_and_mean_the_same_in_queries_and_dashboards(trino, cairn):
    """The metrics layer on real Trino: revenue (paid orders only) and orders, defined once, queried by region and
    month as each viewer (Bob sees only EU), shown on a dashboard tile that follows a change of definition."""
    import httpx

    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    urn = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.m_orders,PROD)"
    s.graph.aspects[(urn, "schemaMetadata")] = {"fields": [{"fieldPath": c} for c in ("id", "region", "status", "amount", "ordered_at")]}
    trino("CREATE TABLE lake.sales.m_orders AS SELECT id, region, status, amount, CAST(d AS date) AS ordered_at FROM (VALUES "
          "(1, 'EU', 'paid', 100e0, '2026-01-05'), (2, 'EU', 'refunded', 40e0, '2026-01-20'), (3, 'US', 'paid', 70e0, '2026-01-09'), "
          "(4, 'EU', 'paid', 30e0, '2026-02-02'), (5, 'US', 'paid', 20e0, '2026-02-14')) AS t(id, region, status, amount, d)")
    admin, ana, bob = ({"X-Cairn-Actor": "urn:li:corpuser:" + u} for u in ("admin", "ana", "bob"))
    base = f"{url}/sql/metrics"
    revenue = {"name": "revenue", "label": "Revenue", "connection": "lakehouse", "table": "sales.m_orders",
               "measure": "Sum([amount])", "filters": [{"column": "status", "op": "=", "value": "paid"}],
               "time_column": "ordered_at", "dimensions": ["region", "status"]}
    r = httpx.post(base, json=revenue, headers=ana, timeout=60)
    assert r.status_code == 201 and r.json()["version"] == 1 and not r.json()["certified"], r.text
    r = httpx.post(base, json={**revenue, "name": "orders", "label": "Orders", "measure": "Count()", "filters": []},
                   headers=ana, timeout=60)
    assert r.status_code == 201, r.text
    # A definition that does not compile is refused when saved.
    bad = httpx.post(base, json={**revenue, "name": "broken", "measure": "[amount]"}, headers=ana, timeout=60)
    assert bad.status_code == 400 and "aggregate" in bad.text
    assert httpx.post(base, json={**revenue, "name": "nope", "time_column": "shipped_at"}, headers=ana,
                      timeout=60).status_code == 400

    def query(h, q):
        r = httpx.post(f"{base}/query", json=q, headers=h, timeout=120)
        assert r.status_code == 200, r.text
        out = r.json()
        return [dict(zip(out["columns"], x)) for x in out["rows"]]

    rows = query(admin, {"metrics": ["revenue", "orders"], "dimensions": ["region"], "grain": "month"})
    got = {(x["region"], str(x["period"])[:7]): (x["revenue"], x["orders"]) for x in rows}
    assert got == {("EU", "2026-01"): (100.0, 2), ("US", "2026-01"): (70.0, 1),
                   ("EU", "2026-02"): (30.0, 1), ("US", "2026-02"): (20.0, 1)}, got
    # Bob's row filter applies under the metric: the same definition, only his rows.
    assert {x["region"] for x in query(bob, {"metrics": ["revenue"], "dimensions": ["region"]})} == {"EU"}
    r = httpx.post(f"{base}/query", json={"metrics": ["revenue"], "dimensions": ["id"]}, headers=admin, timeout=60)
    assert r.status_code == 400 and "not broken down by id" in r.text
    # Only an operator certifies; only the owner (or an operator) changes it.
    assert httpx.post(f"{base}/revenue/certify", json={}, headers=ana, timeout=30).status_code == 403
    assert httpx.post(f"{base}/revenue/certify", json={}, headers=admin, timeout=30).json()["certified"] is True
    assert httpx.put(f"{base}/revenue", json=revenue, headers=bob, timeout=60).status_code == 403

    # A dashboard tile of the metric, filtered by a control on the metric's table.
    r = httpx.post(f"{url}/sql/dashboards", headers=admin, timeout=60, json={
        "name": "Revenue KPIs", "shared": True,
        "tiles": [{"metric": {"metrics": ["revenue"], "dimensions": ["region"]}, "title": "Revenue by region"}],
        "controls": [{"id": "status", "column": "status", "kind": "text"}]})
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    tile = lambda: httpx.post(f"{url}/sql/dashboards/{did}/render", json={}, headers=admin, timeout=120).json()["tiles"][0]  # noqa: E731
    first = tile()
    assert first["chart"] == {"type": "bar", "x": "region", "y": ["revenue"]}
    assert sorted(map(tuple, first["result"]["rows"])) == [("EU", 130.0), ("US", 90.0)], first
    # The owner changes the definition (refunds count too): the tile follows, and certification is withdrawn.
    changed = httpx.put(f"{base}/revenue", json={**revenue, "filters": []}, headers=ana, timeout=60).json()
    assert changed["version"] == 2 and changed["certified"] is False
    assert sorted(map(tuple, tile()["result"]["rows"])) == [("EU", 170.0), ("US", 90.0)]
    actions = [e["action"] for e in s.audit.entries("sql", limit=100) if e.get("subject") == "metric:revenue"]
    assert {"metric.saved", "metric.certified"} <= set(actions), actions


def test_the_ai_builder_makes_a_dashboard_from_a_question_checked_as_the_person(trino, cairn):
    """The AI builder on real Trino, the model scripted: its plan's tiles are compiled as the person asking, the
    ones naming an unknown metric or column are refused and the model told why, the preview runs as the person
    (Bob sees only EU), and the dashboard is saved as theirs. Ask-your-data is given the metric's definition."""
    import httpx

    from cairn.ai.llm import StaticProvider
    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    urn = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.ai_orders,PROD)"
    s.graph.aspects[(urn, "schemaMetadata")] = {"fields": [{"fieldPath": c} for c in ("id", "region", "status", "amount")]}
    trino("CREATE TABLE lake.sales.ai_orders AS SELECT * FROM (VALUES (1, 'EU', 'paid', 100e0), (2, 'EU', 'refunded', 40e0), "
          "(3, 'US', 'paid', 70e0)) AS t(id, region, status, amount)")
    admin, bob = "urn:li:corpuser:admin", "urn:li:corpuser:bob"
    s.metrics.save(admin, {"name": "ai_revenue", "label": "Revenue", "connection": "lakehouse", "table": "sales.ai_orders",
                           "measure": "Sum([amount])", "filters": [{"column": "status", "op": "=", "value": "paid"}],
                           "dimensions": ["region"]})
    good = [{"kind": "metric", "title": "Revenue by region", "metric": {"metrics": ["ai_revenue"], "dimensions": ["region"]}},
            {"kind": "element", "title": "Orders by status", "chart": {"type": "pie", "x": "status", "y": ["Orders"]},
             "element": {"source": {"table": "sales.ai_orders"}, "group_by": ["status"],
                         "aggregates": [{"name": "Orders", "formula": "Count()"}], "sort": [{"column": "status"}]}}]
    bad = [{"kind": "metric", "title": "Profit", "metric": {"metrics": ["profit"]}},
           {"kind": "element", "title": "Margin", "element": {"source": {"table": "sales.ai_orders"},
                                                              "aggregates": [{"name": "M", "formula": "Sum([margin])"}]}}]
    calls = []

    def responder(system, user, schema):
        calls.append(user)
        if "dashboard" in system:
            tiles = good + bad if len([c for c in calls if "Question:" in c and "Tables:" in c]) == 1 else good
            return {"title": "Sales overview", "tiles": tiles,
                    "controls": [{"id": "region", "label": "Region", "column": "region", "kind": "values",
                                  "source": {"table": "sales.ai_orders"}}]}
        return {"sql": 'SELECT region, sum(CASE WHEN "status" = \'paid\' THEN "amount" END) AS revenue '
                       "FROM sales.ai_orders GROUP BY region ORDER BY region", "explanation": "Revenue by region."}
    model = StaticProvider(responder)
    s.builder.llm, s.ask.sql.llm = model, model

    r = httpx.post(f"{url}/sql/builder", json={"question": "How is revenue doing by region, and orders by status?",
                                               "datasets": [urn], "save": True},
                   headers={"X-Cairn-Actor": admin}, timeout=120)
    assert r.status_code == 200, r.text
    out = r.json()
    assert [t["title"] for t in out["tiles"]] == ["Revenue by region", "Orders by status"]
    assert sorted(map(tuple, out["tiles"][0]["result"]["rows"])) == [("EU", 100.0), ("US", 70.0)], out["tiles"][0]
    assert out["tiles"][1]["result"]["rows"] == [["paid", 2], ["refunded", 1]]
    assert out["controls"][0]["source"] == {"connection": "lakehouse", "table": "sales.ai_orders"}
    # The first plan's bad tiles were refused as the person, and the model was told why before its second try.
    retry = [c for c in calls if "A previous tile was refused" in c]
    assert retry and "profit is not one of the metrics listed" in retry[0] and "no column [margin]" in retry[0], retry
    assert out["refused"] == []                                         # the second plan built cleanly
    saved = s.sql_boards.dashboard(admin, out["dashboard"]["id"])
    assert saved["name"] == "Sales overview" and not saved["shared"] and len(saved["tiles"]) == 2
    # The model never writes SQL for tiles, and Bob's preview is his own rows.
    b = httpx.post(f"{url}/sql/builder", json={"question": "Revenue by region", "datasets": [urn]},
                   headers={"X-Cairn-Actor": bob}, timeout=120).json()
    assert [r[0] for r in b["tiles"][0]["result"]["rows"]] == ["EU"] and b["dashboard"] is None
    assert any(e["action"] == "bi.built" for e in s.audit.entries("sql", limit=50))

    # Ask-your-data is told the metric's definition for the tables it uses.
    a = s.ask.ask(admin, "What is revenue by region?", lambda u: True, [urn])
    assert a["error"] is None and "ai_revenue" in [d["name"] for d in a["definitions"]], a
    assert "metric ai_revenue = sum(CASE WHEN \"status\" = 'paid' THEN \"amount\" END)" in calls[-1]


def test_dashboard_actions_write_run_and_call_as_the_person_who_clicks(trino, cairn, monkeypatch):
    """App actions on real Trino: a form adds a row to an input table and a button approves the clicked row, each
    written as the person clicking (Ana, who may not write, is refused by Trino); a workflow button runs only for
    those it is meant for; a webhook goes only to an allowed origin, signed. Every run is recorded."""
    import hashlib
    import hmac
    import http.server
    import threading

    import httpx

    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    admin, ana = {"X-Cairn-Actor": "urn:li:corpuser:admin"}, {"X-Cairn-Actor": "urn:li:corpuser:ana"}
    t = httpx.post(f"{url}/sql/input-tables", headers=admin, timeout=60, json={
        "connection": "lakehouse", "table": "sales.requests",
        "columns": [{"name": "item", "type": "text"}, {"name": "amount", "type": "number"}, {"name": "status", "type": "text"}]})
    assert t.status_code == 201, t.text
    tid = t.json()["id"]
    board = httpx.post(f"{url}/sql/dashboards", headers=admin, timeout=60, json={
        "name": "Requests", "shared": True,
        "tiles": [{"element": {"source": {"table": "sales.requests"}, "sort": [{"column": "item"}]}, "connection": "lakehouse",
                   "title": "Requests"}]}).json()
    did = board["id"]
    base = f"{url}/sql/dashboards/{did}/actions"

    def add(body, h=admin):
        return httpx.post(base, json=body, headers=h, timeout=60)

    r = add({"name": "request", "label": "New request", "kind": "input_row", "input_table": tid,
             "fields": [{"name": "item", "required": True}, "amount"], "constants": {"status": "open"}})
    assert r.status_code == 201, r.text
    request = r.json()["id"]
    approve = add({"name": "approve", "label": "Approve", "kind": "input_update", "input_table": tid,
                   "set": {"status": "approved"}, "confirm": "Approve this request?"}).json()["id"]
    for bad, msg in [({"name": "x", "kind": "input_row", "input_table": tid, "fields": ["nope"]}, "not a column"),
                     ({"name": "y", "kind": "input_update", "input_table": tid, "set": {"amount": "lots"}}, "is not a number"),
                     ({"name": "z", "kind": "teleport"}, "kind")]:
        r = add(bad)
        assert r.status_code == 400 and msg in r.text, (bad, r.text)
    assert add({"name": "mine", "kind": "input_row", "input_table": tid, "fields": ["item"]}, ana).status_code == 403

    run = lambda aid, h, **body: httpx.post(f"{base}/{aid}/run", json=body, headers=h, timeout=120)  # noqa: E731
    r = run(request, admin, inputs={"item": "laptop", "amount": 1200})
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    assert run(request, admin, inputs={"amount": 5}).status_code == 400                      # item is required
    assert run(request, admin, inputs={"item": "x", "status": "approved"}).status_code == 400  # not a field
    # Ana sees the dashboard and its actions, but Trino refuses her write (no WRITE_DATASET).
    assert [x["name"] for x in httpx.get(base, headers=ana, timeout=30).json()] == ["request", "approve"]
    assert run(request, ana, inputs={"item": "monitor"}).status_code == 403
    rows = s.input_tables.rows("urn:li:corpuser:admin", tid)
    row = dict(zip(rows["columns"], rows["rows"][0]))
    assert (row["item"], row["amount"], row["status"]) == ("laptop", 1200.0, "open")
    r = run(approve, admin, row={"_row_id": row["_row_id"], "_updated_at": row["_updated_at"]})
    assert r.status_code == 200, r.text
    assert trino("SELECT status FROM lake.sales.requests") == [{"status": "approved"}]
    assert run(approve, admin, row={"_row_id": row["_row_id"], "_updated_at": row["_updated_at"]}).status_code == 409

    # A workflow: only its owner puts it on a dashboard; "who" decides who may click.
    wf = s.sql_workflows.save("urn:li:corpuser:admin", {"name": "count requests", "steps": [
        {"name": "count", "type": "query", "sql": "SELECT count(*) AS n FROM sales.requests", "connection": "lakehouse"}]})
    mine = add({"name": "refresh", "kind": "workflow", "workflow": wf["id"]}).json()["id"]
    theirs = add({"name": "refresh_all", "kind": "workflow", "workflow": wf["id"], "who": "viewers"}).json()["id"]
    assert run(mine, ana).status_code == 403
    r = run(theirs, ana)
    assert r.status_code == 200 and r.json()["steps"] == [{"name": "count", "status": "ok"}], r.text

    # A webhook: only an operator adds one, only to an allowed origin, signed with Cairn's secret.
    got = []

    class Hook(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    hook = f"http://127.0.0.1:{srv.server_port}"
    body = {"name": "notify", "kind": "webhook", "url": f"{hook}/requests", "fields": [{"name": "note", "type": "text"}]}
    monkeypatch.delenv("CAIRN_ACTION_WEBHOOK_ORIGINS", raising=False)
    assert add(body).status_code == 403                                  # no origin allowed yet
    monkeypatch.setenv("CAIRN_ACTION_WEBHOOK_ORIGINS", hook)
    monkeypatch.setenv("CAIRN_ACTION_WEBHOOK_SECRET", "hook-secret")
    assert add({**body, "url": "http://127.0.0.1:1/elsewhere"}).status_code == 403
    notify = add(body).json()
    assert "url" not in notify["config"]                                  # viewers do not see where it goes
    r = run(notify["id"], ana, inputs={"note": "please hurry"})
    assert r.status_code == 200 and r.json()["answered"] == 204, r.text
    headers, sent = got[0]
    assert json.loads(sent)["actor"] == "urn:li:corpuser:ana" and json.loads(sent)["inputs"] == {"note": "please hurry"}
    assert headers["X-Cairn-Signature"] == "sha256=" + hmac.new(b"hook-secret", sent, hashlib.sha256).hexdigest()
    srv.shutdown()

    # Every run is kept for the dashboard's owner (who, the outcome, not the values).
    runs = httpx.get(f"{base}/{request}/runs", headers=admin, timeout=30).json()
    assert sorted(x["status"] for x in runs) == ["error", "error", "ok", "refused"], runs
    assert any(x["actor"] == "urn:li:corpuser:ana" and x["status"] == "refused" for x in runs)
    assert httpx.get(f"{base}/{request}/runs", headers=ana, timeout=30).status_code == 403
    assert sum(e["action"] == "bi.action_run" for e in s.audit.entries("sql", limit=200)) >= 8


def test_results_are_shared_only_between_people_who_see_the_same_and_exports_are_each_recipients_own(trino, cairn):
    """The result cache and scheduled exports on real Trino. Ana and Carol (the same policies) share a cached tile;
    Bob (an EU row filter) and the admin (no masks) get their own; a change to the table drops it; "fresh" goes
    past it. A scheduled export renders each recipient's copy as them (Bob's has only EU rows), each copy only
    for its recipient, a recipient who may not open the dashboard is skipped, and the email carries the file."""
    import io
    import socket

    import httpx
    from pypdf import PdfReader
    from sqlalchemy import update

    from cairn.notify.channels import EmailSender
    from cairn.quality.collectors import ConnectionRegistry
    from cairn.sqlworkspace.reports import report_schedules

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    urn = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.c_orders,PROD)"
    s.graph.aspects[(urn, "schemaMetadata")] = {"fields": [{"fieldPath": c} for c in ("id", "region", "amount")]}
    trino("CREATE TABLE lake.sales.c_orders AS SELECT * FROM (VALUES (1, 'EU', 100e0), (2, 'US', 70e0), (3, 'EU', 30e0)) "
          "AS t(id, region, amount)")
    who = {u: {"X-Cairn-Actor": f"urn:li:corpuser:{u}"} for u in ("admin", "ana", "carol", "bob")}
    board = httpx.post(f"{url}/sql/dashboards", headers=who["admin"], timeout=60, json={
        "name": "Orders by region", "shared": True,
        "tiles": [{"element": {"source": {"table": "sales.c_orders"}, "group_by": ["region"],
                               "aggregates": [{"name": "Total", "formula": "Sum([amount])"}], "sort": [{"column": "region"}]},
                   "connection": "lakehouse", "title": "By region"}]}).json()
    cache = s.sql_workspace.cache
    cache.clear()

    def tile(user, state=None):
        r = httpx.post(f"{url}/sql/dashboards/{board['id']}/render", json=state or {}, headers=who[user], timeout=120)
        assert r.status_code == 200, r.text
        return r.json()["tiles"][0]["result"]

    first = tile("ana")
    assert not first.get("cached") and cache.stats() == {**cache.stats(), "entries": 1, "hits": 0}
    shared = tile("carol")                                               # the same policies as Ana: her result
    assert shared.get("cached") is True and shared["rows"] == first["rows"] and cache.stats()["hits"] == 1
    bob = tile("bob")                                                    # an EU row filter: his own
    assert not bob.get("cached") and bob["rows"] == [["EU", 130.0]] and cache.stats()["entries"] == 2
    assert not tile("admin").get("cached") and cache.stats()["entries"] == 3   # no masks: his own too
    assert not tile("ana", {"fresh": True}).get("cached")              # past the cache
    s.dataset_changes.record([urn], "test")                              # the table changed: dropped
    assert not tile("carol").get("cached")
    assert any(e["action"] == "data.queried" and (e.get("details") or {}).get("cached")
               for e in s.audit.entries("data", limit=100))

    # A scheduled export: each recipient's copy rendered as them.
    rep = f"{url}/sql/reports"
    r = httpx.post(rep, headers=who["admin"], timeout=60, json={
        "name": "Weekly orders", "target": {"dashboard": board["id"]}, "format": "csv", "every_minutes": 10080,
        "recipients": ["urn:li:corpuser:bob", "urn:li:corpuser:ana"]})
    assert r.status_code == 201 and r.json()["recipients"][0] == "urn:li:corpuser:admin", r.text
    sid = r.json()["id"]
    assert httpx.post(rep, headers=who["admin"], timeout=60, json={"name": "x", "target": {"dashboard": board["id"]},
                                                                   "every_minutes": 5}).status_code == 400
    sent = httpx.post(f"{rep}/{sid}/run", headers=who["admin"], timeout=180).json()
    files = {x["recipient"].removeprefix("urn:li:corpuser:"): x["file"] for x in sent["sent"]}
    assert set(files) == {"admin", "bob", "ana"} and sent["skipped"] == []
    bob_csv = httpx.get(f"{rep}/files/{files['bob']}", headers=who["bob"], timeout=30)
    assert bob_csv.status_code == 200 and bob_csv.headers["content-type"].startswith("text/csv")
    assert bob_csv.text.splitlines()[:3] == ["# By region", "region,Total", "EU,130.0"] and "US" not in bob_csv.text
    assert "US,70.0" in httpx.get(f"{rep}/files/{files['admin']}", headers=who["admin"], timeout=30).text
    assert httpx.get(f"{rep}/files/{files['bob']}", headers=who["ana"], timeout=30).status_code == 404   # his alone

    # PDF, and a recipient who may not open a private dashboard gets nothing.
    private = httpx.post(f"{url}/sql/dashboards", headers=who["admin"], timeout=60, json={
        "name": "Private", "tiles": board["tiles"]}).json()
    pdf = httpx.post(rep, headers=who["admin"], timeout=60, json={
        "name": "Private orders", "target": {"dashboard": private["id"]}, "format": "pdf",
        "recipients": ["urn:li:corpuser:bob"]}).json()
    out = httpx.post(f"{rep}/{pdf['id']}/run", headers=who["admin"], timeout=180).json()
    assert [x["recipient"] for x in out["skipped"]] == ["urn:li:corpuser:bob"] and len(out["sent"]) == 1
    doc = httpx.get(f"{rep}/files/{out['sent'][0]['file']}", headers=who["admin"], timeout=30)
    text = PdfReader(io.BytesIO(doc.content)).pages[0].extract_text()
    assert doc.headers["content-type"] == "application/pdf" and "By region" in text and "US" in text

    # Due schedules are claimed once.
    with s.engine.begin() as conn:
        conn.execute(update(report_schedules).where(report_schedules.c.id == sid).values(next_ms=1))
    assert s.report_schedules.run_due() >= 1 and s.report_schedules.run_due() == 0

    # The email carries the recipient's file (a real SMTP server: Mailpit).
    mail, mp = f"cairn-reports-mail-{os.getpid()}", port()
    smtp = port()
    subprocess.run(["docker", "run", "-d", "--rm", "--name", mail, "-p", f"127.0.0.1:{mp}:8025", "-p", f"127.0.0.1:{smtp}:1025",
                    os.environ.get("CAIRN_MAILPIT_IMAGE", "axllent/mailpit:v1.21.8")], check=True, capture_output=True, timeout=300)
    try:
        end = time.time() + 60
        while True:
            try:
                if httpx.get(f"http://127.0.0.1:{mp}/api/v1/messages", timeout=2).status_code == 200:
                    socket.create_connection(("127.0.0.1", smtp), 2).close()
                    break
            except (httpx.HTTPError, OSError):
                pass
            assert time.time() < end, "Mailpit did not start"
            time.sleep(1)
        sender = EmailSender(f"smtp://127.0.0.1:{smtp}?starttls=false", "cairn@test.example", lambda a: "bob@test.example",
                             lambda row: [s.report_schedules.attachment(row["payload"]["file"])])
        sender.send({"recipient": "urn:li:corpuser:bob", "type": "report.ready", "title": "Weekly orders (CSV)",
                     "body": "Your copy is ready.", "payload": {"file": files["bob"], "url": "/sql/reports/files/x"}}, None)
        msgs = httpx.get(f"http://127.0.0.1:{mp}/api/v1/messages", timeout=10).json()["messages"]
        assert msgs and msgs[0]["Attachments"] == 1, msgs
        mid = msgs[0]["ID"]
        parts = httpx.get(f"http://127.0.0.1:{mp}/api/v1/message/{mid}", timeout=10).json()["Attachments"]
        assert parts[0]["FileName"].endswith(".csv") and parts[0]["ContentType"].startswith("text/csv")
        body = httpx.get(f"http://127.0.0.1:{mp}/api/v1/message/{mid}/part/{parts[0]['PartID']}", timeout=10).text
        assert "EU,130.0" in body and "US" not in body
    finally:
        subprocess.run(["docker", "rm", "-f", mail], capture_output=True)


def test_input_tables_are_written_on_trino_as_the_person_and_read_back_governed(trino, cairn):
    """Input tables (writeback): a table people type into, created and changed on real Trino as the person
    writing, so Cairn's policies decide each statement; rows read back masked and filtered like any table,
    a stale edit is refused, every change is audited, and a workbook reads the table at once."""
    import httpx

    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    admin, ana, bob = ({"X-Cairn-Actor": "urn:li:corpuser:" + u} for u in ("admin", "ana", "bob"))
    base = f"{url}/sql/input-tables"
    spec = {"connection": "lakehouse", "table": "sales.targets", "description": "Quarterly targets, typed in",
            "columns": [{"name": "region", "type": "text"}, {"name": "target", "type": "number"},
                        {"name": "due", "type": "date"}, {"name": "approved", "type": "boolean"}]}
    # Ana may read sales tables but not create one: Trino asks Cairn, and Cairn refuses.
    r = httpx.post(base, json={**spec, "table": "sales.anas"}, headers=ana, timeout=60)
    assert r.status_code == 403, r.text
    assert httpx.post(base, json={**spec, "columns": [{"name": "Bad Name", "type": "text"}]}, headers=admin,
                      timeout=30).status_code == 400
    r = httpx.post(base, json=spec, headers=admin, timeout=60)
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert httpx.post(base, json=spec, headers=admin, timeout=30).status_code == 409          # one input table per table
    ddl = trino("SHOW CREATE TABLE lake.sales.targets")[0]["Create Table"]
    assert '"_updated_at" timestamp(6) with time zone' in ddl or "_updated_at timestamp(6) with time zone" in ddl, ddl
    urn = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.targets,PROD)"
    assert (s.graph.get_aspect(urn, "datasetProperties") or {}).get("customProperties", {}).get("cairn_input_table") == "true"

    rows = [{"region": "EU", "target": 1200.5, "due": "2026-12-31", "approved": True},
            {"region": "US", "target": 900, "due": "2026-12-31", "approved": False},
            {"region": "EU", "target": "300", "due": None, "approved": "false"}]
    r = httpx.post(f"{base}/{tid}/rows", json={"rows": rows}, headers=admin, timeout=60)
    assert r.status_code == 201 and len(r.json()["added"]) == 3, r.text
    # Values are checked against the column's type and written as literals, never as SQL.
    for bad in ({"target": "lots"}, {"due": "tomorrow"}, {"approved": "maybe"}, {"nope": 1},
                {"region": "x'); DROP TABLE sales.targets; --", "target": "1); DROP"}):
        assert httpx.post(f"{base}/{tid}/rows", json={"rows": [bad]}, headers=admin, timeout=30).status_code == 400, bad
    # Ana can read but not write: Trino refuses her INSERT (no WRITE_DATASET).
    r = httpx.post(f"{base}/{tid}/rows", json={"rows": [{"region": "EU", "target": 1}]}, headers=ana, timeout=60)
    assert r.status_code == 403, r.text

    got = httpx.get(f"{base}/{tid}/rows", headers=admin, timeout=60).json()
    cols = got["columns"]
    table = [dict(zip(cols, x)) for x in got["rows"]]
    assert sorted((t["region"], t["target"]) for t in table) == [("EU", 300.0), ("EU", 1200.5), ("US", 900.0)], table
    assert {t["_updated_by"] for t in table} == {"urn:li:corpuser:admin"}
    # Bob's row filter applies to typed-in rows like any other.
    assert {x[cols.index("region")] for x in httpx.get(f"{base}/{tid}/rows", headers=bob, timeout=60).json()["rows"]} == {"EU"}

    us = next(t for t in table if t["region"] == "US")
    r = httpx.patch(f"{base}/{tid}/rows/{us['_row_id']}", json={"values": {"target": 950, "approved": True},
                                                                "expected_updated_at": us["_updated_at"]},
                    headers=admin, timeout=60)
    assert r.status_code == 200, r.text
    # The same edit again carries the old _updated_at: someone (here, the admin) changed the row since.
    r = httpx.patch(f"{base}/{tid}/rows/{us['_row_id']}", json={"values": {"target": 1},
                                                                "expected_updated_at": us["_updated_at"]},
                    headers=admin, timeout=60)
    assert r.status_code == 409, r.text
    assert httpx.patch(f"{base}/{tid}/rows/{'0' * 32}", json={"values": {"target": 1}}, headers=admin,
                       timeout=60).status_code == 404
    assert trino("SELECT target, approved FROM lake.sales.targets WHERE region = 'US'") == [{"target": 950.0, "approved": True}]

    # A workbook reads the input table straight away (it is in the catalog).
    w = s.workbooks.run("urn:li:corpuser:admin", "lakehouse", {
        "source": {"table": "sales.targets"}, "group_by": ["region"],
        "aggregates": [{"name": "Total", "formula": "Sum([target])"}], "sort": [{"column": "region"}]})
    assert [tuple(x) for x in w["rows"]] == [("EU", 1500.5), ("US", 950.0)], w["rows"]

    small = [t["_row_id"] for t in table if t["target"] == 300.0]
    r = httpx.request("DELETE", f"{base}/{tid}/rows", json={"rows": small}, headers=admin, timeout=60)
    assert r.status_code == 200 and r.json()["deleted"] == 1, r.text
    assert len(trino("SELECT * FROM lake.sales.targets")) == 2
    actions = [e["action"] for e in s.audit.entries("sql", limit=100) if (e.get("subject") or "") == "sales.targets"]
    for a in ("input.created", "input.rows_added", "input.row_changed", "input.rows_deleted"):
        assert a in actions, actions
    # Listed for anyone (other tests in this module add input tables of their own).
    assert "sales.targets" in [t["table_name"] for t in httpx.get(base, headers=bob, timeout=30).json()]
    # Only its creator (or an operator) stops it being an input table; the data stays.
    assert httpx.delete(f"{base}/{tid}", headers=bob, timeout=30).status_code == 403
    assert httpx.delete(f"{base}/{tid}", headers=admin, timeout=30).status_code == 204
    assert len(trino("SELECT * FROM lake.sales.targets")) == 2


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

    # A workflow chains them, as its owner: refresh, then a check on what was written (real Trino).
    from cairn.sqlworkspace.workflows import Workflows
    from cairn.sqlworkspace.workspace import WorkspaceError

    wf = Workflows(s.engine, s.sql_workspace, dt, None, lambda actor: False)
    # The SQL workspace reads only tables the catalog knows (the crawler would have registered it).
    s.graph.aspects[(dt.urn("shop", "latest"), "schemaMetadata")] = {"fields": [
        {"fieldPath": c} for c in ("id", "region", "amount", "seq")]}
    run(admin, "INSERT INTO lake.shop.orders VALUES (7, 'US', 2.0, 7)")
    w = wf.save(admin, {"name": "shop nightly", "steps": [
        {"name": "latest", "type": "derived", "table": "latest"},
        {"name": "has-7", "type": "check", "after": ["latest"],
         "query": s.sql_workspace.save(admin, {"name": "n", "connection": "lakehouse",
                                               "sql": "SELECT count(*) AS n FROM shop.latest WHERE id = 7"})["id"],
         "measure": "n", "op": "==", "value": 1}]})
    out = wf.run(w["id"])
    assert out["status"] == "ok", out
    assert [st["status"] for st in out["steps"]] == ["ok", "ok"]
    with pytest.raises(WorkspaceError, match="not yours"):            # ana may not schedule admin's table
        wf.save("urn:li:corpuser:ana", {"name": "x", "steps": [{"name": "l", "type": "derived", "table": "latest"}]})


def test_ai_and_python_functions_run_in_sql_under_cairns_policies(trino, cairn):
    """Trino's AI functions call a model only for people Cairn allows (USE_AI_FUNCTIONS), and the model
    is sent what the caller sees: masked values stay masked. Python functions run inline, sandboxed,
    on the same masked values."""
    from cairn.governance.abac import Policy, PolicyBundle

    _, s = cairn
    q = "SELECT llm.ai.ai_analyze_sentiment(email) AS s FROM sales.orders WHERE id = 1"
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino(q, user="ana")
    assert trino.model.prompts == []                                  # refused before any call
    b = s.pdp.bundle() if callable(s.pdp.bundle) else s.pdp.bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*b.policies, Policy(id="ana-ai", effect="allow", actions=["USE_AI_FUNCTIONS"],
                                                                 condition={"attr": "subject.urn", "op": "eq",
                                                                            "value": "urn:li:corpuser:ana"})]))
    try:
        assert trino(q, user="ana") == [{"s": "positive"}]
        assert any("j•••@•••.com" in p for p in trino.model.prompts), trino.model.prompts
        assert not any("jane@acme.com" in p for p in trino.model.prompts), trino.model.prompts   # the raw value never left
        with pytest.raises(RuntimeError, match="Access Denied"):
            trino(q, user="bob")
        # Through Cairn: the provider got Cairn's key, and the call is in Cairn's AI usage, with ana's query.
        sent = {k.lower(): v for k, v in trino.model.bodies[-1]["headers"].items()}
        assert sent["authorization"] == "Bearer provider-key-held-by-cairn"
        by_actor = {r["key"]: r for r in s.ai_usage.summary(group_by="actor")["groups"]}
        assert by_actor["trino-sql"]["input_tokens"] >= 1 and by_actor["urn:li:corpuser:ana"]["calls"] >= 1, by_actor
        calls = len(trino.model.prompts)
        # A daily cap per person: ana has used hers.
        s.sql_ai.user_daily_queries = s.sql_ai.queries_today("urn:li:corpuser:ana")
        with pytest.raises(RuntimeError, match="Access Denied"):
            trino(q, user="ana")
        s.sql_ai.user_daily_queries = None
        # A model that is not allowed, and the deployment's budget used up: refused, nothing sent.
        s.sql_ai.models = ["another-model"]
        fresh = q.replace("id = 1", "id = 2")       # the connector keeps answers to prompts it has seen
        with pytest.raises(RuntimeError, match="AI provider"):     # Cairn's reason is in its log; Trino says this
            trino(fresh, user="ana")
        s.sql_ai.models = ["stub-model"]
        s.ai_usage.daily_budget_usd = 0.0
        with pytest.raises(RuntimeError, match="Access Denied|budget"):
            trino(q.replace("id = 1", "id = 3"), user="ana")
        s.ai_usage.daily_budget_usd = None
        assert len(trino.model.prompts) == calls
    finally:
        s.pdp.set_bundle(b)
    py = ("WITH FUNCTION domain(e varchar) RETURNS varchar LANGUAGE PYTHON WITH (handler = 'domain') "
          "AS $$\ndef domain(e):\n    return e.split('@')[-1]\n$$ "
          "SELECT id, domain(email) AS d FROM sales.orders WHERE id IN (1, 2) ORDER BY id")
    assert trino(py, user="ana") == [{"id": 1, "d": "•••.com"}, {"id": 2, "d": "•••.org"}]
    assert trino(py) == [{"id": 1, "d": "acme.com"}, {"id": 2, "d": "example.org"}]   # the admin's unmasked rows


def test_delta_lake_tables_are_governed_like_the_lakehouse(trino):
    """Delta Lake in Cairn Store through Trino's delta catalog: tables are created, written and read under
    Cairn's policies (masks, row filters, refusals), and a Delta table another engine wrote is registered."""
    trino("CREATE SCHEMA IF NOT EXISTS delta.dsales")
    trino("CREATE TABLE delta.dsales.orders AS SELECT * FROM lake.sales.orders WHERE id <= 3")
    trino("INSERT INTO delta.dsales.orders VALUES (5, 'kim@acme.com', 'US')")
    assert [r["email"] for r in trino("SELECT email FROM delta.dsales.orders ORDER BY id")] == \
        ["jane@acme.com", "bo@example.org", "li@acme.com", "kim@acme.com"]
    rows = trino("SELECT id, email FROM delta.dsales.orders ORDER BY id", user="ana")
    assert [r["email"] for r in rows] == ["j•••@•••.com", "b•••@•••.org", "l•••@•••.com", "k•••@•••.com"]
    assert [r["id"] for r in trino("SELECT id FROM delta.dsales.orders ORDER BY id", user="bob")] == [1, 3]
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino("INSERT INTO delta.dsales.orders VALUES (6, 'x@y.z', 'EU')", user="ana")
    # Delta's own history and time travel.
    assert len(trino('SELECT version FROM delta.dsales."orders$history"')) >= 2
    assert len(trino("SELECT id FROM delta.dsales.orders FOR VERSION AS OF 0")) == 3
    # A Delta table written elsewhere (here: forgotten by the metastore, its files left in place) is
    # registered by its location and governed the same.
    loc = trino("SELECT \"$path\" AS p FROM delta.dsales.orders LIMIT 1")[0]["p"].rsplit("/", 1)[0]
    trino("CALL delta.system.unregister_table('dsales', 'orders')")
    assert trino("SHOW TABLES FROM delta.dsales") == []
    trino(f"CALL delta.system.register_table('dsales', 'orders', '{loc}')")
    assert [r["email"] for r in trino("SELECT email FROM delta.dsales.orders WHERE id = 5", user="ana")] == ["k•••@•••.com"]


def test_notebooks_query_as_the_person_signed_in(trino, cairn, monkeypatch):
    """%%sql in a notebook (cairn_lakehouse.notebook) runs on Trino as the notebook's Cairn user, with a
    short Trino token from Cairn: ana's cells see her masks, bob's his row filter, neither what is hidden."""
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lakehouse-client", "src"))
    from IPython.core.interactiveshell import InteractiveShell

    import cairn_lakehouse.notebook as nb

    url, s = cairn
    pats = {"pat-nb-ana-" + "a" * 16: "urn:li:corpuser:ana", "pat-nb-bob-" + "b" * 16: "urn:li:corpuser:bob"}
    real = s.identity._me_lookup
    monkeypatch.setattr(s.identity, "_me_lookup", lambda u, h, c: pats.get((h.get("Authorization") or "")[7:]) or real(u, h, c))
    for k, v in {"CAIRN_URL": url, "CAIRN_TRINO_URL": f"https://127.0.0.1:{trino.tls}", "CAIRN_TRINO_CATALOG": "lake",
                 "CAIRN_CA_FILE": f"{trino.dir}/ca.pem"}.items():
        monkeypatch.setenv(k, v)

    def shell_as(pat):
        monkeypatch.setenv("CAIRN_TOKEN", pat)
        nb._session = None
        sh = InteractiveShell.instance()
        sh.run_line_magic("load_ext", "cairn_lakehouse.notebook")
        return sh
    ana, bob = list(pats)
    sh = shell_as(ana)
    assert sh.run_line_magic("cairn", "whoami") == "ana"
    sh.run_cell_magic("sql", "--to rows", "SELECT id, email FROM sales.orders WHERE id <= 3 ORDER BY id")
    got = sh.user_ns["rows"]
    got = got.to_dict("records") if hasattr(got, "to_dict") else got.to_dicts()
    assert [r["email"] for r in got] == ["j•••@•••.com", "b•••@•••.org", "l•••@•••.com"]
    with pytest.raises(Exception, match="Access Denied"):
        sh.run_cell_magic("sql", "", "SELECT * FROM hr.salaries")
    capped = nb.sql("SELECT id FROM sales.orders", limit=1)
    assert len(capped) == 1 and capped.truncated and "<table>" in capped._repr_html_()
    sh = shell_as(bob)
    sh.run_cell_magic("sql", "--to rows", "SELECT id FROM sales.orders WHERE id <= 3 ORDER BY id")
    got = sh.user_ns["rows"]
    got = got.to_dict("records") if hasattr(got, "to_dict") else got.to_dicts()
    assert [r["id"] for r in got] == [1, 3]                       # his row filter
    # Not signed in, or a token Cairn does not know: no Trino token, no query.
    monkeypatch.setenv("CAIRN_TOKEN", "not-a-token")
    nb._session = None
    with pytest.raises(nb.CairnLakehouseError, match="refused a Trino token"):
        nb.sql("SELECT 1")


def test_delta_tables_take_writes_from_several_clusters_at_once(trino, lake):
    """Two Trino clusters write one Delta table at the same time. Each commit is a put-if-absent of the next
    log entry in Cairn Store (If-None-Match), so none is lost and none overwrites another."""
    from concurrent.futures import ThreadPoolExecutor

    trino("CREATE SCHEMA IF NOT EXISTS delta.dconc")
    trino("CREATE TABLE delta.dconc.events (id bigint, src varchar)")
    d = tempfile.mkdtemp(prefix="trino2-")
    os.makedirs(f"{d}/catalog")
    tp = port()
    open(f"{d}/catalog/delta.properties", "w").write("\n".join([
        "connector.name=delta_lake", "hive.metastore=file", "hive.metastore.catalog.dir=s3://lakehouse/delta",
        S3_ON, f"s3.endpoint={lake['store']}", "s3.region=us-east-1", "s3.path-style-access=true",
        f"s3.aws-access-key={STORE_KEY[0]}", f"s3.aws-secret-key={STORE_KEY[1]}", DELTA_CONDITIONAL, ""]))
    open(f"{d}/config.properties", "w").write("\n".join([
        "coordinator=true", "node-scheduler.include-coordinator=true", f"http-server.http.port={tp}",
        f"discovery.uri=http://127.0.0.1:{tp}", ""]))
    os.chmod(d, 0o755)
    other = f"cairn-trino-delta2-{os.getpid()}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", other, "--network", "host",
                    "-v", f"{d}/catalog:/etc/trino/catalog:ro", "-v", f"{d}/config.properties:/etc/trino/config.properties:ro",
                    TRINO], check=True, capture_output=True, timeout=600)
    try:
        end = time.time() + 240
        while "SERVER STARTED" not in subprocess.run(["docker", "logs", other], capture_output=True, text=True).stderr:
            assert time.time() < end, "the second Trino did not start"
            time.sleep(1)

        def second(statement):
            out = subprocess.run(["docker", "exec", other, "trino", "--server", f"http://127.0.0.1:{tp}", "--user", "writer",
                                  "--execute", statement], capture_output=True, text=True, timeout=300)
            assert out.returncode == 0, out.stderr[-2000:]

        jobs = [(lambda i=i: trino(f"INSERT INTO delta.dconc.events VALUES ({i}, 'a')")) for i in range(6)]
        jobs += [(lambda i=i: second(f"INSERT INTO delta.dconc.events VALUES ({100 + i}, 'b')")) for i in range(6)]
        with ThreadPoolExecutor(12) as pool:
            for f in [pool.submit(j) for j in jobs]:
                f.result()
        rows = trino("SELECT id, src FROM delta.dconc.events ORDER BY id")
        assert [r["id"] for r in rows] == list(range(6)) + list(range(100, 106))
        versions = sorted(r["version"] for r in trino('SELECT version FROM delta.dconc."events$history"'))
        assert versions == list(range(13)), versions                  # the table's creation, then one per insert
    finally:
        subprocess.run(["docker", "rm", "-f", other], capture_output=True)


def test_a_delta_table_is_converted_to_iceberg_in_place(trino, lake):
    """A Delta table Trino wrote (with a checkpoint, an update and a delete) becomes an Iceberg table at the
    same location, its live files added without copying; what Iceberg cannot read the same way is refused."""
    from cairn.pipelines.delta_convert import DeltaConversions
    from cairn.pipelines.pipes import PipeError

    conv = DeltaConversions({"endpoint": lake["store"], "access_key": STORE_KEY[0], "secret_key": STORE_KEY[1]},
                            {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                             "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1], "s3.region": "us-east-1",
                             "s3.path-style-access": "true"})
    trino("CREATE SCHEMA IF NOT EXISTS delta.dconv")
    trino("CREATE TABLE delta.dconv.items WITH (checkpoint_interval = 2) AS "
          "SELECT * FROM (VALUES (1, 'a'), (2, 'b'), (3, 'c')) t(id, name)")
    trino("INSERT INTO delta.dconv.items VALUES (4, 'd'), (6, 'f')")
    trino("UPDATE delta.dconv.items SET name = 'B' WHERE id = 2")
    trino("DELETE FROM delta.dconv.items WHERE id = 6")
    loc = trino("SELECT \"$path\" AS p FROM delta.dconv.items LIMIT 1")[0]["p"].rsplit("/", 1)[0]
    seen = conv.inspect(loc)
    assert seen["problems"] == [] and seen["version"] == 3 and seen["files"] >= 2, seen
    with pytest.raises(PipeError, match="namespace.table"):
        conv.convert(loc, "nope", "urn:li:corpuser:admin")
    trino("CALL delta.system.unregister_table('dconv', 'items')")           # retired: nothing writes to it now
    out = conv.convert(loc, "converted.items", "urn:li:corpuser:admin")
    assert out["delta_version"] == 3 and out["location"] == loc
    rows = trino("SELECT id, name FROM lake.converted.items ORDER BY id")
    assert [(r["id"], r["name"]) for r in rows] == [(1, "a"), (2, "B"), (3, "c"), (4, "d")]
    props = conv._catalog().load_table("converted.items").properties
    assert props["cairn.converted-from"] == loc and props["cairn.delta-version"] == "3"
    # It is an ordinary lakehouse table now: written and read like any other.
    trino("INSERT INTO lake.converted.items VALUES (5, 'e')")
    assert trino("SELECT count(*) AS n FROM lake.converted.items")[0]["n"] == 5
    with pytest.raises(PipeError, match="exists"):
        conv.convert(loc, "converted.items", "urn:li:corpuser:admin")
    # Partitioned tables keep values in paths, not files: refused (copy them with a derived table).
    trino("CREATE TABLE delta.dconv.parts (id bigint, region varchar) WITH (partitioned_by = ARRAY['region'])")
    trino("INSERT INTO delta.dconv.parts VALUES (1, 'EU')")
    ploc = trino("SELECT \"$path\" AS p FROM delta.dconv.parts LIMIT 1")[0]["p"].rsplit("/", 2)[0]
    with pytest.raises(PipeError, match="partitioned by region"):
        conv.convert(ploc, "converted.parts", "urn:li:corpuser:admin")
    with pytest.raises(PipeError, match="no Delta log"):
        conv.inspect(loc + "-missing")


def test_stored_functions_and_procedures(trino, cairn):
    """Stored SQL and Python functions, called under Cairn's policies (USE_FUNCTION per function, masks
    first); procedures run their statements as the caller, with typed, bound arguments."""
    from cairn.governance.abac import Policy, PolicyBundle, Target
    from cairn.quality.collectors import ConnectionRegistry
    from cairn.sqlworkspace.workspace import WorkspaceError

    _, s = cairn
    trino("CREATE SCHEMA IF NOT EXISTS routines.shared")
    trino("CREATE FUNCTION routines.shared.domain_of(e varchar) RETURNS varchar RETURN regexp_extract(e, '@(.*)$', 1)")
    trino("CREATE FUNCTION routines.shared.shout(x varchar) RETURNS varchar LANGUAGE PYTHON WITH (handler = 'shout') "
          "AS $$\ndef shout(x):\n    return x.upper() + '!'\n$$")
    assert trino("SELECT routines.shared.domain_of('kim@acme.com') AS d, routines.shared.shout('hi') AS s") == \
        [{"d": "acme.com", "s": "HI!"}]
    # Ana may not call them until Cairn allows her, function by function; she never creates one.
    q = "SELECT routines.shared.domain_of(email) AS d FROM sales.orders WHERE id = 1"
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino(q, user="ana")
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino("CREATE FUNCTION routines.shared.mine(x bigint) RETURNS bigint RETURN x", user="ana")
    b = s.pdp.bundle() if callable(s.pdp.bundle) else s.pdp.bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*b.policies, Policy(
        id="ana-domain", effect="allow", actions=["USE_FUNCTION"],
        target=Target(resource_urns=["urn:li:function:routines.shared.domain_of"]),
        condition={"attr": "subject.urn", "op": "eq", "value": "urn:li:corpuser:ana"})]))
    try:
        assert trino(q, user="ana") == [{"d": "•••.com"}]            # the mask applies before the function
        assert trino(q) == [{"d": "acme.com"}]
        with pytest.raises(RuntimeError, match="Access Denied"):
            trino("SELECT routines.shared.shout('x') AS s", user="ana")   # only the one she was given
    finally:
        s.pdp.set_bundle(b)
    # Procedures: statements with typed arguments, run as the caller on Trino.
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    procs = s.procedures
    admin = "urn:li:corpuser:admin"
    trino("CREATE SCHEMA IF NOT EXISTS lake.ops")
    trino("CREATE TABLE IF NOT EXISTS lake.ops.adjustments (region varchar, amount double, day date)")
    procs.create(admin, {"name": "adjust_region", "connection": "lakehouse",
                         "params": [{"name": "region", "type": "varchar"}, {"name": "amount", "type": "double"},
                                    {"name": "day", "type": "date"}],
                         "statements": ["DELETE FROM lake.ops.adjustments WHERE region = :region AND day = :day",
                                        "INSERT INTO lake.ops.adjustments VALUES (:region, :amount, :day)",
                                        "SELECT region, sum(amount) AS total FROM lake.ops.adjustments GROUP BY region"]})
    out = procs.call(admin, "adjust_region", {"region": "EU", "amount": 5, "day": "2026-11-01"})
    assert out["ran"] == 3 and out["rows"] == [["EU", 5.0]], out
    procs.call(admin, "adjust_region", {"region": "EU", "amount": 7.5, "day": "2026-11-01"})   # replaced, not added
    hostile = "x'); DROP TABLE lake.ops.adjustments; --"
    out = procs.call(admin, "adjust_region", {"region": hostile, "amount": 1, "day": "2026-11-02"})
    assert sorted(map(tuple, out["rows"])) == [("EU", 7.5), (hostile, 1.0)]  # a value, never SQL
    with pytest.raises(WorkspaceError, match="day: a date"):
        procs.call(admin, "adjust_region", {"region": "EU", "amount": 1, "day": "yesterday"})
    with pytest.raises(WorkspaceError, match="undeclared parameter :nope"):
        procs.create(admin, {"name": "bad", "connection": "lakehouse", "statements": ["SELECT :nope"]})
    # Ana needs EXECUTE_PROCEDURE; given it, each statement still runs as her (she may not write the table).
    with pytest.raises(WorkspaceError, match="needs EXECUTE_PROCEDURE"):
        procs.call("urn:li:corpuser:ana", "adjust_region", {"region": "US", "amount": 1, "day": "2026-11-01"})
    s.pdp.set_bundle(PolicyBundle(policies=[*b.policies, Policy(
        id="ana-proc", effect="allow", actions=["EXECUTE_PROCEDURE"],
        target=Target(resource_urns=["urn:li:procedure:adjust_region"]),
        condition={"attr": "subject.urn", "op": "eq", "value": "urn:li:corpuser:ana"})]))
    try:
        with pytest.raises(WorkspaceError, match="statement 1"):
            procs.call("urn:li:corpuser:ana", "adjust_region", {"region": "US", "amount": 1, "day": "2026-11-01"})
    finally:
        s.pdp.set_bundle(b)
    assert trino("SELECT count(*) AS n FROM lake.ops.adjustments WHERE region = 'US'") == [{"n": 0}]
    ev = [e for e in s.audit.entries("sql", limit=50) if e["action"] == "procedure.called"]
    assert ev and ev[0]["details"]["args"]["region"] in ("EU", hostile, "US")


def test_a_tables_change_feed_is_read_as_the_caller(trino, cairn):
    """A table's row changes between snapshots (inserts, deletes, and updates by key), read as whoever asks:
    their masks and row filters apply to the feed as to any read; Trino's own table_changes stays refused."""
    from cairn.pipelines.change_feed import ChangeFeed, ChangeFeedError
    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    definition = s.data_access.registry.definitions()["lakehouse"]
    feed = ChangeFeed(lambda actor, sql, limit: s.data_access.run_on_trino(definition, actor, sql, limit), "lake")
    admin, ana, bob = ("urn:li:corpuser:" + u for u in ("admin", "ana", "bob"))
    urn = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.cdf_orders,PROD)"
    s.graph.aspects[(urn, "schemaMetadata")] = {"fields": [
        {"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}}, {"fieldPath": "region"}]}
    trino("CREATE TABLE lake.sales.cdf_orders (id integer, email varchar, region varchar)")
    trino("INSERT INTO lake.sales.cdf_orders VALUES (1, 'kim@acme.com', 'EU'), (2, 'lee@acme.com', 'US')")
    first = feed.snapshots(admin, "sales", "cdf_orders")[-1]["snapshot"]
    trino("INSERT INTO lake.sales.cdf_orders VALUES (3, 'max@acme.com', 'EU'), (4, 'ola@acme.com', 'US')")
    trino("DELETE FROM lake.sales.cdf_orders WHERE id = 2")
    trino("UPDATE lake.sales.cdf_orders SET region = 'APAC' WHERE id = 1")
    snaps = feed.snapshots(admin, "sales", "cdf_orders")
    assert len(snaps) >= 4 and snaps[-1]["snapshot"] != first

    out = feed.changes(admin, "sales", "cdf_orders", str(first), key=["id"])
    got = [(c["_change_type"], c["id"], c["region"], c["email"]) for c in out["changes"]]
    assert got == [("update_preimage", 1, "EU", "kim@acme.com"), ("update_postimage", 1, "APAC", "kim@acme.com"),
                   ("delete", 2, "US", "lee@acme.com"), ("insert", 3, "EU", "max@acme.com"),
                   ("insert", 4, "US", "ola@acme.com")], got
    assert out["since_snapshot"] == first and out["until_snapshot"] == snaps[-1]["snapshot"]
    # Without a key, an update is a delete and an insert. Nothing changes after the last snapshot.
    plain = feed.changes(admin, "sales", "cdf_orders", str(first))
    assert sorted(c["_change_type"] for c in plain["changes"]) == ["delete", "delete", "insert", "insert", "insert"]
    assert feed.changes(admin, "sales", "cdf_orders", str(out["until_snapshot"]))["changes"] == []
    # since as a time: the last snapshot committed at or before it.
    at = next(s for s in snaps if s["snapshot"] == first)["committed_at"]
    assert feed.changes(admin, "sales", "cdf_orders", at, key=["id"])["since_snapshot"] == first
    with pytest.raises(ChangeFeedError, match="no snapshot at or before"):
        feed.changes(admin, "sales", "cdf_orders", "2000-01-01T00:00:00+00:00")

    # Ana's feed is masked, as her reads are; Bob's holds only the EU rows he may see.
    mine = feed.changes(ana, "sales", "cdf_orders", str(first), key=["id"])
    assert all("•" in c["email"] and "acme" not in c["email"] for c in mine["changes"]), mine["changes"]
    assert len(mine["changes"]) == 5
    bobs = feed.changes(bob, "sales", "cdf_orders", str(first), key=["id"])
    # At each snapshot he sees EU rows only: id 1 left EU (a delete for him), id 3 arrived.
    assert sorted((c["_change_type"], c["id"]) for c in bobs["changes"]) == [("delete", 1), ("insert", 3)], bobs["changes"]

    # The feed's errors.
    with pytest.raises(ChangeFeedError, match="no snapshot"):
        feed.changes(admin, "sales", "cdf_orders", "123")
    with pytest.raises(ChangeFeedError, match="key"):
        feed.changes(admin, "sales", "cdf_orders", str(first), key=["nope"])
    # A table she may not read: refused by Trino under Cairn's policy, before any row is compared.
    trino("CREATE SCHEMA IF NOT EXISTS lake.hr")
    with pytest.raises(Exception, match="Access Denied"):
        feed.changes(ana, "hr", "salaries", "1")
    # Iceberg's value-bearing metadata (file bounds) is the table managers' only; snapshots are readers'.
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino('SELECT lower_bounds FROM lake.sales."cdf_orders$files"', user="ana")
    assert trino('SELECT count(*) AS n FROM lake.sales."cdf_orders$snapshots"', user="ana")[0]["n"] >= 4
    # Trino's table_changes would return raw rows past masks: refused (on an append-only table, where it runs).
    trino("CREATE TABLE lake.sales.cdf_appends AS SELECT * FROM lake.sales.cdf_orders")
    trino("INSERT INTO lake.sales.cdf_appends VALUES (9, 'zed@acme.com', 'EU')")
    apps = [x["snapshot"] for x in feed.snapshots(admin, "sales", "cdf_appends")]
    q = f"SELECT email FROM TABLE(lake.system.table_changes('sales', 'cdf_appends', {apps[0]}, {apps[-1]}))"
    assert trino(q) == [{"email": "zed@acme.com"}]                    # it works, for the admin
    with pytest.raises(RuntimeError, match="Access Denied"):
        trino(q, user="ana")


def test_materialized_views_in_sql_refresh_when_what_they_read_changes(trino, cairn):
    """CREATE / REFRESH / DROP MATERIALIZED VIEW in the SQL editor (Trino has none on a REST catalog): the view is
    filled at once, says when it is stale, refreshes when a table it reads changes, and is its owner's."""
    from cairn.pipelines.changes import DatasetChanges
    from cairn.pipelines.derived import DerivedTables
    from cairn.pipelines.matviews import MaterializedViews
    from cairn.quality.collectors import ConnectionRegistry
    from cairn.sqlworkspace.workspace import WorkspaceError

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    definition = s.data_access.registry.definitions()["lakehouse"]

    def run(owner, sql):
        return s.data_access.run_on_trino(definition, owner, sql, 100_000)[1]
    admin, ana = "urn:li:corpuser:admin", "urn:li:corpuser:ana"
    changes = DatasetChanges(s.engine)
    dt = DerivedTables(s.engine, run, "lake", s.audit, changes, urn=lambda ns, t: s.lakehouse.urn([ns], t))
    s.sql_workspace.matviews = MaterializedViews(dt, "lake", lambda actor: actor == admin)
    for stmt in ["CREATE SCHEMA IF NOT EXISTS lake.mv",
                 "CREATE TABLE lake.mv.sales (region varchar, amount double)",
                 "INSERT INTO lake.mv.sales VALUES ('EU', 10.0), ('US', 5.0), ('EU', 2.5)"]:
        run(admin, stmt)
    ws = s.sql_workspace
    out = ws.run(admin, "CREATE MATERIALIZED VIEW lake.mv.by_region AS "
                        "SELECT region, sum(amount) AS total FROM lake.mv.sales GROUP BY region", "lakehouse")
    assert "created" in out["rows"][0][0] and "when a table it reads changes" in out["rows"][0][0]
    assert {r["region"]: r["total"] for r in trino("SELECT * FROM lake.mv.by_region")} == {"EU": 12.5, "US": 5.0}
    [mv] = [d for d in dt.list() if d["kind"] == "materialized_view"]
    assert mv["owner"] == admin and mv["on_change"] and dt.freshness(mv)["fresh"] is True
    # The table it reads changes; the catalog reports it (as Cairn Catalog does, POST /v1/lakehouse/changes).
    run(admin, "INSERT INTO lake.mv.sales VALUES ('US', 4.0)")
    r = httpx.post(f"{url}/v1/lakehouse/changes", json={"tables": [{"namespace": ["mv"], "name": "sales"}]},
                   headers={"Authorization": "Bearer svc-secret"}, timeout=10)
    assert r.status_code == 200, r.text
    stale = dt.freshness(dt.get(mv["id"]))
    assert stale["fresh"] is False and stale["changed"] == [s.lakehouse.urn(["mv"], "sales")]
    assert dt.run_due() >= 1                                          # the scheduler's tick refreshes it
    assert {r["region"]: r["total"] for r in trino("SELECT * FROM lake.mv.by_region")} == {"EU": 12.5, "US": 9.0}
    assert dt.freshness(dt.get(mv["id"]))["fresh"] is True
    runs = len(dt.runs(mv["id"]))
    dt.run_due()
    assert len(dt.runs(mv["id"])) == runs                             # nothing it reads changed since: not refreshed
    # REFRESH on demand; another person may neither refresh nor drop it.
    assert "refreshed" in ws.run(admin, "REFRESH MATERIALIZED VIEW mv.by_region", "lakehouse")["rows"][0][0]
    for stmt in ("REFRESH MATERIALIZED VIEW lake.mv.by_region", "DROP MATERIALIZED VIEW lake.mv.by_region"):
        with pytest.raises(WorkspaceError, match="no materialized view"):
            ws.run(ana, stmt, "lakehouse")
    # A view whose first fill fails is not kept; its owner's access applies (ana may not read hr.salaries).
    with pytest.raises(WorkspaceError, match="could not be filled"):
        ws.run(ana, "CREATE MATERIALIZED VIEW lake.mv.pay AS SELECT * FROM lake.hr.salaries", "lakehouse")
    assert not [d for d in dt.list() if d["target"] == "mv.pay"]
    again = ws.run(admin, "CREATE MATERIALIZED VIEW IF NOT EXISTS lake.mv.by_region AS SELECT 1 AS x", "lakehouse")
    assert again["rows"][0][0].endswith("exists")                     # IF NOT EXISTS: left as it is
    # DROP removes the definition and the table.
    assert "dropped" in ws.run(admin, "DROP MATERIALIZED VIEW lake.mv.by_region", "lakehouse")["rows"][0][0]
    assert not [d for d in dt.list() if d["kind"] == "materialized_view"]
    assert trino("SELECT count(*) AS n FROM lake.information_schema.tables WHERE table_schema = 'mv' "
                 "AND table_name = 'by_region'") == [{"n": 0}]
    assert ws.run(admin, "DROP MATERIALIZED VIEW IF EXISTS lake.mv.by_region", "lakehouse")["rows"] == [["nothing to drop"]]
    s.sql_workspace.matviews = None


def test_stored_functions_are_catalog_assets(trino, cairn):
    """Stored functions in the catalog, as Trino tells Cairn of them: created (owned by their creator, with
    signature, body and comment), replaced, overloaded and dropped; the sync adds what Cairn was not told."""
    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    fc = s.trino_functions
    urn = fc.asset_urn("routines", "catalog_e2e", "net_price")
    trino("CREATE SCHEMA IF NOT EXISTS routines.catalog_e2e")
    trino("CREATE FUNCTION routines.catalog_e2e.net_price(gross double, vat double) RETURNS double "
          "COMMENT 'Price without VAT' RETURN gross / (1 + vat)")

    def info():
        return s.graph.get_aspect(urn, "dataJobInfo") or {}
    end = time.time() + 30
    while time.time() < end and not info():
        time.sleep(0.5)
    props = info().get("customProperties") or {}
    assert info().get("description") == "Price without VAT", info()
    assert props["function_urn"] == "urn:li:function:routines.catalog_e2e.net_price"
    assert props["body"] == "RETURN gross / (1 + vat)" and props["language"] == "SQL"
    assert [o["owner"] for o in s.graph.get_aspect(urn, "ownership")["owners"]] == ["urn:li:corpuser:admin"]
    # An overload: one asset, both signatures. Dropping one keeps the other; dropping both removes it.
    trino("CREATE FUNCTION routines.catalog_e2e.net_price(gross double) RETURNS double RETURN gross / 1.2")
    end = time.time() + 30
    while time.time() < end and len(json.loads((info().get("customProperties") or {}).get("signatures", "[]"))) < 2:
        time.sleep(0.5)
    assert sorted(x["types"] for x in json.loads(info()["customProperties"]["signatures"])) == ["double", "double, double"]
    trino("DROP FUNCTION routines.catalog_e2e.net_price(double)")
    end = time.time() + 30
    while time.time() < end and len(json.loads(info()["customProperties"]["signatures"])) > 1:
        time.sleep(0.5)
    assert [x["types"] for x in json.loads(info()["customProperties"]["signatures"])] == ["double, double"]
    # A function Cairn was never told of (made while it was away) comes in with the sync, as Trino lists it.
    fc.catalogs.discard("routines")
    try:
        trino("CREATE FUNCTION routines.catalog_e2e.missed(x bigint) RETURNS bigint RETURN x + 1")
        time.sleep(2)
    finally:
        fc.catalogs.add("routines")
    missed = fc.asset_urn("routines", "catalog_e2e", "missed")
    assert s.graph.get_aspect(missed, "dataJobInfo") is None
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    r = httpx.post(f"{url}/v1/trino/functions/sync", json={"connection": "lakehouse"},
                   headers={"X-Cairn-Actor": "urn:li:corpuser:ana"}, timeout=60)
    assert r.status_code == 403                                     # syncing needs MANAGE_INGESTION
    r = httpx.post(f"{url}/v1/trino/functions/sync", json={"connection": "lakehouse"},
                   headers={"X-Cairn-Actor": "urn:li:corpuser:admin"}, timeout=60)
    assert r.status_code == 200 and r.json()["added"] >= 1, r.text
    got = json.loads(s.graph.get_aspect(missed, "dataJobInfo")["customProperties"]["signatures"])
    assert got == [{"arguments": "bigint", "types": "bigint", "returns": "bigint", "language": "SQL"}]
    # Dropped in Trino: removed from the catalog.
    trino("DROP FUNCTION routines.catalog_e2e.net_price(double, double)")
    trino("DROP FUNCTION routines.catalog_e2e.missed(bigint)")
    end = time.time() + 30
    while time.time() < end and not (s.graph.get_aspect(urn, "status") or {}).get("removed"):
        time.sleep(0.5)
    assert s.graph.get_aspect(urn, "status") == {"removed": True}
    assert s.graph.get_aspect(missed, "status") == {"removed": True}


def test_clean_rooms_release_only_approved_aggregates(trino, cairn):
    """Two parties join their tables in a clean room on real Trino: only templates both approved run, only
    aggregates over at least k people leave, the room reads its tables and nothing else, and neither party
    reads the other's table."""
    from cairn.common.security import AuthenticationError
    from cairn.dataaccess.cleanrooms import RoomError
    from cairn.governance.abac import Policy, PolicyBundle, Target
    from cairn.quality.collectors import ConnectionRegistry

    _, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": ""}}})
    for stmt in ["CREATE SCHEMA IF NOT EXISTS lake.retail", "CREATE SCHEMA IF NOT EXISTS lake.ads",
                 "CREATE TABLE lake.retail.buyers (email varchar, region varchar, spend double)",
                 "INSERT INTO lake.retail.buyers VALUES ('a@x', 'EU', 10), ('b@x', 'EU', 20), ('c@x', 'EU', 30), "
                 "('d@x', 'US', 40), ('e@x', 'US', 50), ('f@x', 'APAC', 60)",
                 "CREATE TABLE lake.ads.clicks (email varchar, campaign varchar)",
                 "INSERT INTO lake.ads.clicks VALUES ('a@x', 'spring'), ('b@x', 'spring'), ('c@x', 'spring'), "
                 "('d@x', 'spring'), ('e@x', 'spring'), ('f@x', 'spring'), ('z@x', 'spring')"]:
        trino(stmt)
    b = s.pdp.bundle() if callable(s.pdp.bundle) else s.pdp.bundle
    reads = lambda pid, pattern, user: Policy(  # noqa: E731
        id=pid, effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"], target=Target(resource_urns=[pattern]),
        condition={"attr": "subject.urn", "op": "eq", "value": f"urn:li:corpuser:{user}"})
    s.pdp.set_bundle(PolicyBundle(policies=[*b.policies, reads("ana-retail", "*retail.*", "ana"), reads("bob-ads", "*ads.*", "bob")]))
    ana, bob, eve = "urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:eve"
    cr = s.clean_rooms
    try:
        cr.create(ana, {"name": "reach", "parties": [ana, bob], "min_group": 2})
        cr.contribute(ana, "reach", "lake.retail.buyers")
        with pytest.raises(RoomError, match="may not read"):
            cr.contribute(bob, "reach", "lake.retail.buyers")         # only what you may read yourself
        cr.contribute(bob, "reach", "lake.ads.clicks")
        sql = ("SELECT b.region, c.campaign, count(*) AS people, sum(b.spend) AS spend FROM lake.retail.buyers b "
               "JOIN lake.ads.clicks c ON b.email = c.email WHERE c.campaign = :campaign GROUP BY b.region, c.campaign")
        cr.propose(ana, "reach", {"name": "by_region", "sql": sql, "params": [{"name": "campaign", "type": "varchar"}]})
        with pytest.raises(RoomError, match="waits for approval by urn:li:corpuser:bob"):
            cr.run_template(ana, "reach", "by_region", {"campaign": "spring"})
        cr.approve(bob, "reach", "by_region")
        out = cr.run_template(bob, "reach", "by_region", {"campaign": "spring"})
        got = {r[0]: (r[2], r[3]) for r in out["rows"]}
        assert got == {"EU": (3, 60.0), "US": (2, 90.0)}, out           # APAC is one person: never released
        # Changing a template's text clears the approvals; rows, SELECT * and other tables are refused.
        cr.propose(ana, "reach", {"name": "by_region", "sql": sql.replace("sum(b.spend)", "max(b.spend)"),
                                  "params": [{"name": "campaign", "type": "varchar"}]})
        with pytest.raises(RoomError, match="waits for approval"):
            cr.run_template(ana, "reach", "by_region", {"campaign": "spring"})
        for bad, msg in [("SELECT email FROM lake.ads.clicks", "aggregate"), ("SELECT * FROM lake.ads.clicks", "SELECT \\*"),
                         ("SELECT count(*) FROM lake.sales.orders", "not in the room")]:
            with pytest.raises(RoomError, match=msg):
                cr.propose(ana, "reach", {"name": "bad", "sql": bad})
        with pytest.raises(RoomError, match="not a party"):
            cr.run_template(eve, "reach", "by_region", {"campaign": "spring"})
        # Neither party reads the other's table; the room's identity reads its tables and nothing else.
        with pytest.raises(RuntimeError, match="Access Denied"):
            trino("SELECT count(*) AS n FROM lake.ads.clicks", user="ana")
        with pytest.raises(RuntimeError, match="Access Denied"):
            trino("SELECT count(*) AS n FROM lake.sales.orders", user="cleanroom-reach")
        assert trino("SELECT count(*) AS n FROM lake.ads.clicks", user="cleanroom-reach") == [{"n": 7}]
        # Nobody signs in as a room, whatever their identity provider calls them.
        s.identity.dev_trust_actor_header = True
        with pytest.raises(AuthenticationError, match="reserved"):
            s.identity.resolve({"x-cairn-actor": "urn:li:corpuser:cleanroom-reach"}, {})
        assert any(e["action"] == "clean_room.run" for e in s.audit.entries("sharing", limit=50))
    finally:
        s.pdp.set_bundle(b)


def test_bi_sharing_with_people_and_groups_keeps_each_viewers_data(trino, cairn):
    """Saved queries, workbooks and dashboards shared with a person or a group, as a viewer or an editor, on
    real Trino: nobody else finds them, editors change but do not reshare or delete, and every viewer still sees
    only their own rows (Bob's row filter) through a dashboard built on the owner's private query."""
    import httpx

    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    trino("CREATE TABLE lake.sales.sh_orders AS SELECT * FROM (VALUES (1, 'EU', 10e0), (2, 'US', 20e0), (3, 'EU', 5e0)) "
          "AS t(id, region, amount)")
    s.graph.aspects[("urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.sh_orders,PROD)", "schemaMetadata")] = {
        "fields": [{"fieldPath": c} for c in ("id", "region", "amount")]}
    team = "urn:li:corpGroup:sharing-analysts"
    s.graph.aspects[("urn:li:corpuser:carol", "groupMembership")] = {"groups": [team]}
    s.attributes.invalidate("urn:li:corpuser:carol")
    admin, ana, bob, carol = ({"X-Cairn-Actor": f"urn:li:corpuser:{u}"} for u in ("admin", "ana", "bob", "carol"))
    c = httpx.Client(base_url=url, timeout=120)

    q = c.post("/sql/saved", headers=admin, json={"name": "sh orders", "connection": "lakehouse",
                                                  "sql": "SELECT id, region, amount FROM sales.sh_orders ORDER BY id"})
    assert q.status_code in (200, 201), q.text
    qid = q.json()["id"]
    d = c.post("/sql/dashboards", headers=admin, json={"name": "Shared orders", "tiles": [{"query": qid, "title": "Orders"}]})
    assert d.status_code == 201, d.text
    did = d.json()["id"]
    assert d.json()["role"] == "OWNER"

    # Not shared yet: as if it did not exist.
    for h in (bob, carol):
        assert c.get(f"/sql/dashboards/{did}", headers=h).status_code == 404
        assert did not in [x["id"] for x in c.get("/sql/dashboards", headers=h).json()]
    assert c.get(f"/sql/shares/dashboard/{did}", headers=bob).status_code == 404

    # Bob as a viewer: he opens it and sees only his rows, though the query under it stays the owner's.
    r = c.put(f"/sql/shares/dashboard/{did}", headers=admin, json={"principal": "urn:li:corpuser:bob", "role": "VIEWER"})
    assert r.status_code == 200 and r.json()["shares"][0]["role"] == "VIEWER", r.text
    assert c.get(f"/sql/dashboards/{did}", headers=bob).json()["role"] == "VIEWER"
    told = [n for n in s.notify.inbox("urn:li:corpuser:bob") if n["type"] == "bi.shared"]
    assert told and "Shared orders" in json.dumps(told), told                # Bob is told it is shared with him
    assert did in [x["id"] for x in c.get("/sql/dashboards", headers=bob).json()]
    tiles = c.post(f"/sql/dashboards/{did}/render", headers=bob, json={}).json()["tiles"]
    assert {row[1] for row in tiles[0]["result"]["rows"]} == {"EU"}, tiles[0]
    full = c.post(f"/sql/dashboards/{did}/render", headers=admin, json={}).json()["tiles"]
    assert [row[0] for row in full[0]["result"]["rows"]] == [1, 2, 3]
    assert c.get(f"/sql/saved/{qid}", headers=bob).status_code == 404          # the query itself is not shared
    assert c.put(f"/sql/dashboards/{did}", headers=bob, json={"name": "mine", "tiles": [{"query": qid}]}).status_code == 403
    assert c.put(f"/sql/shares/dashboard/{did}", headers=bob,
                 json={"principal": "urn:li:corpuser:ana", "role": "VIEWER"}).status_code == 403

    # A group as editor: Carol (a member) renames it, keeping the owner's tile, but cannot make it public,
    # reshare it or delete it.
    c.put(f"/sql/shares/dashboard/{did}", headers=admin, json={"principal": team, "role": "EDITOR"})
    r = c.put(f"/sql/dashboards/{did}", headers=carol, json={"name": "Orders (renamed)", "shared": True,
                                                            "tiles": [{"query": qid, "title": "Orders"}]})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Orders (renamed)" and r.json()["shared"] is False and r.json()["owner"].endswith(":admin")
    assert c.get(f"/sql/shares/dashboard/{did}", headers=carol).status_code == 200      # editors see who has it
    assert c.put(f"/sql/shares/dashboard/{did}", headers=carol,
                 json={"principal": "urn:li:corpuser:ana", "role": "EDITOR"}).status_code == 403
    assert c.delete(f"/sql/dashboards/{did}", headers=carol).status_code == 404
    # A new tile on a query Carol may not open is refused.
    private = c.post("/sql/saved", headers=admin, json={"name": "private", "connection": "lakehouse",
                                                        "sql": "SELECT 1 AS one"}).json()["id"]
    assert c.put(f"/sql/dashboards/{did}", headers=carol, json={
        "name": "x", "tiles": [{"query": qid}, {"query": private}]}).status_code == 404

    # Saved queries and workbooks share the same way.
    c.put(f"/sql/shares/query/{qid}", headers=admin, json={"principal": "urn:li:corpuser:ana", "role": "VIEWER"})
    assert c.get(f"/sql/saved/{qid}", headers=ana).json()["role"] == "VIEWER"
    assert qid in [x["id"] for x in c.get("/sql/saved", headers=ana).json()]
    wb = c.post("/sql/workbooks", headers=admin, json={"name": "sh wb", "connection": "lakehouse", "spec": {
        "elements": [{"source": {"table": "sales.sh_orders"}}]}})
    assert wb.status_code in (200, 201), wb.text
    wid = wb.json()["id"]
    assert c.get(f"/sql/workbooks/{wid}", headers=bob).status_code == 404
    c.put(f"/sql/shares/workbook/{wid}", headers=admin, json={"principal": "urn:li:corpuser:bob", "role": "EDITOR"})
    r = c.put(f"/sql/workbooks/{wid}", headers=bob, json={"name": "sh wb 2", "connection": "lakehouse", "spec": {
        "elements": [{"source": {"table": "sales.sh_orders"}}]}})
    assert r.status_code == 200 and r.json()["role"] == "EDITOR", r.text

    # Taking it away, and deleting, end access; the audit says who shared what.
    c.put(f"/sql/shares/dashboard/{did}", headers=admin, json={"principal": "urn:li:corpuser:bob", "role": None})
    assert c.get(f"/sql/dashboards/{did}", headers=bob).status_code == 404
    assert c.delete(f"/sql/dashboards/{did}", headers=admin).status_code == 204
    assert c.get(f"/sql/shares/dashboard/{did}", headers=admin).status_code == 404
    from cairn.sqlworkspace.sharing import bi_shares

    with s.engine.connect() as conn:
        assert not conn.execute(bi_shares.select().where(bi_shares.c.asset_id == did)).all()
    acts = [e["action"] for e in s.audit.entries(limit=2000)]
    assert "bi.shared" in acts and "bi.unshared" in acts
    # Bad requests.
    assert c.put(f"/sql/shares/nope/{qid}", headers=admin, json={"principal": team, "role": "VIEWER"}).status_code == 404
    assert c.put(f"/sql/shares/query/{qid}", headers=admin, json={"principal": "bob", "role": "VIEWER"}).status_code == 400
    assert c.put(f"/sql/shares/query/{qid}", headers=admin,
                 json={"principal": "urn:li:corpuser:bob", "role": "OWNER"}).status_code == 400


def test_legal_hold_retention_that_deletes_and_erasure_with_signed_proof(trino, cairn):
    """Records management on real Trino: a retention policy that deletes the rows older than its limit; a legal
    hold that keeps a table's rows from everyone (retention, erasure and a direct DELETE alike) until it is
    released; and an erasure request that deletes one person's rows, checks none remain, expires the history and
    ends with a report signed by Cairn that anyone can check."""
    import base64

    import httpx

    from cairn.governance.privacy import RequestIn, RetentionPolicy
    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    member, retained = "urn:li:glossaryTerm:Test.MemberId", "urn:li:glossaryTerm:Test.Retained"
    s.privacy.identifiers["member_id"] = {"labels": [member], "columns": []}
    tables = {}
    for name in ("rec_events", "rec_held"):
        trino(f"CREATE TABLE lake.sales.{name} (id integer, member_id varchar, created_at timestamp(6) with time zone)")
        trino(f"INSERT INTO lake.sales.{name} VALUES (1, 'm-1', TIMESTAMP '2019-01-01 00:00:00 UTC'), "
              "(2, 'm-2', TIMESTAMP '2019-06-01 00:00:00 UTC'), (3, 'm-1', current_timestamp), (4, 'm-2', current_timestamp)")
        urn = f"urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.{name},PROD)"
        tables[name] = urn
        s.graph.aspects[(urn, "schemaMetadata")] = {"fields": [
            {"fieldPath": "id"}, {"fieldPath": "member_id", "glossaryTerms": {"terms": [{"urn": member}]}},
            {"fieldPath": "created_at"}]}
        s.graph.aspects[(urn, "glossaryTerms")] = {"terms": [{"urn": retained}]}
        s.graph.aspects[(urn, "ownership")] = {"owners": [{"owner": "urn:li:corpuser:ana"}]}
    admin, ana = {"X-Cairn-Actor": "urn:li:corpuser:admin"}, {"X-Cairn-Actor": "urn:li:corpuser:ana"}
    c = httpx.Client(base_url=url, timeout=300)
    s.records.actor = None
    assert c.post("/governance/privacy/retention/enforce", headers=admin).status_code == 503   # who deletes, first
    s.records.actor = "urn:li:corpuser:admin"

    # A legal hold on one table: only an officer places it, and it needs its case and reason.
    assert c.post("/governance/privacy/holds", headers=ana, json={"dataset": tables["rec_held"], "case": "x",
                                                                 "reason": "y"}).status_code == 403
    assert c.post("/governance/privacy/holds", headers=admin, json={"dataset": tables["rec_held"]}).status_code == 400
    hold = c.post("/governance/privacy/holds", headers=admin, json={
        "dataset": tables["rec_held"], "case": "ACME v. Example (2026-114)", "reason": "litigation: keep everything"})
    assert hold.status_code == 201, hold.text
    hid = hold.json()["id"]
    # While it is on, nobody removes or rewrites its rows on Trino, not even an administrator.
    for stmt in ("DELETE FROM lake.sales.rec_held WHERE id = 1", "UPDATE lake.sales.rec_held SET member_id = 'x'",
                 "TRUNCATE TABLE lake.sales.rec_held", "DROP TABLE lake.sales.rec_held",
                 "ALTER TABLE lake.sales.rec_held EXECUTE expire_snapshots(retention_threshold => '0s')"):
        with pytest.raises(RuntimeError, match="Access Denied"):
            trino(stmt)
    trino("INSERT INTO lake.sales.rec_held VALUES (5, 'm-3', current_timestamp)")        # adding is still fine
    assert trino("SELECT count(*) AS n FROM lake.sales.rec_held") == [{"n": 5}]

    # Retention that deletes: rows older than two years go from the table not on hold; the held one keeps them.
    s.privacy.put_policy(RetentionPolicy(id="rec-2y", label=retained, days=730, enforce=True), "urn:li:corpuser:admin")
    out = c.post("/governance/privacy/retention/enforce", headers=admin)
    assert out.status_code == 200, out.text
    assert out.json()["deleted"] >= 2 and out.json()["held"] >= 1, out.json()
    assert [r["id"] for r in trino("SELECT id FROM lake.sales.rec_events ORDER BY id")] == [3, 4]
    assert trino("SELECT count(*) AS n FROM lake.sales.rec_held") == [{"n": 5}]
    runs = {r["dataset"]: r for r in c.get("/governance/privacy/retention/runs", headers=admin).json()}
    assert runs[tables["rec_events"]]["state"] == "deleted" and runs[tables["rec_events"]]["deleted"] == 2
    assert runs[tables["rec_held"]]["state"] == "held"

    # Erasure: m-2 asks to be forgotten. The identifier is sent again (Cairn kept only its hash).
    req = s.privacy.open(RequestIn(kind="deletion", identifier_kind="member_id", identifier="m-2", reference="DSR-9"),
                         "urn:li:corpuser:admin")
    rid = req["id"]
    assert {p["dataset"] for p in req["parts"]} == set(tables.values())
    assert c.post(f"/governance/privacy/requests/{rid}/erase", headers=admin, json={"identifier": "m-1"}).status_code == 403
    assert c.post(f"/governance/privacy/requests/{rid}/erase", headers=ana, json={"identifier": "m-2"}).status_code == 403
    r = c.post(f"/governance/privacy/requests/{rid}/erase", headers=admin, json={"identifier": "m-2"})
    assert r.status_code == 200, r.text
    by = {x["dataset"]: x for x in r.json()["results"]}
    assert by[tables["rec_events"]]["state"] == "DONE" and by[tables["rec_events"]]["rows_deleted"] == 1
    assert by[tables["rec_events"]]["rows_after"] == 0 and by[tables["rec_events"]]["history_expired"] is True
    assert by[tables["rec_held"]]["state"] == "HELD" and by[tables["rec_held"]]["holds"] == [hid]
    assert trino("SELECT count(*) AS n FROM lake.sales.rec_events WHERE member_id = 'm-2'") == [{"n": 0}]
    # History is gone too: the table's only snapshot is the one after the delete.
    assert trino('SELECT count(*) AS n FROM lake.sales."rec_events$snapshots"') == [{"n": 1}]
    assert trino("SELECT count(*) AS n FROM lake.sales.rec_held WHERE member_id = 'm-2'") == [{"n": 2}]

    # The request is complete (the held table is listed as kept), and the proof is signed.
    assert r.json()["request"]["state"] == "COMPLETED"
    proof = c.get(f"/governance/privacy/requests/{rid}/proof", headers=admin).json()
    rep = proof["report"]
    assert rep["request"] == rid and rep["identifier_kind"] == "member_id" and "m-2" not in json.dumps(rep)
    assert {t["dataset"]: t["state"] for t in rep["tables"]} == {tables["rec_events"]: "DONE", tables["rec_held"]: "HELD"}
    assert c.post("/governance/privacy/proof/verify", json={"jws": proof["jws"]}).json()["valid"] is True
    # Anyone can check it with the published key, without Cairn.
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    jwk = c.get("/governance/privacy/proof-keys").json()["keys"][0]
    un = lambda v: base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))  # noqa: E731
    pub = rsa.RSAPublicNumbers(int.from_bytes(un(jwk["e"]), "big"), int.from_bytes(un(jwk["n"]), "big")).public_key()
    h, body, sig = proof["jws"].split(".")
    pub.verify(un(sig), f"{h}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    assert json.loads(un(body)) == rep
    forged = json.loads(un(body))
    forged["tables"][0]["rows_deleted"] = 99
    tampered = f"{h}.{base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip('=')}.{sig}"
    assert c.post("/governance/privacy/proof/verify", json={"jws": tampered}).json()["valid"] is False
    # The proof is not a token: Trino refuses it, and it is signed with a key of its own.
    assert jwk["kid"] not in json.dumps(s.trino_tokens.jwks())
    told = [n for n in s.notify.inbox("urn:li:corpuser:admin") if n["type"] == "privacy.erasure_proved"]
    assert told

    # Releasing the hold needs a reason; afterwards the table can be changed again.
    assert c.post(f"/governance/privacy/holds/{hid}/release", headers=admin, json={}).status_code == 400
    assert c.post(f"/governance/privacy/holds/{hid}/release", headers=admin,
                  json={"note": "case settled"}).json()["released_by"] == "urn:li:corpuser:admin"
    assert c.post(f"/governance/privacy/holds/{hid}/release", headers=admin, json={"note": "x"}).status_code == 404
    trino("DELETE FROM lake.sales.rec_held WHERE id = 5")
    assert c.get("/governance/privacy/holds", headers=admin).json() == [] or \
        hid not in [x["id"] for x in c.get("/governance/privacy/holds", headers=admin).json()]
    assert hid in [x["id"] for x in c.get("/governance/privacy/holds", params={"all": True}, headers=admin).json()]
    acts = {e["action"] for e in s.audit.entries(limit=3000)}
    assert {"privacy.hold_placed", "privacy.hold_released", "privacy.retention_deleted", "privacy.erasure_run",
            "privacy.erasure_proved"} <= acts


def test_bi_assets_are_catalog_entities_and_a_dropped_column_shows_what_breaks(trino, cairn):
    """Saved queries, workbooks and dashboards published to the catalog with the tables and columns they read;
    the impact of dropping a column, asked before it is dropped, names exactly the assets that then fail on Trino."""
    import httpx

    from cairn.quality.collectors import ConnectionRegistry
    from cairn.sqlworkspace.bi_catalog import chart_urn, dashboard_urn

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    trino("CREATE TABLE lake.sales.imp_orders AS SELECT * FROM (VALUES (1, 'EU', 10e0, 'a'), (2, 'US', 20e0, 'b')) "
          "AS t(id, region, amount, note)")
    ds = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.imp_orders,PROD)"
    s.graph.aspects[(ds, "schemaMetadata")] = {"fields": [{"fieldPath": c} for c in ("id", "region", "amount", "note")]}
    admin, ana = {"X-Cairn-Actor": "urn:li:corpuser:admin"}, {"X-Cairn-Actor": "urn:li:corpuser:ana"}
    c = httpx.Client(base_url=url, timeout=120)
    by_region = c.post("/sql/saved", headers=admin, json={
        "name": "imp revenue by region", "connection": "lakehouse",
        "sql": "SELECT o.region, SUM(o.amount) AS revenue FROM sales.imp_orders o GROUP BY o.region"}).json()
    ids_only = c.post("/sql/saved", headers=admin, json={
        "name": "imp ids", "connection": "lakehouse", "sql": "SELECT id FROM sales.imp_orders"}).json()
    wb = c.post("/sql/workbooks", headers=admin, json={"name": "imp notes", "connection": "lakehouse", "spec": {
        "elements": [{"source": {"table": "sales.imp_orders"}, "columns": [{"name": "loud", "formula": "Upper([note])"}],
                      "group_by": ["loud"], "aggregates": [{"name": "n", "formula": "Count()"}]}]}}).json()
    board = c.post("/sql/dashboards", headers=admin, json={"name": "imp board", "tiles": [
        {"query": by_region["id"], "title": "Revenue"}, {"query": ids_only["id"], "title": "Ids"}]}).json()

    # In the catalog: charts with their input tables and columns, a dashboard over its charts.
    chart = s.graph.get_aspect(chart_urn("query", by_region["id"]), "chartInfo")
    assert chart["title"] == "imp revenue by region" and chart["inputs"] == [{"string": ds}]
    fields = s.graph.get_aspect(chart_urn("query", by_region["id"]), "inputFields")["fields"]
    assert {f["schemaFieldUrn"].rsplit(",", 1)[-1].rstrip(")") for f in fields} == {"region", "amount"}
    info = s.graph.get_aspect(dashboard_urn(board["id"]), "dashboardInfo")
    assert set(info["charts"]) == {chart_urn("query", by_region["id"]), chart_urn("query", ids_only["id"])}
    assert info["datasets"] == [ds]
    assert s.graph.get_aspect(chart_urn("workbook", wb["id"]), "chartInfo")["inputs"] == [{"string": ds}]

    # Before dropping `amount`: the revenue query and the dashboard using it break; the ids query and the
    # workbook (which reads note) do not.
    imp = c.get("/sql/impact", params={"dataset": ds, "column": "amount"}, headers=admin).json()
    hit = {(a["kind"], a["id"]) for a in imp["assets"]}
    assert hit == {("query", by_region["id"]), ("dashboard", board["id"])}, imp
    assert next(a for a in imp["assets"] if a["kind"] == "dashboard")["through_queries"] == [by_region["id"]]
    note = c.get("/sql/impact", params={"dataset": ds, "column": "note"}, headers=admin).json()
    assert {(a["kind"], a["id"]) for a in note["assets"]} == {("workbook", wb["id"])}
    table = c.get("/sql/impact", params={"dataset": ds}, headers=admin).json()
    assert len(table["assets"]) == 4
    # Someone the assets are not shared with learns only how many, and whose.
    other = c.get("/sql/impact", params={"dataset": ds, "column": "amount"}, headers=ana).json()
    assert other["assets"] == [] and other["others"] == [{"owner": "urn:li:corpuser:admin", "assets": 2}]

    # Drop it: exactly the predicted ones fail.
    trino("ALTER TABLE lake.sales.imp_orders DROP COLUMN amount")
    assert c.post(f"/sql/saved/{by_region['id']}/run", headers=admin).status_code >= 400
    assert c.post(f"/sql/saved/{ids_only['id']}/run", headers=admin).status_code == 200
    tiles = c.post(f"/sql/dashboards/{board['id']}/render", headers=admin, json={}).json()["tiles"]
    assert "error" in tiles[0] and "error" not in tiles[1], tiles

    # A deleted asset leaves the catalog and the impact.
    assert c.delete(f"/sql/saved/{ids_only['id']}", headers=admin).status_code == 204
    assert s.graph.get_aspect(chart_urn("query", ids_only["id"]), "status") == {"removed": True}
    assert ("query", ids_only["id"]) not in {(a["kind"], a["id"]) for a in c.get(
        "/sql/impact", params={"dataset": ds}, headers=admin).json()["assets"]}
    assert c.post("/sql/catalog/sync", headers=ana).status_code == 403
    assert c.post("/sql/catalog/sync", headers=admin).json()["published"] >= 3


def test_bi_version_history_rolls_a_broken_edit_back(trino, cairn):
    """Every save kept: a saved query and a dashboard broken by an edit are compared with the version before and
    restored to it, as a new version; viewers read the history, only owners and editors restore."""
    import httpx

    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    trino("CREATE TABLE lake.sales.ver_orders AS SELECT * FROM (VALUES (1, 'EU', 10e0), (2, 'US', 20e0)) AS t(id, region, amount)")
    s.graph.aspects[("urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.ver_orders,PROD)", "schemaMetadata")] = {
        "fields": [{"fieldPath": c} for c in ("id", "region", "amount")]}
    admin, bob = {"X-Cairn-Actor": "urn:li:corpuser:admin"}, {"X-Cairn-Actor": "urn:li:corpuser:bob"}
    c = httpx.Client(base_url=url, timeout=120)
    good = "SELECT region, SUM(amount) AS revenue FROM sales.ver_orders GROUP BY region ORDER BY region"
    q = c.post("/sql/saved", headers=admin, json={"name": "ver revenue", "connection": "lakehouse", "sql": good}).json()
    board = c.post("/sql/dashboards", headers=admin, json={"name": "ver board", "tiles": [{"query": q["id"], "title": "Revenue"}]}).json()
    assert c.post(f"/sql/saved/{q['id']}/run", headers=admin).status_code == 200

    # The broken edit: a column that is not there.
    bad = good.replace("SUM(amount)", "SUM(amount_eur)")
    assert c.put(f"/sql/saved/{q['id']}", headers=admin, json={**q, "sql": bad}).status_code == 200
    assert c.post(f"/sql/saved/{q['id']}/run", headers=admin).status_code >= 400
    assert "error" in c.post(f"/sql/dashboards/{board['id']}/render", headers=admin, json={}).json()["tiles"][0]
    c.put(f"/sql/dashboards/{board['id']}", headers=admin, json={"name": "ver board (renamed)",
                                                                "tiles": [{"query": q["id"], "title": "Oops"}]})

    hist = c.get(f"/sql/versions/query/{q['id']}", headers=admin).json()
    assert [h["version"] for h in hist] == [2, 1] and hist[0]["saved_by"] == "urn:li:corpuser:admin"
    d = c.get(f"/sql/versions/query/{q['id']}/diff", params={"a": 1, "b": 2}, headers=admin).json()
    assert d["changed"] == ["sql"] and "amount_eur" in d["diff"]
    assert c.get(f"/sql/versions/query/{q['id']}/1", headers=admin).json()["doc"]["sql"] == good

    # Bob may not see the history of a query not shared with him; shared as a viewer he reads it, but only an
    # editor restores.
    assert c.get(f"/sql/versions/query/{q['id']}", headers=bob).status_code == 404
    c.put(f"/sql/shares/query/{q['id']}", headers=admin, json={"principal": "urn:li:corpuser:bob", "role": "VIEWER"})
    assert len(c.get(f"/sql/versions/query/{q['id']}", headers=bob).json()) == 2
    assert c.post(f"/sql/versions/query/{q['id']}/1/restore", headers=bob).status_code == 403

    # Restored: it runs again, and the restore is a new version.
    r = c.post(f"/sql/versions/query/{q['id']}/1/restore", headers=admin)
    assert r.status_code == 200 and r.json()["asset"]["sql"] == good, r.text
    assert c.post(f"/sql/saved/{q['id']}/run", headers=admin).json()["rows"] == [["EU", 10.0], ["US", 20.0]]
    assert [h["version"] for h in c.get(f"/sql/versions/query/{q['id']}", headers=admin).json()] == [3, 2, 1]
    # The dashboard's own history: back to its first name and title.
    bh = c.get(f"/sql/versions/dashboard/{board['id']}", headers=admin).json()
    assert [h["name"] for h in bh] == ["ver board (renamed)", "ver board"]
    rb = c.post(f"/sql/versions/dashboard/{board['id']}/1/restore", headers=admin).json()["asset"]
    assert rb["name"] == "ver board" and rb["tiles"][0]["title"] == "Revenue"
    assert "error" not in c.post(f"/sql/dashboards/{board['id']}/render", headers=admin, json={}).json()["tiles"][0]
    # A save that changes nothing is not a new version; a deleted asset takes its history with it.
    c.put(f"/sql/saved/{q['id']}", headers=admin, json={**q, "sql": good})
    assert len(c.get(f"/sql/versions/query/{q['id']}", headers=admin).json()) == 3
    assert c.delete(f"/sql/dashboards/{board['id']}", headers=admin).status_code == 204
    assert c.get(f"/sql/versions/dashboard/{board['id']}", headers=admin).status_code == 404
    assert s.bi_versions.count("dashboard", board["id"]) == 0


def test_an_embedded_dashboard_shows_each_portal_user_their_own_rows(trino, cairn, tmp_path):
    """A portal frames a Cairn dashboard for its users with tokens its server signs: in Chromium, Bob's portal
    page shows only Bob's rows (his row filter), a page on another origin cannot frame it, and tokens that are
    reused, expired, tampered with, for another user, origin or dashboard are refused."""
    import http.server
    import json as _json
    import threading as _th
    import time as _time
    import uuid as _uuid

    import httpx
    import uvicorn

    from cairn.app import create_api_app
    from cairn.embed.dashboards import EmbeddedDashboards, load_clients, sign
    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    trino("CREATE TABLE lake.sales.emb_orders AS SELECT * FROM (VALUES (1, 'EU', 10e0), (2, 'US', 20e0), (3, 'EU', 5e0)) "
          "AS t(id, region, amount)")
    s.graph.aspects[("urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.emb_orders,PROD)", "schemaMetadata")] = {
        "fields": [{"fieldPath": c} for c in ("id", "region", "amount")]}
    admin = {"X-Cairn-Actor": "urn:li:corpuser:admin"}
    q = httpx.post(f"{url}/sql/saved", headers=admin, timeout=60, json={
        "name": "emb orders", "connection": "lakehouse", "sql": "SELECT id, region FROM sales.emb_orders ORDER BY id"}).json()
    board = httpx.post(f"{url}/sql/dashboards", headers=admin, timeout=60, json={
        "name": "Orders for partners", "tiles": [{"query": q["id"], "title": "Orders"}]}).json()
    other = httpx.post(f"{url}/sql/dashboards", headers=admin, timeout=60, json={
        "name": "Internal", "tiles": [{"query": q["id"]}]}).json()

    # The portal, on its own origin; and another site.
    hp, xp = port(), port()
    portal, elsewhere = f"http://127.0.0.1:{hp}", f"http://localhost:{xp}"
    secret = b"p" * 40
    (tmp_path / "secret").write_bytes(secret)
    (tmp_path / "clients.json").write_text(_json.dumps({"portal": {
        "secret_file": str(tmp_path / "secret"), "origins": [portal], "dashboards": [board["id"]],
        "users": ["urn:li:corpuser:bob", "urn:li:corpuser:portal-*"]}}))
    s.embedded_dashboards = EmbeddedDashboards(s.engine, s.sql_boards, load_clients(str(tmp_path / "clients.json")), s.audit)
    cp = port()
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=cp, timeout_keep_alive=75, log_level="warning"))
    _th.Thread(target=server.run, daemon=True).start()
    wait(cp)
    cairn_url = f"http://127.0.0.1:{cp}"

    def token(**over):
        claims = {"client": "portal", "sub": "urn:li:corpuser:bob", "dashboard": board["id"], "origin": portal,
                  "exp": int(_time.time()) + 300, "jti": _uuid.uuid4().hex, **over}
        return sign(secret, claims)

    # Refusals, before any browser: each is 403 and nothing is served.
    get = lambda t, did=board["id"]: httpx.get(f"{cairn_url}/embed/v1/dashboards/{did}", params={"token": t}, timeout=30)  # noqa: E731
    used = token()
    assert get(used).status_code == 200
    assert get(used).status_code == 403                                                    # once only
    assert get(token(exp=int(_time.time()) - 5)).status_code == 403                         # expired
    assert get(token(exp=int(_time.time()) + 3600)).status_code == 403                      # too far ahead
    assert get(token(sub="urn:li:corpuser:admin")).status_code == 403                       # not the client's to sign
    assert get(token(origin=elsewhere)).status_code == 403                                  # not the client's origin
    assert get(token(dashboard=other["id"]), other["id"]).status_code == 403               # not given to the client
    assert get(token(), other["id"]).status_code == 403                                     # for another dashboard
    body, sig = token().split(".")
    forged = sig[:-2] + ("AA" if sig[-2:] != "AA" else "EA")                               # always differs from sig
    assert get(f"{body}.{forged}").status_code == 403                                      # tampered
    assert httpx.post(f"{cairn_url}/embed/v1/dashboards/{board['id']}/render", json={}, timeout=30).status_code == 401
    page = get(token())
    assert page.headers["content-security-policy"].startswith(f"frame-ancestors {portal};")

    class Host(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            src = f"{cairn_url}/embed/v1/dashboards/{board['id']}?token={token()}"
            out = f'<!doctype html><title>Portal</title><iframe id="f" src="{src}" width="800" height="400"></iframe>'.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    for p_ in (hp, xp):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", p_), Host)
        _th.Thread(target=srv.serve_forever, daemon=True).start()

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        pg = browser.new_page()
        pg.goto(portal)
        frame = pg.frame_locator("#f")
        frame.locator("[data-tile='0'] table tr").nth(1).wait_for(timeout=60_000)
        cells = frame.locator("[data-tile='0'] td").all_inner_texts()
        assert cells and set(cells[1::2]) == {"EU"} and cells[0::2] == ["1", "3"], cells   # only Bob's rows
        # The same page served from another origin: Chromium will not frame it.
        pg2 = browser.new_page()
        pg2.goto(f"{elsewhere}/")
        pg2.wait_for_timeout(3000)
        inner = pg2.frames[1] if len(pg2.frames) > 1 else None
        assert inner is None or "Orders for partners" not in (inner.content() if inner.url.startswith(cairn_url) else ""), \
            "a page on another origin framed the dashboard"
        browser.close()
    # A session follows what the operator allows now: taken off the client, the dashboard stops rendering.
    sess = s.embedded_dashboards.open(token(), board["id"])["session"]
    render = lambda: httpx.post(f"{cairn_url}/embed/v1/dashboards/{board['id']}/render", json={}, timeout=60,  # noqa: E731
                                headers={"Authorization": f"Embed {sess}"})
    assert render().status_code == 200
    s.embedded_dashboards.clients["portal"]["dashboards"] = []
    assert render().status_code == 403
    server.should_exit = True
    acts = [e["action"] for e in s.audit.entries(limit=3000)]
    assert "embed.session_started" in acts and acts.count("embed.refused") >= 7


def test_bi_as_code_deploys_a_dashboard_to_staging_and_production(trino, cairn, tmp_path):
    """BI in `cairn bundle`: an input table, a saved query, a metric, a workbook and a dashboard with query, element
    and metric tiles deployed from one file to staging and production; a second plan has nothing to do; an edit is
    an update; prune removes what the file no longer has; an input table is never rewritten."""
    import httpx

    from cairn.iac.bundle import BundleError, Client, apply, load, plan
    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    trino("CREATE TABLE lake.sales.bic_orders AS SELECT * FROM (VALUES (1, 'EU', 10e0), (2, 'US', 20e0)) AS t(id, region, amount)")
    s.graph.aspects[("urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.bic_orders,PROD)", "schemaMetadata")] = {
        "fields": [{"fieldPath": c} for c in ("id", "region", "amount")]}
    yaml_text = """
bundle: bi-${env}
input_tables:
  - table: sales.bic_targets_${env}
    connection: lakehouse
    columns: [{name: region, type: text}, {name: target, type: number}]
queries:
  - name: Revenue by region (${env})
    connection: lakehouse
    sql: SELECT region, SUM(amount) AS revenue FROM sales.bic_orders GROUP BY region ORDER BY region
metrics:
  - name: bic_revenue_${env}
    label: Revenue
    connection: lakehouse
    table: sales.bic_orders
    measure: Sum([amount])
    dimensions: [region]
workbooks:
  - name: Orders (${env})
    connection: lakehouse
    spec: {elements: [{source: {table: sales.bic_orders}, sort: [{column: id}]}]}
dashboards:
  - name: Sales (${env})
    tiles:
      - {query: "Revenue by region (${env})", title: Revenue}
      - {element: {source: {table: sales.bic_orders}, group_by: [region], aggregates: [{name: n, formula: "Count()"}]},
         connection: lakehouse, title: Orders by region}
      - {metric: {metrics: ["bic_revenue_${env}"], dimensions: [region]}, title: Metric}
"""
    (tmp_path / "bi.yaml").write_text(yaml_text)
    admin = {"X-Cairn-Actor": "urn:li:corpuser:admin"}
    client = Client(url, "", http=httpx.Client(headers=admin, timeout=120))

    def deploy(env, prune=False):
        b = load(str(tmp_path / "bi.yaml"), {"env": env})
        changes, state = plan(client, b, prune)
        apply(client, b, changes, state, log=lambda *_: None)
        return changes

    for env in ("staging", "prod"):
        made = deploy(env)
        assert sorted((c.kind, c.action) for c in made) == sorted(
            [("input_tables", "create"), ("queries", "create"), ("metrics", "create"), ("workbooks", "create"),
             ("dashboards", "create")]), [c.line() for c in made]
    boards = {d["name"]: d for d in httpx.get(f"{url}/sql/dashboards", headers=admin, timeout=60).json()}
    for env in ("staging", "prod"):
        b = boards[f"Sales ({env})"]
        tiles = httpx.post(f"{url}/sql/dashboards/{b['id']}/render", headers=admin, json={}, timeout=120).json()["tiles"]
        assert [t.get("error") for t in tiles] == [None, None, None], tiles
        assert [tuple(r) for r in tiles[0]["result"]["rows"]] == [("EU", 10.0), ("US", 20.0)]
    names = [t["table_name"] for t in httpx.get(f"{url}/sql/input-tables", headers=admin, timeout=60).json()]
    assert {"sales.bic_targets_staging", "sales.bic_targets_prod"} <= set(names)

    # Nothing to do the second time.
    again = plan(client, load(str(tmp_path / "bi.yaml"), {"env": "prod"}))[0]
    assert {c.action for c in again} == {"keep"}, [c.line() for c in again if c.action != "keep"]
    # An edit is an update, in production only.
    (tmp_path / "bi.yaml").write_text(yaml_text.replace("title: Revenue}", "title: Revenue (EUR)}"))
    upd = [c for c in deploy("prod") if c.action != "keep"]
    assert [(c.kind, c.action) for c in upd] == [("dashboards", "update")]
    boards = {d["name"]: d for d in httpx.get(f"{url}/sql/dashboards", headers=admin, timeout=60).json()}
    assert boards["Sales (prod)"]["tiles"][0]["title"] == "Revenue (EUR)" and boards["Sales (staging)"]["tiles"][0]["title"] == "Revenue"
    # An input table is created by a bundle but never rewritten by one.
    (tmp_path / "bi.yaml").write_text(yaml_text.replace("type: number}]", "type: integer}]"))
    with pytest.raises(BundleError, match="never rewrites"):
        plan(client, load(str(tmp_path / "bi.yaml"), {"env": "prod"}))
    # Prune: the workbook and the metric leave the file, and Cairn.
    trimmed = yaml_text.split("metrics:")[0] + "dashboards:" + yaml_text.split("dashboards:")[1].replace(
        '      - {metric: {metrics: ["bic_revenue_${env}"], dimensions: [region]}, title: Metric}\n', "")
    (tmp_path / "bi.yaml").write_text(trimmed)
    gone = [(c.kind, c.action) for c in deploy("prod", prune=True) if c.action != "keep"]
    assert ("workbooks", "delete") in gone and ("metrics", "delete") in gone, gone
    assert "Orders (prod)" not in [w["name"] for w in httpx.get(f"{url}/sql/workbooks", headers=admin, timeout=60).json()]
    assert "Orders (staging)" in [w["name"] for w in httpx.get(f"{url}/sql/workbooks", headers=admin, timeout=60).json()]


def test_input_table_changes_wait_for_a_reviewer_and_land_as_their_author(trino, cairn):
    """Input-table approvals on real Trino: with reviewers set, a change waits (nothing is written); nobody approves
    their own; an approved change is written as its author; a rejected one never is; a change that no longer
    applies (the row was edited meanwhile) ends failed, with why, instead of overwriting."""
    import httpx

    from cairn.quality.collectors import ConnectionRegistry

    url, s = cairn
    s.data_access.registry = ConnectionRegistry({"lakehouse": {
        "url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem", "run_as_user": True,
        "catalog": {"platform": "iceberg", "database": "", "dialect": "trino"}}})
    admin, ana, carol = ({"X-Cairn-Actor": f"urn:li:corpuser:{u}"} for u in ("admin", "ana", "carol"))
    base = f"{url}/sql/input-tables"
    c = httpx.Client(timeout=120)
    t = c.post(base, headers=admin, json={"connection": "lakehouse", "table": "sales.appr_targets",
                                          "columns": [{"name": "region", "type": "text"}, {"name": "target", "type": "number"}]})
    assert t.status_code == 201, t.text
    tid = t.json()["id"]
    assert c.put(f"{base}/{tid}/approval", headers=ana, json={"reviewers": ["urn:li:corpuser:carol"]}).status_code == 403
    assert c.put(f"{base}/{tid}/approval", headers=admin, json={"reviewers": ["urn:li:corpuser:carol"]}).status_code == 200

    count = lambda: trino("SELECT count(*) AS n FROM lake.sales.appr_targets")[0]["n"]  # noqa: E731
    r = c.post(f"{base}/{tid}/rows", headers=admin, json={"rows": [{"region": "EU", "target": 100}]})
    assert r.status_code == 202 and r.json()["state"] == "pending", r.text
    cid = r.json()["pending"]
    assert count() == 0                                                              # nothing written yet
    bad = c.post(f"{base}/{tid}/rows", headers=admin, json={"rows": [{"region": "EU", "target": "lots"}]})
    assert bad.status_code == 400                                                    # checked when asked, not later
    pending = c.get(f"{base}/{tid}/changes", params={"state": "pending"}, headers=carol).json()
    assert [x["id"] for x in pending] == [cid] and pending[0]["author"] == "urn:li:corpuser:admin"
    told = [n for n in s.notify.inbox("urn:li:corpuser:carol") if n["type"] == "input.change_requested"]
    assert told
    assert c.post(f"{base}/changes/{cid}/approve", headers=admin).status_code == 403    # not their own
    assert c.post(f"{base}/changes/{cid}/approve", headers=ana).status_code == 403      # not a reviewer
    ok = c.post(f"{base}/changes/{cid}/approve", headers=carol)
    assert ok.status_code == 200 and ok.json()["state"] == "applied", ok.text
    assert c.post(f"{base}/changes/{cid}/approve", headers=carol).status_code == 409    # decided once
    row = trino("SELECT _row_id, region, target, _updated_by FROM lake.sales.appr_targets")
    assert len(row) == 1 and row[0]["_updated_by"] == "urn:li:corpuser:admin" and row[0]["target"] == 100.0
    rid = row[0]["_row_id"]
    stamp = c.get(f"{base}/{tid}/rows", headers=admin).json()["rows"][0]
    stamp = stamp[-1]                                                                # _updated_at, as read

    # Rejected: never written.
    r2 = c.patch(f"{base}/{tid}/rows/{rid}", headers=admin, json={"values": {"target": 999}})
    assert r2.status_code == 202
    rej = c.post(f"{base}/changes/{r2.json()['pending']}/reject", headers=carol, json={"note": "too high"})
    assert rej.json()["state"] == "rejected" and rej.json()["note"] == "too high"
    assert trino("SELECT target FROM lake.sales.appr_targets")[0]["target"] == 100.0
    decided = [n for n in s.notify.inbox("urn:li:corpuser:admin") if n["type"] == "input.change_decided"]
    assert {"applied", "rejected"} <= {n["title"].rsplit(" ", 1)[-1] for n in decided}

    # A change asked on the row as it was, approved after someone else changed it: failed, not an overwrite.
    stale = c.patch(f"{base}/{tid}/rows/{rid}", headers=admin, json={"values": {"target": 120}, "expected_updated_at": stamp})
    assert stale.status_code == 202
    trino(f"UPDATE lake.sales.appr_targets SET target = 110, _updated_at = current_timestamp WHERE _row_id = '{rid}'")
    failed = c.post(f"{base}/changes/{stale.json()['pending']}/approve", headers=carol).json()
    assert failed["state"] == "failed" and "changed this row" in failed["result"]["error"], failed
    assert trino("SELECT target FROM lake.sales.appr_targets")[0]["target"] == 110.0
    # Ana (not a reviewer) sees only her own changes; with review off, writes land at once again.
    assert c.get(f"{base}/{tid}/changes", headers=ana).json() == []
    c.put(f"{base}/{tid}/approval", headers=admin, json={"reviewers": []})
    assert c.post(f"{base}/{tid}/rows", headers=admin, json={"rows": [{"region": "US", "target": 50}]}).status_code == 201
    assert count() == 2
    acts = {e["action"] for e in s.audit.entries(limit=3000)}
    assert {"input.reviewers_set", "input.change_requested", "input.change_applied", "input.change_rejected",
            "input.change_failed"} <= acts


def test_a_missing_day_in_an_iceberg_table_fails_its_partitioned_volume_monitor(trino, cairn):
    """L43 on Trino: an Iceberg table partitioned by day(ts) holds a month of rows. Yesterday's partition goes
    missing while a backfill puts as many rows on the day before, so the table's total stays the same; the monitor
    counting rows per day fails on yesterday and recovers when it is loaded."""
    from datetime import datetime, timezone

    from cairn.quality.collectors import ConnectionRegistry, SqlCollector
    from cairn.quality.engine import QualityEngine
    from cairn.quality.models import MonitorSpec
    from cairn.quality.store import QualityStore

    _, s = cairn
    day = 86_400_000
    now = int(time.time() * 1000)
    start = lambda back: datetime.fromtimestamp((now // day - back) * day / 1000, tz=timezone.utc)  # noqa: E731

    def load(counts):
        rows = ", ".join(f"(TIMESTAMP '{start(b):%Y-%m-%d %H:%M:%S}', {n})" for b, n in counts.items() if n)
        trino("DELETE FROM lake.sales.daily_events")
        trino("INSERT INTO lake.sales.daily_events SELECT n, d + n * INTERVAL '1' MINUTE "
              f"FROM (VALUES {rows}) AS v(d, c) CROSS JOIN UNNEST(sequence(1, c)) AS u(n)")

    trino("CREATE TABLE IF NOT EXISTS lake.sales.daily_events (id integer, ts timestamp(6)) WITH (partitioning = ARRAY['day(ts)'])")
    normal = {back: 20 + back % 3 for back in range(1, 29)}
    load(normal)
    # The monitor reads through a governed connection as its service user, signed in with a fresh Cairn token
    # (Trino asks Cairn's policies about that user). Without Cairn's sign-in it refuses to run at all.
    lake_conn = {"url": f"trino://cairn@127.0.0.1:{trino.tls}/lake?protocol=https&verify={trino.dir}/ca.pem",
                 "run_as_user": True, "service_user": "urn:li:corpuser:admin"}
    unsigned = QualityEngine(QualityStore(s.engine), None, {"sql": SqlCollector(ConnectionRegistry({"lake": lake_conn}))})
    reg = ConnectionRegistry({"lake": lake_conn})
    reg.trino_tokens = s.trino_tokens
    qe = QualityEngine(QualityStore(s.engine), None, {"sql": SqlCollector(reg)})
    m = qe.store.create_monitor(MonitorSpec(
        name="daily events", dataset_urn="urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.daily_events,PROD)",
        type="volume", config={"source": "sql", "connection": "lake", "table": "sales.daily_events", "partition_column": "ts"}),
        "urn:li:corpuser:admin")
    refused = unsigned.evaluate(unsigned.store.get_monitor(m.id), now=now - 1)
    assert refused.status == "ERROR" and "sign-in" in refused.message, refused.message
    ok = qe.evaluate(qe.store.get_monitor(m.id), now=now)
    assert ok.status == "PASS", ok.message
    assert ok.metric_value == normal[1]

    late = {**normal, 1: 0, 2: normal[1] + normal[2]}
    load(late)
    assert trino("SELECT count(*) AS n FROM lake.sales.daily_events")[0]["n"] == sum(normal.values())   # the same total
    bad = qe.evaluate(qe.store.get_monitor(m.id), now=now + 1)
    assert bad.status == "FAIL" and bad.details["missing"] == [f"{start(1):%Y-%m-%d}"], bad.message

    load(normal)
    assert qe.evaluate(qe.store.get_monitor(m.id), now=now + 2).status == "PASS"
    trino("DROP TABLE lake.sales.daily_events")
