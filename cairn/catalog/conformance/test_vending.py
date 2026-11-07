"""Scoped credentials: with CAIRN_CATALOG_VEND_ROLE the catalog hands each engine temporary keys for one
table (from the store's STS, narrowed to the table's location; read-only for read-only tokens), so
engines hold no store key at all. pyiceberg and Trino run with nothing but the catalog token."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid

import boto3
import pytest
import requests
from botocore.config import Config
from botocore.exceptions import ClientError

from conftest import ADMIN_TOKEN, READER_TOKEN, STORE_KEY, Stack

STS = {"CAIRN_STORE_STS_SECRET": "catalog-sts-" + "s" * 32}
TRINO = os.environ.get("CAIRN_TRINO_IMAGE", "trinodb/trino:484")   # the upgrade matrix tries newer ones
_TV = int(TRINO.rsplit(":", 1)[-1]) if TRINO.rsplit(":", 1)[-1].isdigit() else 484
S3_ON = "fs.s3.enabled=true" if _TV >= 484 else "fs.native-s3.enabled=true"   # renamed in Trino 484


@pytest.fixture(scope="module")
def scoped():
    with tempfile.TemporaryDirectory() as d:
        st = Stack(d, store_env=STS, catalog_env={"CAIRN_CATALOG_VEND_ROLE": "cairn-catalog-tables"}).start()
        yield st
        st.stop()


def load(st, ns, table, token=ADMIN_TOKEN):
    r = requests.get(f"{st.uri}/v1/namespaces/{ns}/tables/{table}", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


def s3_with(st, cfg):
    return boto3.client("s3", endpoint_url=st.store_url, aws_access_key_id=cfg["s3.access-key-id"],
                        aws_secret_access_key=cfg["s3.secret-access-key"], aws_session_token=cfg["s3.session-token"],
                        region_name="us-east-1", config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 1}))


def code(fn, *a, **kw):
    with pytest.raises(ClientError) as ei:
        fn(*a, **kw)
    return ei.value.response["Error"]["Code"]


def make_table(cat, ns, name):
    import pyarrow as pa
    from pyiceberg.schema import Schema
    from pyiceberg.types import LongType, NestedField, StringType

    t = cat.create_table(f"{ns}.{name}", Schema(NestedField(1, "id", LongType(), required=False),
                                               NestedField(2, "v", StringType(), required=False)))
    t.append(pa.table({"id": pa.array([1, 2], pa.int64()), "v": pa.array(["a", "b"])}))
    return t


def test_engines_get_keys_for_one_table_only(scoped):
    cat = scoped.catalog()
    ns = "v_" + uuid.uuid4().hex[:6]
    cat.create_namespace(ns)
    a, b = make_table(cat, ns, "a"), make_table(cat, ns, "b")
    # pyiceberg wrote and reads them with the vended keys alone (the catalog object has no store keys).
    assert sorted(a.scan().to_arrow().column("v").to_pylist()) == ["a", "b"]
    out = load(scoped, ns, "a")
    cfg = out["config"]
    assert cfg["s3.access-key-id"].startswith("ASIA") and cfg["s3.access-key-id"] != STORE_KEY[0]
    assert "s3.session-token" in cfg and out["storage-credentials"][0]["prefix"] == a.location()
    s3 = s3_with(scoped, cfg)
    bucket, prefix = a.location().removeprefix("s3://").split("/", 1)
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix=prefix + "/")["Contents"]]
    assert keys and all(k.startswith(prefix + "/") for k in keys)
    assert s3.get_object(Bucket=bucket, Key=keys[0])["Body"].read()
    s3.put_object(Bucket=bucket, Key=f"{prefix}/data/extra.txt", Body=b"x")
    # Not another table's files, not the rest of the bucket.
    other = b.location().removeprefix("s3://").split("/", 1)[1]
    assert code(s3.list_objects_v2, Bucket=bucket, Prefix=other + "/") == "AccessDenied"
    other_key = [o["Key"] for o in boto3.client("s3", endpoint_url=scoped.store_url, aws_access_key_id=STORE_KEY[0],
                                                aws_secret_access_key=STORE_KEY[1], region_name="us-east-1",
                                                config=Config(s3={"addressing_style": "path"})).list_objects_v2(
        Bucket=bucket, Prefix=other + "/")["Contents"]][0]
    assert code(s3.get_object, Bucket=bucket, Key=other_key) == "AccessDenied"
    assert code(s3.list_objects_v2, Bucket=bucket) == "AccessDenied"
    assert code(s3.create_bucket, Bucket="anything") == "AccessDenied"
    # Keys are reused while fresh (one STS call per table, not per load).
    assert load(scoped, ns, "a")["config"]["s3.access-key-id"] == cfg["s3.access-key-id"]


def test_read_only_tokens_get_read_only_keys(scoped):
    cat = scoped.catalog()
    ns = "r_" + uuid.uuid4().hex[:6]
    cat.create_namespace(ns)
    t = make_table(cat, ns, "t")
    cfg = load(scoped, ns, "t", READER_TOKEN)["config"]
    s3 = s3_with(scoped, cfg)
    bucket, prefix = t.location().removeprefix("s3://").split("/", 1)
    key = [o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix=prefix + "/")["Contents"]][0]
    assert s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    assert code(s3.put_object, Bucket=bucket, Key=f"{prefix}/data/no.txt", Body=b"x") == "AccessDenied"
    assert code(s3.delete_object, Bucket=bucket, Key=key) == "AccessDenied"
    assert sorted(scoped.catalog(READER_TOKEN).load_table(f"{ns}.t").scan().to_arrow().column("id").to_pylist()) == [1, 2]


def test_the_catalog_refuses_to_start_without_sts():
    with tempfile.TemporaryDirectory() as d:
        st = Stack(d, catalog_env={"CAIRN_CATALOG_VEND_ROLE": "cairn-catalog-tables"})
        with pytest.raises(RuntimeError):
            st.start()   # fails closed: never silently falls back to handing out its own key
        st.stop()


def docker_ok():
    return bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


@pytest.mark.skipif(not docker_ok(), reason="needs docker (Trino)")
def test_trino_needs_no_store_key(scoped):
    d = tempfile.mkdtemp(prefix="trino-vend-")
    os.makedirs(f"{d}/catalog")
    open(f"{d}/catalog/lake.properties", "w").write("\n".join([
        "connector.name=iceberg", "iceberg.catalog.type=rest", f"iceberg.rest-catalog.uri={scoped.uri}",
        "iceberg.rest-catalog.security=OAUTH2", f"iceberg.rest-catalog.oauth2.token={ADMIN_TOKEN}",
        "iceberg.rest-catalog.vended-credentials-enabled=true",
        S3_ON, f"s3.endpoint={scoped.store_url}", "s3.region=us-east-1", "s3.path-style-access=true", ""]))
    port = 18600 + os.getpid() % 300
    open(f"{d}/config.properties", "w").write(f"coordinator=true\nnode-scheduler.include-coordinator=true\n"
                                              f"http-server.http.port={port}\ndiscovery.uri=http://127.0.0.1:{port}\n")
    os.chmod(d, 0o755)
    name = f"trino-vend-{os.getpid()}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "host",
                    "-v", f"{d}/catalog:/etc/trino/catalog:ro", "-v", f"{d}/config.properties:/etc/trino/config.properties:ro", TRINO],
                   check=True, capture_output=True, timeout=600)
    try:
        end = time.time() + 240
        while "SERVER STARTED" not in subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr:
            assert time.time() < end, "Trino did not start"
            time.sleep(1)

        def sql(q):
            out = subprocess.run(["docker", "exec", name, "trino", "--server", f"http://127.0.0.1:{port}", "--catalog", "lake",
                                  "--output-format", "JSON", "--execute", q], capture_output=True, text=True, timeout=300)
            assert out.returncode == 0, out.stderr[-2000:]
            return [json.loads(x) for x in out.stdout.splitlines() if x.strip()]

        ns = "t_" + uuid.uuid4().hex[:6]
        sql(f"CREATE SCHEMA lake.{ns}")
        sql(f"CREATE TABLE lake.{ns}.o AS SELECT * FROM (VALUES (1, 'EU'), (2, 'US')) t(id, region)")
        sql(f"INSERT INTO lake.{ns}.o VALUES (3, 'EU')")
        sql(f"UPDATE lake.{ns}.o SET region = 'APAC' WHERE id = 2")
        sql(f"DELETE FROM lake.{ns}.o WHERE id = 3")
        sql(f"ALTER TABLE lake.{ns}.o EXECUTE optimize")
        assert sql(f"SELECT id, region FROM lake.{ns}.o ORDER BY id") == [{"id": 1, "region": "EU"}, {"id": 2, "region": "APAC"}]
        sql(f"CREATE TABLE lake.{ns}.by_region AS SELECT region, count(*) AS n FROM lake.{ns}.o GROUP BY region")
        assert len(sql(f"SELECT * FROM lake.{ns}.by_region")) == 2
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
        shutil.rmtree(d, ignore_errors=True)
