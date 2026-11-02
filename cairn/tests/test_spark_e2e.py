"""Spark on the lakehouse, governed by Cairn table by table, end to end, in the cairn/spark image (Spark 4
with Iceberg): a person signs in with their Cairn token; Cairn Catalog asks Cairn about every table for
them, lists only what they may see, refuses tables a mask or row filter applies to (their files would
show what the mask or filter hides), lets them write only where Cairn's policies allow, and hands Spark
per-table temporary credentials, never a store key. Spark's lineage reaches Cairn.

Needs docker, the cairn/spark image (CAIRN_SPARK_IMAGE) and the store and catalog binaries."""

import json
import os
import shutil
import subprocess
import tempfile
import textwrap

import pytest
import httpx

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, cairn, port, wait  # noqa: F401  (fixtures)

IMAGE = os.environ.get("CAIRN_SPARK_IMAGE", "")
pytestmark = pytest.mark.skipif(not (_ready() and IMAGE), reason="needs docker, CAIRN_SPARK_IMAGE and the binaries")

PATS = {"pat-ana-" + "a" * 20: "urn:li:corpuser:ana", "pat-bob-" + "b" * 20: "urn:li:corpuser:bob"}
ANA, BOB = list(PATS)
STS = "spark-sts-secret-" + "s" * 32
SERVICE = "catalog-service-token-" + "c" * 24  # the Cairn service token Cairn Catalog asks with


@pytest.fixture(scope="module")
def people(cairn):  # noqa: F811
    from cairn.governance.abac import Policy, PolicyBundle, Target

    _, s = cairn
    real, real_service = s.identity._me_lookup, s.identity.service_token
    s.identity._me_lookup = lambda url, h, c: PATS.get((h.get("Authorization") or "")[7:]) or real(url, h, c)
    s.identity.service_token = SERVICE
    s.lakehouse.trino_catalog = "lake"  # the test API maps Trino's "lake" catalog to its datasets
    ana = {"attr": "subject.urn", "op": "eq", "value": "urn:li:corpuser:ana"}
    bundle = s.pdp.bundle
    bundle = bundle() if callable(bundle) else bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*bundle.policies,
        Policy(id="ana-analytics", effect="allow", condition=ana,
               actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE", "WRITE_DATASET", "MANAGE_DATASET"],
               target=Target(resource_urns=["*analytics.*"])),
        Policy(id="ana-lineage", effect="allow", condition=ana, actions=["EDIT_LINEAGE"])]))
    yield
    s.identity._me_lookup, s.identity.service_token = real, real_service


@pytest.fixture(scope="module")
def lake(cairn, people):  # noqa: F811
    """Cairn Store with STS, and Cairn Catalog taking Cairn-issued tokens (decided by the test API)."""
    url, _ = cairn
    d = tempfile.mkdtemp()
    sp, cp = port(), port()
    store = subprocess.Popen([os.environ["CAIRN_STORE_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **os.environ, "CAIRN_STORE_ACCESS_KEY": STORE_KEY[0], "CAIRN_STORE_SECRET_KEY": STORE_KEY[1],
        "CAIRN_STORE_DATA": f"{d}/s", "CAIRN_STORE_ADDR": f"0.0.0.0:{sp}", "CAIRN_STORE_BUCKETS": "lakehouse",
        "CAIRN_STORE_FSYNC": "false", "CAIRN_STORE_META": "", "CAIRN_STORE_STS_SECRET": STS})
    wait(sp)
    env = {**os.environ, "CAIRN_CATALOG_ADDR": f"0.0.0.0:{cp}", "CAIRN_CATALOG_DATA": f"{d}/c", "CAIRN_CATALOG_TOKEN": CATALOG_TOKEN,
           "CAIRN_CATALOG_S3_ENDPOINT": f"http://127.0.0.1:{sp}", "CAIRN_CATALOG_S3_ACCESS_KEY": STORE_KEY[0],
           "CAIRN_CATALOG_S3_SECRET_KEY": STORE_KEY[1], "CAIRN_CATALOG_WAREHOUSE": "s3://lakehouse/warehouse",
           "CAIRN_CATALOG_FSYNC": "false", "CAIRN_CATALOG_VEND_ROLE": "cairn-catalog-tables",
           "CAIRN_CATALOG_JWKS_URL": f"{url}/v1/trino/jwks.json", "CAIRN_CATALOG_CAIRN_URL": url,
           "CAIRN_CATALOG_CAIRN_TOKEN": SERVICE}
    catalog = subprocess.Popen([os.environ["CAIRN_CATALOG_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    wait(cp)
    # The admin's (static) token sets up tables: sales.orders (email is PII: masked for ana, filtered for
    # bob), hr.salaries (ana may not see it) and analytics.products (nothing restricted).
    from pyiceberg.catalog.rest import RestCatalog
    import pyarrow as pa
    cat = RestCatalog("admin", uri=f"http://127.0.0.1:{cp}", token=CATALOG_TOKEN)
    for ns in ("sales", "hr", "analytics"):
        cat.create_namespace(ns)
    orders = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "email": ["jane@acme.com", "bo@example.org", "li@acme.com"],
                       "region": ["EU", "US", "EU"]})
    cat.create_table("sales.orders", schema=orders.schema).append(orders)
    sal = pa.table({"name": ["jane"], "salary": pa.array([100], pa.int64())})
    cat.create_table("hr.salaries", schema=sal.schema).append(sal)
    prod = pa.table({"sku": ["a", "b", "c"], "price": pa.array([5, 7, 9], pa.int64())})
    cat.create_table("analytics.products", schema=prod.schema).append(prod)
    yield {"catalog": f"http://127.0.0.1:{cp}", "store": f"http://127.0.0.1:{sp}", "env": env, "admin": cat, "dir": d}
    for p in (catalog, store):
        p.terminate()
        p.wait(15)
    shutil.rmtree(d, ignore_errors=True)


JOB = textwrap.dedent('''
    import json, sys
    from cairn_spark import lakehouse_session

    spark = lakehouse_session("governed-job", master="local[2]")
    out = {}

    def attempt(name, fn):
        try:
            out[name] = {"ok": fn()}
        except Exception as e:  # what Spark reports
            out[name] = {"error": str(e)[:600]}

    for step in sys.argv[1:]:
        kind, _, arg = step.partition("=")
        if kind == "tables":
            attempt(step, lambda: sorted(r.tableName for r in spark.sql(f"SHOW TABLES IN lakehouse.{arg}").collect()))
        elif kind == "count":
            attempt(step, lambda: spark.sql(f"SELECT count(*) AS n FROM lakehouse.{arg}").collect()[0].n)
        elif kind == "sql":
            attempt(step, lambda: [list(r) for r in spark.sql(arg).collect()])
    print("CAIRN-RESULT " + json.dumps(out))
    spark.stop()
''')


def run_job(tmp_path, cairn_url, lake, token, *steps):
    job = tmp_path / "job.py"
    job.write_text(JOB)
    os.chmod(tmp_path, 0o755)
    r = subprocess.run(["docker", "run", "--rm", "--network", "host", "-v", f"{job}:/job/job.py:ro",
                        "-e", f"CAIRN_URL={cairn_url}", "-e", f"CAIRN_TOKEN={token}", "-e", f"CAIRN_CATALOG_URI={lake['catalog']}",
                        IMAGE, "/opt/spark/bin/spark-submit", "--master", "local[2]", "--conf", "spark.ui.enabled=false",
                        "/job/job.py", *steps], capture_output=True, text=True, timeout=900)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("CAIRN-RESULT ")), None)
    assert line, (r.stdout + r.stderr)[-6000:]
    return json.loads(line[len("CAIRN-RESULT "):]), r.stdout + r.stderr


def test_spark_is_governed_by_cairn_table_by_table(cairn, lake, tmp_path):  # noqa: F811
    url, s = cairn
    s.lakehouse.catalog_uris = (lake["catalog"],)  # CAIRN_LAKEHOUSE_CATALOG_URIS
    res, log = run_job(tmp_path, url, lake, ANA,
                       "tables=sales", "tables=hr", "count=sales.orders", "count=hr.salaries", "count=analytics.products",
                       "sql=CREATE TABLE lakehouse.analytics.cheap AS SELECT sku, price FROM lakehouse.analytics.products WHERE price < 8",
                       "sql=INSERT INTO lakehouse.analytics.cheap VALUES ('z', 1)", "count=analytics.cheap",
                       "sql=INSERT INTO lakehouse.sales.orders VALUES (9, 'x@y.z', 'EU')")
    # What ana may see is listed; hr is not hers to see.
    assert res["tables=sales"] == {"ok": ["orders"]} and res["tables=hr"] == {"ok": []}, res
    # sales.orders has a masked column for her: its files are not handed to Spark.
    assert "Masks or row filters apply" in res["count=sales.orders"]["error"], res
    assert "do not let" in res["count=hr.salaries"]["error"] or "Forbidden" in res["count=hr.salaries"]["error"], res
    # Where nothing is restricted she reads, and in analytics she builds tables and writes.
    assert res["count=analytics.products"] == {"ok": 3}
    assert res["count=analytics.cheap"] == {"ok": 3}, res
    assert "error" in res["sql=INSERT INTO lakehouse.sales.orders VALUES (9, 'x@y.z', 'EU')"]
    # The table she built is in the catalog, with her rows.
    assert lake["admin"].load_table("analytics.cheap").scan().to_arrow().num_rows == 3
    # Spark's lineage reached Cairn, on the datasets Trino and the catalog name (not storage paths):
    # analytics.cheap was made from analytics.products, column by column.
    import time
    cheap, products = s.lakehouse.urn(["analytics"], "cheap"), s.lakehouse.urn(["analytics"], "products")
    end = time.time() + 60
    found = []
    while time.time() < end and not found:
        found = [v for (u, a), v in list(s.graph.aspects.items()) if a == "dataJobInputOutput" and cheap in v.get("outputDatasets", [])]
        time.sleep(1)
    assert found, sorted({u for (u, a) in s.graph.aspects if a == "dataJobInputOutput"})
    ctas = next(v for v in found if products in v.get("inputDatasets", []))
    pairs = {(tuple(f["upstreams"]), tuple(f["downstreams"])) for f in ctas.get("fineGrainedLineages") or []}
    assert ((f"urn:li:schemaField:({products},price)",), (f"urn:li:schemaField:({cheap},price)",)) in pairs, pairs


def test_the_example_job_runs_as_its_cairn_user(cairn, lake, tmp_path):  # noqa: F811
    """examples/spark/lakehouse_products.py, as Compose's cairn-spark service runs it."""
    url, _ = cairn
    jobs = os.path.join(os.path.dirname(__file__), "..", "examples", "spark")
    r = subprocess.run(["docker", "run", "--rm", "--network", "host", "-v", f"{os.path.abspath(jobs)}:/jobs:ro",
                        "-e", f"CAIRN_URL={url}", "-e", f"CAIRN_TOKEN={ANA}", "-e", f"CAIRN_CATALOG_URI={lake['catalog']}",
                        IMAGE, "/opt/spark/bin/spark-submit", "--master", "local[2]", "--conf", "spark.ui.enabled=false",
                        "/jobs/lakehouse_products.py"], capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, (r.stdout + r.stderr)[-6000:]
    rows = lake["admin"].load_table("analytics.products_by_price_band").scan().to_arrow().to_pylist()
    assert {(x["price_band"], x["products"]) for x in rows} == {("low", 3)}, rows
    # As bob, who may not see analytics, the same job is refused.
    r = subprocess.run(["docker", "run", "--rm", "--network", "host", "-v", f"{os.path.abspath(jobs)}:/jobs:ro",
                        "-e", f"CAIRN_URL={url}", "-e", f"CAIRN_TOKEN={BOB}", "-e", f"CAIRN_CATALOG_URI={lake['catalog']}",
                        IMAGE, "/opt/spark/bin/spark-submit", "--master", "local[2]", "--conf", "spark.ui.enabled=false",
                        "/jobs/lakehouse_products.py"], capture_output=True, text=True, timeout=900)
    assert r.returncode != 0


def test_row_filters_also_keep_files_away_and_others_see_less(cairn, lake, tmp_path):  # noqa: F811
    url, _ = cairn
    res, _ = run_job(tmp_path, url, lake, BOB, "tables=analytics", "count=sales.orders",
                     "sql=INSERT INTO lakehouse.analytics.products VALUES ('q', 3)")
    assert res["tables=analytics"] == {"ok": []}  # analytics is not bob's to see
    assert "Masks or row filters apply" in res["count=sales.orders"]["error"]  # his row filter
    assert "error" in res["sql=INSERT INTO lakehouse.analytics.products VALUES ('q', 3)"]
    assert lake["admin"].load_table("analytics.products").scan().to_arrow().num_rows == 3


def test_the_catalog_takes_only_cairns_lakehouse_tokens_and_fails_closed(cairn, lake):  # noqa: F811
    url, s = cairn
    cat = lake["catalog"]
    good = httpx.post(f"{url}/v1/lakehouse/token", headers={"Authorization": f"Bearer {ANA}"}, timeout=10).json()["token"]
    ok = httpx.get(f"{cat}/v1/namespaces/analytics/tables/products", headers={"Authorization": f"Bearer {good}"}, timeout=10)
    assert ok.status_code == 200
    creds = ok.json()["storage-credentials"][0]["config"]
    assert creds["s3.access-key-id"] != STORE_KEY[0] and creds.get("s3.session-token")  # temporary, never the store key
    # A Trino token (another audience), a forged token and garbage are refused.
    trino_tok = s.trino_tokens.mint("urn:li:corpuser:ana")["token"]
    for bad in (trino_tok, good[:-4] + "AAAA", "x.y.z"):
        r = httpx.get(f"{cat}/v1/namespaces/analytics/tables/products", headers={"Authorization": f"Bearer {bad}"}, timeout=10)
        assert r.status_code == 401, (bad[:20], r.text)
    # Service and agent credentials get no lakehouse token.
    r = httpx.post(f"{url}/v1/lakehouse/token", headers={"Authorization": f"Bearer {SERVICE}"}, timeout=10)
    assert r.status_code == 403
    # Only Cairn Catalog (a service token) may ask for decisions.
    r = httpx.post(f"{url}/v1/lakehouse/decide", json={"user": "ana", "tables": []},
                      headers={"Authorization": f"Bearer {ANA}"}, timeout=10)
    assert r.status_code == 403
    # A catalog that cannot reach Cairn refuses rather than guessing.
    dead = port()
    env = {**lake["env"], "CAIRN_CATALOG_ADDR": f"0.0.0.0:{dead}", "CAIRN_CATALOG_DATA": f"{lake['dir']}/c2",
           "CAIRN_CATALOG_CAIRN_URL": f"http://127.0.0.1:{port()}"}
    p = subprocess.Popen([os.environ["CAIRN_CATALOG_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        wait(dead)
        httpx.post(f"http://127.0.0.1:{dead}/v1/namespaces", json={"namespace": ["analytics"]},
                      headers={"Authorization": f"Bearer {CATALOG_TOKEN}"}, timeout=10)
        r = httpx.get(f"http://127.0.0.1:{dead}/v1/namespaces/analytics/tables", headers={"Authorization": f"Bearer {good}"},
                         timeout=30)
        assert r.status_code in (200, 503)  # an empty namespace needs no decision
        r = httpx.get(f"http://127.0.0.1:{dead}/v1/namespaces/analytics/tables/products",
                         headers={"Authorization": f"Bearer {good}"}, timeout=30)
        assert r.status_code == 503, r.text
    finally:
        p.terminate()
        p.wait(15)
    # And one configured without scoped credentials does not start.
    env = {**lake["env"], "CAIRN_CATALOG_ADDR": f"0.0.0.0:{port()}", "CAIRN_CATALOG_DATA": f"{lake['dir']}/c3",
           "CAIRN_CATALOG_VEND_ROLE": "", "CAIRN_CATALOG_VEND_CREDENTIALS": "static"}
    r = subprocess.run([os.environ["CAIRN_CATALOG_BIN"]], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "scoped credentials" in (r.stdout + r.stderr)
