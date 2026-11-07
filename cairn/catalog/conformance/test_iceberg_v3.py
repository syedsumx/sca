"""Iceberg v3 tables on Cairn Catalog, written by Spark 4 with Iceberg 1.10 (the engine versions Cairn ships) and
read back by the other engines. Spark creates v3 tables and upgrades a v2 one. It deletes a row with a deletion
vector and reads row lineage (`_row_id`). It stores VARIANT values, which need the catalog to understand v3 types,
and reads its metadata tables (`t.snapshots`), which need the catalog to answer "no such table" for
`<ns>.<table>` as a namespace. pyiceberg and Trino then read the same table. Needs docker and the Spark image
(CAIRN_SPARK_BASE_IMAGE, default apache/spark 4.0.1); the Iceberg jars come from Maven Central."""

import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import urllib.request

import pytest

from conftest import ADMIN_TOKEN, STORE_KEY
from test_trino import TRINO, Trino, docker_ok

SPARK = os.environ.get("CAIRN_SPARK_BASE_IMAGE", "apache/spark:4.0.1-scala2.13-java17-python3-ubuntu")
ICEBERG = os.environ.get("CAIRN_ICEBERG_SPARK_VERSION", "1.10.0")
MAVEN = "https://repo1.maven.org/maven2/org/apache/iceberg"
JARS = [f"{MAVEN}/iceberg-spark-runtime-4.0_2.13/{ICEBERG}/iceberg-spark-runtime-4.0_2.13-{ICEBERG}.jar",
        f"{MAVEN}/iceberg-aws-bundle/{ICEBERG}/iceberg-aws-bundle-{ICEBERG}.jar"]

pytestmark = pytest.mark.skipif(not docker_ok(), reason="needs docker (Spark, Trino)")

JOB = textwrap.dedent('''
    import json, sys
    from pyspark.sql import SparkSession
    uri, store, token, key, secret = sys.argv[1:6]
    spark = (SparkSession.builder
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
        .config("spark.sql.catalog.lake", "org.apache.iceberg.spark.SparkCatalog")
        .config("spark.sql.catalog.lake.type", "rest").config("spark.sql.catalog.lake.uri", uri)
        .config("spark.sql.catalog.lake.token", token)
        .config("spark.sql.catalog.lake.io-impl", "org.apache.iceberg.aws.s3.S3FileIO")
        .config("spark.sql.catalog.lake.s3.endpoint", store).config("spark.sql.catalog.lake.s3.path-style-access", "true")
        .config("spark.sql.catalog.lake.s3.access-key-id", key).config("spark.sql.catalog.lake.s3.secret-access-key", secret)
        .config("spark.sql.catalog.lake.client.region", "us-east-1").getOrCreate())
    q = lambda s: [r.asDict(recursive=True) for r in spark.sql(s).collect()]
    out = {}
    spark.sql("CREATE NAMESPACE IF NOT EXISTS lake.v3")
    spark.sql("CREATE TABLE lake.v3.orders (id BIGINT, region STRING) USING iceberg "
              "TBLPROPERTIES ('format-version'='3', 'write.delete.mode'='merge-on-read')")
    # One data file, so deleting one row needs a deletion vector rather than dropping a file.
    spark.range(1, 7).selectExpr("id", "CASE WHEN id % 2 = 0 THEN 'emea' ELSE 'amer' END AS region") \\
        .coalesce(1).writeTo("lake.v3.orders").append()
    spark.sql("DELETE FROM lake.v3.orders WHERE id = 2")
    out["rows"] = q("SELECT id FROM lake.v3.orders ORDER BY id")
    out["lineage"] = q("SELECT id, _row_id FROM lake.v3.orders ORDER BY id")
    out["snapshots"] = q("SELECT operation, summary FROM lake.v3.orders.snapshots ORDER BY committed_at")
    out["delete_files"] = q("SELECT content, file_format, file_path FROM lake.v3.orders.delete_files")
    spark.sql("CREATE TABLE lake.v3.events (id BIGINT, payload VARIANT) USING iceberg TBLPROPERTIES ('format-version'='3')")
    spark.sql("""INSERT INTO lake.v3.events SELECT 1, parse_json('{"user": {"id": 7}, "tags": ["a"]}')""")
    out["variant"] = q("SELECT id, variant_get(payload, '$.user.id', 'int') AS uid FROM lake.v3.events")
    spark.sql("CREATE TABLE lake.v3.legacy (id BIGINT) USING iceberg TBLPROPERTIES ('format-version'='2')")
    spark.sql("INSERT INTO lake.v3.legacy VALUES (1)")
    spark.sql("ALTER TABLE lake.v3.legacy SET TBLPROPERTIES ('format-version'='3')")
    spark.sql("INSERT INTO lake.v3.legacy VALUES (2)")
    out["legacy"] = q("SELECT id, _row_id FROM lake.v3.legacy ORDER BY id")
    print("RESULT " + json.dumps(out, default=str))
''')


@pytest.fixture(scope="module")
def jars():
    d = os.path.join(os.environ.get("CAIRN_TEST_CACHE", tempfile.gettempdir()), f"iceberg-spark-{ICEBERG}")
    os.makedirs(d, exist_ok=True)
    for url in JARS:
        path = os.path.join(d, url.rsplit("/", 1)[1])
        if not os.path.exists(path):
            urllib.request.urlretrieve(url, path + ".part")
            os.replace(path + ".part", path)
    return d


@pytest.fixture(scope="module")
def spark_run(stack, jars):
    job = tempfile.mkdtemp(prefix="v3-job-")
    open(os.path.join(job, "job.py"), "w").write(JOB)
    os.chmod(job, 0o755)
    names = ",".join(f"/jars/{u.rsplit('/', 1)[1]}" for u in JARS)
    r = subprocess.run(["docker", "run", "--rm", "--network", "host", "-v", f"{jars}:/jars:ro", "-v", f"{job}:/job:ro",
                        SPARK, "/opt/spark/bin/spark-submit", "--jars", names, "--conf", "spark.jars.ivy=/tmp/ivy",
                        "/job/job.py", stack.uri, stack.store_url, ADMIN_TOKEN, STORE_KEY[0], STORE_KEY[1]],
                       capture_output=True, text=True, timeout=1200)
    shutil.rmtree(job, ignore_errors=True)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, (r.stdout[-3000:], r.stderr[-6000:])
    import json
    return json.loads(line[len("RESULT "):])


def test_spark_writes_v3_tables_through_the_catalog(spark_run, cat):
    out = spark_run
    assert [r["id"] for r in out["rows"]] == [1, 3, 4, 5, 6]
    # Row lineage: every row keeps the id it was given when first written, deletes leave gaps.
    assert [r["_row_id"] for r in out["lineage"]] == [0, 2, 3, 4, 5]
    # The delete wrote a deletion vector (a Puffin file), not a rewrite or a positional delete file.
    assert [s["operation"] for s in out["snapshots"]] == ["append", "delete"]
    assert [(f["content"], f["file_format"]) for f in out["delete_files"]] == [(1, "PUFFIN")]
    assert out["variant"] == [{"id": 1, "uid": 7}]
    # A v2 table upgraded in place: rows written before the upgrade get ids on the first v3 write.
    assert [r["id"] for r in out["legacy"]] == [1, 2] and all(r["_row_id"] is not None for r in out["legacy"])
    t = cat.load_table("v3.orders")
    assert t.metadata.format_version == 3 and cat.load_table("v3.legacy").metadata.format_version == 3
    assert t.metadata.model_dump(by_alias=True).get("next-row-id", 0) >= 6


def test_other_engines_read_what_spark_wrote(spark_run, stack, cat):
    # pyiceberg reads the v3 table, deletion vector applied.
    rows = sorted(cat.load_table("v3.orders").scan().to_arrow().column("id").to_pylist())
    assert rows == [1, 3, 4, 5, 6]
    trino = Trino(stack, "v3-")
    try:
        for _ in range(60):                              # the worker registers a moment after the server starts
            try:
                trino.sql("SELECT 1")
                break
            except RuntimeError as e:
                if "No nodes available" not in str(e):
                    raise
                time.sleep(1)
        # A v3 table without deletion vectors or VARIANT columns (copy-on-write, Iceberg's default delete mode):
        # Trino reads it, so it stays queryable through Cairn's governed SQL.
        assert [r["id"] for r in trino.sql("SELECT id FROM lake.v3.legacy ORDER BY id")] == [1, 2]
        tag = TRINO.rsplit(":", 1)[-1]
        if tag.isdigit() and int(tag) >= 480:
            # Trino reads the deletion vector (never the deleted row) and VARIANT columns,
            assert [r["id"] for r in trino.sql("SELECT id FROM lake.v3.orders ORDER BY id")] == [1, 3, 4, 5, 6]
            assert [r["id"] for r in trino.sql("SELECT id FROM lake.v3.events")] == [1]
            assert [r["v"] for r in trino.sql("SELECT json_extract_scalar(CAST(payload AS JSON), '$.user.id') AS v "
                                              "FROM lake.v3.events")] == ["7"]
            # and writes v3: a delete as a deletion vector, and an insert.
            trino.sql("DELETE FROM lake.v3.orders WHERE id = 3")
            trino.sql("INSERT INTO lake.v3.orders VALUES (10, 'apac')")
            assert [r["id"] for r in trino.sql("SELECT id FROM lake.v3.orders ORDER BY id")] == [1, 4, 5, 6, 10]
            assert sorted(cat.load_table("v3.orders").scan().to_arrow().column("id").to_pylist()) == [1, 4, 5, 6, 10]
        else:
            # An older Trino fails such a query with its own error and returns no rows: never the deleted row.
            for q, err in (("SELECT id FROM lake.v3.orders ORDER BY id", "PUFFIN"),
                           ("SELECT id FROM lake.v3.events", "variant")):
                with pytest.raises(RuntimeError, match=err):
                    trino.sql(q)
    finally:
        trino.stop()


READ = textwrap.dedent('''
    import json, sys
    from pyspark.sql import SparkSession
    uri, store, token, key, secret, query = sys.argv[1:7]
    spark = (SparkSession.builder
        .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
        .config("spark.sql.catalog.lake", "org.apache.iceberg.spark.SparkCatalog")
        .config("spark.sql.catalog.lake.type", "rest").config("spark.sql.catalog.lake.uri", uri)
        .config("spark.sql.catalog.lake.token", token)
        .config("spark.sql.catalog.lake.io-impl", "org.apache.iceberg.aws.s3.S3FileIO")
        .config("spark.sql.catalog.lake.s3.endpoint", store).config("spark.sql.catalog.lake.s3.path-style-access", "true")
        .config("spark.sql.catalog.lake.s3.access-key-id", key).config("spark.sql.catalog.lake.s3.secret-access-key", secret)
        .config("spark.sql.catalog.lake.client.region", "us-east-1").getOrCreate())
    print("RESULT " + json.dumps([r.asDict(recursive=True) for r in spark.sql(query).collect()], default=str))
''')


def spark_sql(stack, jars, query):
    job = tempfile.mkdtemp(prefix="v3-read-")
    open(os.path.join(job, "job.py"), "w").write(READ)
    os.chmod(job, 0o755)
    names = ",".join(f"/jars/{u.rsplit('/', 1)[1]}" for u in JARS)
    r = subprocess.run(["docker", "run", "--rm", "--network", "host", "-v", f"{jars}:/jars:ro", "-v", f"{job}:/job:ro",
                        SPARK, "/opt/spark/bin/spark-submit", "--jars", names, "--conf", "spark.jars.ivy=/tmp/ivy",
                        "/job/job.py", stack.uri, stack.store_url, ADMIN_TOKEN, STORE_KEY[0], STORE_KEY[1], query],
                       capture_output=True, text=True, timeout=1200)
    shutil.rmtree(job, ignore_errors=True)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, (r.stdout[-3000:], r.stderr[-6000:])
    import json
    return json.loads(line[len("RESULT "):])


def test_cairn_writes_v3_with_row_lineage_spark_reads(spark_run, stack, cat, jars):
    """Cairn's Python writers (pyiceberg with cairn.pipelines.iceberg_v3) append to the v3 table Spark made: the new
    rows take the next row ids, the rows already there keep theirs, and Spark reads both."""
    import pyarrow as pa

    from cairn.pipelines.iceberg_v3 import enable

    enable()
    t = cat.load_table("v3.orders")
    start = t.metadata.next_row_id
    before = {r["id"]: r["_row_id"] for r in spark_sql(stack, jars, "SELECT id, _row_id FROM lake.v3.orders")}
    t.append(pa.table({"id": pa.array([20, 21], pa.int64()), "region": pa.array(["apac", "apac"])}))
    after = {r["id"]: r["_row_id"] for r in spark_sql(stack, jars, "SELECT id, _row_id FROM lake.v3.orders")}
    assert {k: after[k] for k in before} == before                      # the rows already there keep their ids
    assert sorted([after[20], after[21]]) == [start, start + 1]           # the new rows take the next ones
    assert len(set(after.values())) == len(after)
    assert cat.load_table("v3.orders").metadata.next_row_id == start + 2
