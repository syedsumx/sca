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
        "CAIRN_STORE_ADDR": f"127.0.0.1:{sp}", "CAIRN_STORE_BUCKETS": "lakehouse", "CAIRN_STORE_FSYNC": "false",
        "CAIRN_STORE_META": ""})]
    wait(sp)
    procs.append(subprocess.Popen([os.environ["CAIRN_CATALOG_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **os.environ, "CAIRN_CATALOG_ADDR": f"127.0.0.1:{cp}", "CAIRN_CATALOG_DATA": f"{d}/c", "CAIRN_CATALOG_TOKEN": CATALOG_TOKEN,
        "CAIRN_CATALOG_S3_ENDPOINT": f"http://127.0.0.1:{sp}", "CAIRN_CATALOG_S3_ACCESS_KEY": STORE_KEY[0],
        "CAIRN_CATALOG_S3_SECRET_KEY": STORE_KEY[1], "CAIRN_CATALOG_WAREHOUSE": "s3://lakehouse/warehouse",
        "CAIRN_CATALOG_FSYNC": "false"}))
    wait(cp)
    yield {"store": f"http://127.0.0.1:{sp}", "catalog": f"http://127.0.0.1:{cp}"}
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
    try:
        graph = InMemoryGraph()
        graph.aspects[(ORDERS, "schemaMetadata")] = {"fields": [
            {"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [{"urn": EMAIL}]}}, {"fieldPath": "region"}]}
        settings = Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-secret", dev_trust_actor_header=True,
                            gateway={"key_dir": str(tmp / "keys")})
        s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    finally:
        os.environ.pop("CAIRN_TRINO_ACCESS_KEY")
        os.environ.pop("CAIRN_TRINO_CATALOGS")
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
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="127.0.0.1", port=p, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    wait(p)
    yield f"http://127.0.0.1:{p}"
    server.should_exit = True
    t.join(10)


@pytest.fixture(scope="module")
def trino(lake, cairn):
    d = tempfile.mkdtemp(prefix="trino-")
    os.makedirs(f"{d}/catalog")
    tp = port()
    open(f"{d}/catalog/lake.properties", "w").write("\n".join([
        "connector.name=iceberg", "iceberg.catalog.type=rest", f"iceberg.rest-catalog.uri={lake['catalog']}",
        "iceberg.rest-catalog.security=OAUTH2", f"iceberg.rest-catalog.oauth2.token={CATALOG_TOKEN}",
        "fs.native-s3.enabled=true", f"s3.endpoint={lake['store']}", "s3.region=us-east-1", "s3.path-style-access=true",
        f"s3.aws-access-key={STORE_KEY[0]}", f"s3.aws-secret-key={STORE_KEY[1]}", ""]))
    base = f"{cairn}/v1/trino/{KEY}"
    open(f"{d}/access-control.properties", "w").write("\n".join([
        "access-control.name=opa", f"opa.policy.uri={base}/allow", f"opa.policy.batched-uri={base}/batch",
        f"opa.policy.row-filters-uri={base}/row-filters", f"opa.policy.column-masking-uri={base}/column-mask", ""]))
    open(f"{d}/config.properties", "w").write(f"coordinator=true\nnode-scheduler.include-coordinator=true\n"
                                              f"http-server.http.port={tp}\ndiscovery.uri=http://127.0.0.1:{tp}\n")
    os.chmod(d, 0o755)
    name = f"cairn-trino-acl-{os.getpid()}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "host",
                    "-v", f"{d}/catalog:/etc/trino/catalog:ro", "-v", f"{d}/config.properties:/etc/trino/config.properties:ro",
                    "-v", f"{d}/access-control.properties:/etc/trino/access-control.properties:ro", TRINO],
                   check=True, capture_output=True, timeout=600)
    end = time.time() + 240
    while "SERVER STARTED" not in subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr:
        assert time.time() < end, "Trino did not start"
        time.sleep(1)

    def sql(statement, user="admin"):
        out = subprocess.run(["docker", "exec", name, "trino", "--server", f"http://127.0.0.1:{tp}", "--user", user,
                              "--catalog", "lake", "--output-format", "JSON", "--execute", statement],
                             capture_output=True, text=True, timeout=300)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip().splitlines()[-1] if out.stderr.strip() else out.stdout)
        return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]

    sql("CREATE SCHEMA lake.sales")
    sql("CREATE SCHEMA lake.hr")
    sql("CREATE TABLE lake.sales.orders AS SELECT * FROM (VALUES (1, 'jane@acme.com', 'EU'), (2, 'bo@example.org', 'US'), "
        "(3, 'li@acme.com', 'EU')) AS t(id, email, region)")
    sql("CREATE TABLE lake.hr.salaries AS SELECT 'jane' AS name, 100 AS salary")
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
