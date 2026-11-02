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


LONG_JOB = textwrap.dedent('''
    import json, sys, time
    from cairn_spark import lakehouse_session

    # One table handle for the whole job (the catalog is not asked again), so storage keys are renewed
    # through the catalog's credentials endpoint and commits need a renewed catalog token.
    spark = lakehouse_session("long-job", master="local[2]",
                              extra_conf={"spark.sql.catalog.lakehouse.cache.expiration-interval-ms": "-1"})
    rounds, pause = int(sys.argv[1]), float(sys.argv[2])
    start = time.time()
    for i in range(rounds):
        row = {"i": i, "t": round(time.time() - start, 1)}
        try:
            row["read"] = spark.sql("SELECT count(*) AS n FROM lakehouse.analytics.products").collect()[0].n
        except Exception as e:
            row["read_error"] = str(e)[:300]
        try:
            spark.sql(f"INSERT INTO lakehouse.analytics.long_job VALUES ({i})")
            row["wrote"] = True
        except Exception as e:
            row["write_error"] = str(e)[:300]
        print("CAIRN-ROUND " + json.dumps(row), flush=True)
        time.sleep(pause)
    spark.stop()
''')


@pytest.fixture
def renewing_catalog(cairn, lake, tmp_path):  # noqa: F811
    """A catalog that tells clients their storage keys expire after 6 minutes (renewed after 1), and
    Cairn lakehouse tokens that last a minute: a 3-minute job outlives both several times."""
    _, s = cairn
    cp = port()
    log = open(tmp_path / "catalog.log", "w")
    env = {**lake["env"], "CAIRN_CATALOG_ADDR": f"0.0.0.0:{cp}", "CAIRN_CATALOG_DATA": str(tmp_path / "c"),
           "CAIRN_CATALOG_VEND_SECONDS": "900", "CAIRN_CATALOG_VEND_REVALIDATE_SECONDS": "360"}
    p = subprocess.Popen([os.environ["CAIRN_CATALOG_BIN"]], stdout=log, stderr=subprocess.STDOUT, env=env)
    wait(cp)
    from pyiceberg.catalog.rest import RestCatalog
    import pyarrow as pa
    admin = RestCatalog("admin", uri=f"http://127.0.0.1:{cp}", token=CATALOG_TOKEN)
    admin.create_namespace("analytics")
    prod = pa.table({"sku": ["a", "b", "c"], "price": pa.array([5, 7, 9], pa.int64())})
    admin.create_table("analytics.products", schema=prod.schema).append(prod)
    admin.create_table("analytics.long_job", schema=pa.schema([("i", pa.int64())]))
    ttl = s.lakehouse_sessions.ttl
    s.lakehouse_sessions.ttl = 60
    yield {"uri": f"http://127.0.0.1:{cp}", "log": tmp_path / "catalog.log", "admin": admin}
    s.lakehouse_sessions.ttl = ttl
    p.terminate()
    p.wait(15)


def start_long_job(tmp_path, cairn_url, catalog_uri, token, rounds, pause):
    job = tmp_path / "long.py"
    job.write_text(LONG_JOB)
    os.chmod(tmp_path, 0o755)
    return subprocess.Popen(["docker", "run", "--rm", "--network", "host", "-v", f"{job}:/job/long.py:ro",
                             "-e", f"CAIRN_URL={cairn_url}", "-e", f"CAIRN_TOKEN={token}", "-e", f"CAIRN_CATALOG_URI={catalog_uri}",
                             IMAGE, "/opt/spark/bin/spark-submit", "--master", "local[2]", "--conf", "spark.ui.enabled=false",
                             "/job/long.py", str(rounds), str(pause)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def rounds_of(output):
    return [json.loads(ln.split("CAIRN-ROUND ", 1)[1]) for ln in output.splitlines() if "CAIRN-ROUND " in ln]


def test_a_long_job_outlives_its_token_and_its_storage_keys(cairn, lake, renewing_catalog, tmp_path):  # noqa: F811
    url, s = cairn
    before = {e["id"] for e in s.audit.entries("lakehouse", limit=1000)}
    # Past the keys' reported expiry (6 minutes; the AWS SDK renews them at a random time in their last 5),
    # and many times the one-minute tokens.
    p = start_long_job(tmp_path, url, renewing_catalog["uri"], ANA, rounds=27, pause=15)
    out, _ = p.communicate(timeout=1200)
    rounds = rounds_of(out)
    assert len(rounds) == 27 and p.returncode == 0, out[-6000:]
    assert rounds[-1]["t"] >= 380
    bad = [r for r in rounds if "read" not in r or not r.get("wrote")]
    assert not bad, (bad, out[-4000:])
    assert renewing_catalog["admin"].load_table("analytics.long_job").scan().to_arrow().num_rows == 27
    # Iceberg got and renewed its catalog tokens at Cairn, in one session (with the session credential,
    # then by token exchange), and its storage keys through the catalog.
    new = [e for e in s.audit.entries("lakehouse", limit=1000) if e["id"] not in before]
    refreshed = [e for e in new if e["action"] == "lakehouse.token_refreshed" and e["actor"] == "urn:li:corpuser:ana"]
    assert len([e for e in refreshed if e["details"]["with"] == "token"]) >= 2, [e["action"] for e in new]
    assert len({e["details"]["session"] for e in refreshed}) == 1
    renewed = [ln for ln in renewing_catalog["log"].read_text().splitlines() if "storage credentials renewed" in ln]
    assert renewed and all('"principal":"user:ana"' in ln for ln in renewed), renewed[:3]


def test_revoked_or_suspended_sessions_end_within_a_token_lifetime(cairn, lake, renewing_catalog, tmp_path):  # noqa: F811
    url, s = cairn
    p = start_long_job(tmp_path, url, renewing_catalog["uri"], ANA, rounds=14, pause=15)
    lines = []
    for ln in p.stdout:
        lines.append(ln)
        if sum("CAIRN-ROUND " in x for x in lines) == 2:
            break
    # Only administrators revoke: ana cannot end her own (or anyone's) sessions through the API.
    r = httpx.post(f"{url}/v1/lakehouse/sessions/revoke", json={"user": "ana"},
                   headers={"Authorization": f"Bearer {ANA}"}, timeout=10)
    assert r.status_code == 403
    revoked = s.lakehouse_sessions.revoke("ana", "urn:li:corpuser:admin")
    assert revoked["user"] == "ana" and revoked["sessions_ended"] >= 1
    t_revoke = rounds_of("".join(lines))[-1]["t"]
    rest, _ = p.communicate(timeout=900)
    rounds = rounds_of("".join(lines) + rest)
    # Once the last token renewed before the revocation is gone: its 60 s, plus the catalog's minute of
    # allowed clock skew.
    after = [r for r in rounds if r["t"] > t_revoke + 60 + 60 + 15]
    assert after and all(not r.get("wrote") for r in after), rounds
    assert any(e["action"] == "lakehouse.sessions_revoked" for e in s.audit.entries("lakehouse", limit=200))
    # A new session works again (revocation ends the sessions that existed, not the user).
    r = httpx.post(f"{url}/v1/lakehouse/token", headers={"Authorization": f"Bearer {ANA}"}, timeout=10)
    assert r.status_code == 200


def test_token_exchange_is_strict(cairn, lake, renewing_catalog):  # noqa: F811
    url, s = cairn
    ex = f"{url}/v1/lakehouse/oauth/tokens"
    grant = {"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
             "subject_token_type": "urn:ietf:params:oauth:token-type:access_token"}
    tok = httpx.post(f"{url}/v1/lakehouse/token", headers={"Authorization": f"Bearer {BOB}"}, timeout=10).json()
    r = httpx.post(ex, data={**grant, "subject_token": tok["token"]}, timeout=10)
    assert r.status_code == 200 and r.json()["issued_token_type"].endswith("access_token") and r.json()["access_token"] != tok["token"]
    # The session credential, as form fields (Iceberg) or HTTP Basic; a wrong secret is an unknown client.
    sid, secret = tok["credential"].split(":", 1)
    assert httpx.post(ex, data={"grant_type": "client_credentials", "client_id": sid, "client_secret": secret,
                                "scope": "catalog"}, timeout=10).status_code == 200
    assert httpx.post(ex, data={"grant_type": "client_credentials"}, auth=(sid, secret), timeout=10).status_code == 200
    r = httpx.post(ex, data={"grant_type": "client_credentials", "client_id": sid, "client_secret": "nope"}, timeout=10)
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    # Not another service's token, not a token without a session, not a forged one, not another grant.
    trino = s.trino_tokens.mint("urn:li:corpuser:bob")["token"]
    sessionless = s.trino_tokens.mint("urn:li:corpuser:bob", audience="lakehouse")["token"]
    for bad in (trino, sessionless, tok["token"][:-4] + "AAAA", "x.y.z"):
        assert httpx.post(ex, data={**grant, "subject_token": bad}, timeout=10).json()["error"] == "invalid_grant"
    assert httpx.post(ex, data={**grant, "grant_type": "password", "subject_token": tok["token"]},
                      timeout=10).json()["error"] == "unsupported_grant_type"
    # A suspended user's session is not renewed; Cairn unable to tell refuses too.
    status = s.lakehouse_sessions.user_status
    try:
        s.lakehouse_sessions.user_status = lambda urn: "SUSPENDED"
        assert httpx.post(ex, data={**grant, "subject_token": tok["token"]}, timeout=10).json()["error"] == "invalid_grant"
        s.lakehouse_sessions.user_status = lambda urn: (_ for _ in ()).throw(RuntimeError("graph down"))
        r = httpx.post(ex, data={**grant, "subject_token": tok["token"]}, timeout=10)
        assert r.status_code == 503 and r.json()["error"] == "temporarily_unavailable"
    finally:
        s.lakehouse_sessions.user_status = status
    # A session ends at its maximum length.
    m = s.lakehouse_sessions.max_session
    try:
        s.lakehouse_sessions.max_session = 1
        import time
        time.sleep(1.5)
        assert httpx.post(ex, data={**grant, "subject_token": tok["token"]}, timeout=10).json()["error"] == "invalid_grant"
    finally:
        s.lakehouse_sessions.max_session = m
    # Storage keys are renewed through the catalog, decided again each time: read-only for a reader,
    # refused to someone the table is not for, and reported to expire within the revalidation interval.
    import time
    cat = renewing_catalog["uri"]
    path = "/v1/namespaces/analytics/tables/products/credentials"
    ana = httpx.post(f"{url}/v1/lakehouse/token", headers={"Authorization": f"Bearer {ANA}"}, timeout=10).json()["token"]
    r = httpx.get(cat + path, headers={"Authorization": f"Bearer {ana}"}, timeout=10)
    assert r.status_code == 200, r.text
    cfg = r.json()["storage-credentials"][0]["config"]
    assert cfg["s3.session-token"] and int(cfg["s3.session-token-expires-at-ms"]) <= (time.time() + 360 + 5) * 1000
    # Loading the table tells the client where to renew (in its config and its storage credential).
    loaded = httpx.get(cat + "/v1/namespaces/analytics/tables/products", headers={"Authorization": f"Bearer {ana}"}, timeout=10).json()
    endpoint = loaded["config"]["client.refresh-credentials-endpoint"]
    assert endpoint == "v1/namespaces/analytics/tables/products/credentials"
    assert loaded["storage-credentials"][0]["config"]["client.refresh-credentials-endpoint"] == endpoint
    bob = httpx.post(f"{url}/v1/lakehouse/token", headers={"Authorization": f"Bearer {BOB}"}, timeout=10).json()["token"]
    assert httpx.get(cat + path, headers={"Authorization": f"Bearer {bob}"}, timeout=10).status_code == 403
    # The catalog refuses a revalidation interval Iceberg clients could not honour.
    env = {**lake["env"], "CAIRN_CATALOG_ADDR": f"0.0.0.0:{port()}", "CAIRN_CATALOG_DATA": f"{lake['dir']}/c9",
           "CAIRN_CATALOG_VEND_SECONDS": "900", "CAIRN_CATALOG_VEND_REVALIDATE_SECONDS": "120"}
    r = subprocess.run([os.environ["CAIRN_CATALOG_BIN"]], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "CAIRN_CATALOG_VEND_REVALIDATE_SECONDS" in (r.stdout + r.stderr)
