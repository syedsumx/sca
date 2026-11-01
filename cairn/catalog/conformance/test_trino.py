"""Trino on Cairn Catalog and Cairn Store: Trino's Iceberg connector, pointed at the catalog's REST API
(bearer token) with its data in the store (native S3), creates schemas and tables, inserts, updates,
deletes and merges rows, time-travels by snapshot, evolves a schema, and reads what pyiceberg wrote (and
pyiceberg reads what Trino wrote). Token scopes hold for Trino too. Needs docker."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid

import pytest

from conftest import ADMIN_TOKEN, READER_TOKEN, STORE_KEY

TRINO = "trinodb/trino:476"


def docker_ok():
    return bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


pytestmark = pytest.mark.skipif(not docker_ok(), reason="needs docker (Trino)")


def catalog_props(stack, token):
    return "\n".join([
        "connector.name=iceberg",
        "iceberg.catalog.type=rest",
        f"iceberg.rest-catalog.uri={stack.uri}",
        "iceberg.rest-catalog.security=OAUTH2",
        f"iceberg.rest-catalog.oauth2.token={token}",
        "fs.native-s3.enabled=true",
        f"s3.endpoint={stack.store_url}",
        "s3.region=us-east-1",
        "s3.path-style-access=true",
        f"s3.aws-access-key={STORE_KEY[0]}",
        f"s3.aws-secret-key={STORE_KEY[1]}",
        "",
    ])


class Trino:
    """A Trino server in a container on the host network, with catalogs `lake` (admin token) and
    `lake_ro` (a read-only token)."""

    def __init__(self, stack):
        self.dir = tempfile.mkdtemp(prefix="trino-")
        os.makedirs(f"{self.dir}/catalog")
        open(f"{self.dir}/catalog/lake.properties", "w").write(catalog_props(stack, ADMIN_TOKEN))
        open(f"{self.dir}/catalog/lake_ro.properties", "w").write(catalog_props(stack, READER_TOKEN))
        os.chmod(self.dir, 0o755)
        self.port = 18000 + os.getpid() % 1000
        open(f"{self.dir}/config.properties", "w").write(
            f"coordinator=true\nnode-scheduler.include-coordinator=true\nhttp-server.http.port={self.port}\n"
            f"discovery.uri=http://127.0.0.1:{self.port}\n")
        self.name = f"cairn-trino-{os.getpid()}"
        subprocess.run(["docker", "run", "-d", "--rm", "--name", self.name, "--network", "host",
                        "-v", f"{self.dir}/catalog:/etc/trino/catalog:ro",
                        "-v", f"{self.dir}/config.properties:/etc/trino/config.properties:ro", TRINO],
                       check=True, capture_output=True, timeout=600)
        end = time.time() + 240
        while time.time() < end:
            logs = subprocess.run(["docker", "logs", self.name], capture_output=True, text=True).stderr
            if "SERVER STARTED" in logs:
                return
            if "Error injecting constructor" in logs or "Configuration errors" in logs:
                raise AssertionError(logs[-3000:])
            time.sleep(1)
        raise AssertionError("Trino did not start")

    def sql(self, statement, catalog="lake"):
        out = subprocess.run(["docker", "exec", self.name, "trino", "--server", f"http://127.0.0.1:{self.port}",
                              "--catalog", catalog, "--output-format", "JSON", "--execute", statement],
                             capture_output=True, text=True, timeout=300)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip() or out.stdout.strip())
        return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]

    def stop(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, timeout=60)
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture(scope="module")
def trino(stack):
    t = Trino(stack)
    yield t
    t.stop()


@pytest.fixture
def schema(trino):
    name = "t_" + uuid.uuid4().hex[:8]
    trino.sql(f"CREATE SCHEMA lake.{name}")
    return name


def test_sql_writes_reads_and_time_travel(trino, schema):
    t = f"lake.{schema}.orders"
    trino.sql(f"CREATE TABLE {t} (id BIGINT, customer VARCHAR, amount DECIMAL(10,2), day DATE) "
              "WITH (partitioning = ARRAY['day'], format = 'PARQUET')")
    trino.sql(f"INSERT INTO {t} VALUES (1, 'acme', 10.50, DATE '2026-10-01'), (2, 'globex', 99.99, DATE '2026-10-01'), "
              "(3, 'acme', 5.00, DATE '2026-10-02')")
    first = trino.sql(f'SELECT snapshot_id FROM {schema}."orders$snapshots" ORDER BY committed_at')[-1]["snapshot_id"]
    trino.sql(f"UPDATE {t} SET amount = amount + 1 WHERE customer = 'acme'")
    trino.sql(f"DELETE FROM {t} WHERE id = 2")
    trino.sql(f"MERGE INTO {t} o USING (VALUES (3, 'acme', 7.00), (4, 'initech', 1.25)) AS s(id, customer, amount) ON o.id = s.id "
              "WHEN MATCHED THEN UPDATE SET amount = s.amount "
              "WHEN NOT MATCHED THEN INSERT (id, customer, amount, day) VALUES (s.id, s.customer, s.amount, DATE '2026-10-03')")
    rows = trino.sql(f"SELECT id, customer, CAST(amount AS VARCHAR) AS amount FROM {t} ORDER BY id")
    assert rows == [{"id": 1, "customer": "acme", "amount": "11.50"}, {"id": 3, "customer": "acme", "amount": "7.00"},
                    {"id": 4, "customer": "initech", "amount": "1.25"}]
    # Time travel to the first snapshot; partitions as Iceberg keeps them.
    old = trino.sql(f"SELECT count(*) AS n FROM {t} FOR VERSION AS OF {first}")
    assert old == [{"n": 3}]
    parts = trino.sql(f'SELECT count(*) AS n FROM {schema}."orders$partitions"')
    assert parts[0]["n"] == 3
    # Schema evolution.
    trino.sql(f"ALTER TABLE {t} ADD COLUMN channel VARCHAR")
    trino.sql(f"INSERT INTO {t} VALUES (5, 'umbrella', 3.00, DATE '2026-10-03', 'web')")
    assert trino.sql(f"SELECT channel FROM {t} WHERE id = 5") == [{"channel": "web"}]
    assert trino.sql(f"SELECT count(*) AS n FROM {t} WHERE channel IS NULL") == [{"n": 3}]
    # Table maintenance through the catalog.
    trino.sql(f"ALTER TABLE {t} EXECUTE optimize")
    assert trino.sql(f"SELECT count(*) AS n FROM {t}") == [{"n": 4}]


def test_trino_and_pyiceberg_share_tables(trino, schema, cat):
    import pyarrow as pa

    # pyiceberg writes; Trino reads.
    from pyiceberg.schema import Schema
    from pyiceberg.types import LongType, NestedField, StringType

    tbl = cat.create_table(f"{schema}.events", Schema(NestedField(1, "id", LongType(), required=False),
                                                     NestedField(2, "kind", StringType(), required=False)))
    tbl.append(pa.table({"id": pa.array([1, 2, 3], pa.int64()), "kind": pa.array(["view", "click", "view"])}))
    assert trino.sql(f"SELECT kind, count(*) AS n FROM lake.{schema}.events GROUP BY kind ORDER BY kind") == [
        {"kind": "click", "n": 1}, {"kind": "view", "n": 2}]
    # Trino writes; pyiceberg reads the new snapshot.
    trino.sql(f"INSERT INTO lake.{schema}.events VALUES (4, 'buy')")
    back = cat.load_table(f"{schema}.events").scan().to_arrow()
    assert sorted(back.column("kind").to_pylist()) == ["buy", "click", "view", "view"]
    # A CTAS in Trino is a table every engine sees.
    trino.sql(f"CREATE TABLE lake.{schema}.kinds AS SELECT kind, count(*) AS n FROM lake.{schema}.events GROUP BY kind")
    assert sorted(r["kind"] for r in cat.load_table(f"{schema}.kinds").scan().to_arrow().to_pylist()) == ["buy", "click", "view"]


def test_token_scopes_hold_for_trino(trino, schema):
    trino.sql(f"CREATE TABLE lake.{schema}.facts AS SELECT 1 AS x")
    # The read-only token reads...
    assert trino.sql(f"SELECT x FROM lake_ro.{schema}.facts") == [{"x": 1}]
    # ...and is refused every change, by the catalog.
    for stmt in [f"INSERT INTO lake_ro.{schema}.facts VALUES (2)", f"CREATE TABLE lake_ro.{schema}.other (a INT)",
                 f"DROP TABLE lake_ro.{schema}.facts"]:
        with pytest.raises(RuntimeError):  # Trino reports the catalog's 403 in its own words
            trino.sql(stmt)
    # Nothing changed.
    assert trino.sql(f"SELECT count(*) AS n FROM lake.{schema}.facts") == [{"n": 1}]
    assert [r["Table"] for r in trino.sql(f"SHOW TABLES FROM lake.{schema}")] == ["facts"]


def test_views(trino, schema, cat):
    trino.sql(f"CREATE TABLE lake.{schema}.sales AS SELECT * FROM "
              "(VALUES (1, 'EU', 10), (2, 'US', 20), (3, 'EU', 5)) t(id, region, amount)")
    trino.sql(f"CREATE VIEW lake.{schema}.eu AS SELECT id, amount FROM lake.{schema}.sales WHERE region = 'EU'")
    assert trino.sql(f"SELECT sum(amount) AS s FROM lake.{schema}.eu") == [{"s": 15}]
    assert {r["Table"] for r in trino.sql(f"SHOW TABLES FROM lake.{schema}")} == {"sales", "eu"}
    # Replacing the view adds a version; queries see the new definition.
    trino.sql(f"CREATE OR REPLACE VIEW lake.{schema}.eu AS SELECT id, amount * 2 AS amount FROM lake.{schema}.sales WHERE region = 'EU'")
    assert trino.sql(f"SELECT sum(amount) AS s FROM lake.{schema}.eu") == [{"s": 30}]
    assert "amount * 2" in trino.sql(f"SHOW CREATE VIEW lake.{schema}.eu")[0]["Create View"]
    trino.sql(f"COMMENT ON VIEW lake.{schema}.eu IS 'EU sales'")
    # Every engine sees it: pyiceberg lists it as a view.
    assert (schema, "eu") in [tuple(v) for v in cat.list_views(schema)]
    trino.sql(f"ALTER VIEW lake.{schema}.eu RENAME TO lake.{schema}.europe")
    assert trino.sql(f"SELECT count(*) AS n FROM lake.{schema}.europe") == [{"n": 2}]
    # A view cannot take a table's name, nor a table a view's.
    with pytest.raises(RuntimeError):
        trino.sql(f"CREATE VIEW lake.{schema}.sales AS SELECT 1 AS x")
    with pytest.raises(RuntimeError):
        trino.sql(f"CREATE TABLE lake.{schema}.europe AS SELECT 1 AS x")
    # The read-only token reads views and changes none.
    assert trino.sql(f"SELECT count(*) AS n FROM lake_ro.{schema}.europe") == [{"n": 2}]
    with pytest.raises(RuntimeError):
        trino.sql(f"DROP VIEW lake_ro.{schema}.europe")
    trino.sql(f"DROP VIEW lake.{schema}.europe")
    assert {r["Table"] for r in trino.sql(f"SHOW TABLES FROM lake.{schema}")} == {"sales"}
