"""The connector gallery, proven (L168, part 1): each connector that can run against a real system here is set up through
its gallery form (``build_recipe``, as the Sources page does), and its recipe is run by the ingestion framework the
executor runs (acryl-datahub, the exact version and extras of ``docker/agent.Dockerfile``), against the real system in Docker.
Secrets stay ``${NAME}`` references in the recipe and are given to the run through its environment, as the executor
resolves them. Each test reads what the run emitted and checks the assets, their columns and their lineage:

- PostgreSQL 16, MySQL 8.4, SQL Server 2022, Trino 484 (password sign-in over HTTPS): schemas, tables with their
  columns, views and each view's lineage to its table; a schema left out of the form's filter is not read;
- MongoDB 7: databases and collections, with columns inferred from the documents;
- Kafka (Redpanda, with its schema registry): topics, and a topic's Avro schema as its columns;
- Amazon S3 (Cairn Store, the S3-compatible store): Parquet datasets from a path spec, with their columns;
- MLflow 3.5: registered models and their versions;
- dbt Core 1.12 (a project run for real on PostgreSQL): models, their columns, and lineage from the source table;
- Metabase 0.58 on PostgreSQL: a dashboard, its question, and the question's lineage to the table its SQL reads, column
  by column;
- the executor image: every gallery connector's source type is among its extras, and each loads in the environment
  built from them.

The ingestion framework needs Python 3.12 (its extras do not build on 3.13): ``CAIRN_INGEST_PYTHON`` names that
interpreter, with ``acryl-datahub[...]`` installed. The other gallery connectors are SaaS services (Snowflake, BigQuery,
Databricks, Redshift, dbt Cloud, Looker, Tableau, Power BI, Preset, Sigma, Qlik, SageMaker, Vertex AI, Glue): no
account here, so they are not run (their forms are checked in ``test_sources.py``)."""

import io
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time

import pytest
import yaml

INGEST_PYTHON = os.environ.get("CAIRN_INGEST_PYTHON", "")   # CAIRN_DBT_BIN: dbt with dbt-postgres, for test_dbt_core
IMAGES = {"postgres": "postgres:16-alpine", "mysql": "mysql:8.4", "mssql": "mcr.microsoft.com/mssql/server:2022-latest",
          "mongodb": "mongo:7.0", "trino": "trinodb/trino:484", "kafka": "redpandadata/redpanda:v25.2.9",
          "mlflow": "ghcr.io/mlflow/mlflow:v3.5.0", "metabase": "metabase/metabase:v0.58.40.1",
          "clickhouse": "clickhouse/clickhouse-server:25.8.33.6-alpine", "oracle": "gvenzl/oracle-free:23.26.3-slim",
          "dynamodb": "amazon/dynamodb-local:3.3.2", "hive": "apache/hive:4.0.1"}


def _docker() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(not INGEST_PYTHON or not os.path.exists(INGEST_PYTHON) or not _docker(),
                                reason="needs docker and CAIRN_INGEST_PYTHON (Python 3.12 with acryl-datahub[...])")


# CAIRN_GALLERY_PULL=1 (CI): each test pulls its image when it starts and removes it when it ends, so the images of
# every connector are never on disk at once (they come to over 15 GB). Without it, a missing image skips its test.
PULL = os.environ.get("CAIRN_GALLERY_PULL") == "1"
KEEP = {"postgres:16-alpine", "trinodb/trino:484"}   # used by several tests, and by later CI steps


def needs(image: str):
    return pytest.mark.skipif(not PULL and subprocess.run(["docker", "image", "inspect", image],
                                                          capture_output=True).returncode != 0,
                              reason=f"needs the {image} image")


def ensure(image: str) -> None:
    """The image, pulled with retries when it is missing (a registry's hiccup is not a connector's failure)."""
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0:
        return
    for attempt in range(3):
        if subprocess.run(["docker", "pull", "-q", image], capture_output=True, timeout=900).returncode == 0:
            return
        time.sleep(15 * (attempt + 1))
    raise RuntimeError(f"could not pull {image}")


def release(image: str) -> None:
    if PULL and image not in KEEP:
        subprocess.run(["docker", "rmi", "-f", image], capture_output=True)


def port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Container:
    def __init__(self, image: str, ports: dict, env: dict = None, args: list = None, name: str = "gallery",
                 network: str = None):
        self.name, self.image = f"cairn-{name}-{secrets.token_hex(4)}", image
        ensure(image)
        cmd = ["docker", "run", "-d", "--rm", "--name", self.name] + (["--network", network] if network else [])
        for inside, outside in ports.items():
            cmd += ["-p", f"127.0.0.1:{outside}:{inside}"]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        subprocess.run(cmd + [image] + (args or []), check=True, capture_output=True)

    def exec(self, *cmd, input=None, check=True):
        out = subprocess.run(["docker", "exec", "-i", self.name, *cmd], input=input, capture_output=True, text=True,
                             timeout=300)
        if check and out.returncode != 0:
            raise RuntimeError(f"{cmd}: {out.stdout[-1500:]} {out.stderr[-1500:]}")
        return out.stdout

    def stop(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
        release(self.image)


def until(check, seconds=180, what="ready"):
    end = time.time() + seconds
    while True:
        try:
            if check():
                return
        except Exception:  # noqa: BLE001 - not ready yet
            if time.time() > end:
                raise
        if time.time() > end:
            raise TimeoutError(what)
        time.sleep(2)


def ingest(connector: str, values: dict, secrets_env: dict, advanced: dict = None, env: dict = None) -> list[dict]:
    """The recipe from the gallery form, run with the ingestion framework and a file sink; the events it wrote."""
    from cairn.sources.catalog import build_recipe

    recipe = build_recipe(connector, values, advanced)
    text = json.dumps(recipe)
    for name in secrets_env:                                 # the form keeps secrets as references, never values
        assert "${" + name + "}" in text and secrets_env[name] not in text
    d = tempfile.mkdtemp(prefix="gallery-")
    recipe["sink"] = {"type": "file", "config": {"filename": f"{d}/out.json"}}
    with open(f"{d}/recipe.yml", "w") as f:
        yaml.safe_dump(recipe, f)
    run = subprocess.run([INGEST_PYTHON, "-m", "datahub", "ingest", "-c", f"{d}/recipe.yml"], capture_output=True,
                         text=True, timeout=600, cwd=d,
                         env={**os.environ, **secrets_env, **(env or {}), "DATAHUB_TELEMETRY_ENABLED": "false"})
    log = run.stdout + run.stderr
    assert run.returncode == 0 and "Pipeline finished" in log, log[-4000:]
    assert "'failures': [" not in log.replace("'failures': []", ""), log[-4000:]
    with open(f"{d}/out.json") as f:
        return json.load(f)


def aspects(events: list[dict]) -> dict:
    """``{urn: {aspect name: value}}`` from the file sink's events (proposals and snapshots alike)."""
    out: dict = {}
    for e in events:
        if "entityUrn" in e and "aspectName" in e:
            v = e.get("aspect", {})
            if isinstance(v, dict) and "json" in v:
                v = v["json"]
            elif isinstance(v, dict) and isinstance(v.get("value"), str):
                v = json.loads(v["value"])
            out.setdefault(e["entityUrn"], {})[e["aspectName"]] = v
        elif "proposedSnapshot" in e:
            ((_, snap),) = e["proposedSnapshot"].items()
            for a in snap.get("aspects", []):
                ((cls, value),) = a.items()
                name = cls.rsplit(".", 1)[-1]
                name = name[0].lower() + name[1:].replace("Class", "")
                out.setdefault(snap["urn"], {})[name] = value
    return out


def datasets(got: dict, platform: str) -> dict:
    return {u.split(",")[1]: a for u, a in got.items() if u.startswith(f"urn:li:dataset:(urn:li:dataPlatform:{platform},")}


def columns(aspect: dict) -> list[str]:
    return [f["fieldPath"].split(".")[-1] for f in (aspect.get("schemaMetadata") or {}).get("fields", [])]


def upstreams(aspect: dict) -> list[str]:
    return [u["dataset"].split(",")[1] for u in (aspect.get("upstreamLineage") or {}).get("upstreams", [])]


# ---- relational databases ------------------------------------------------------------------------
@needs(IMAGES["postgres"])
def test_postgresql():
    p = port()
    db = Container(IMAGES["postgres"], {5432: p}, {"POSTGRES_PASSWORD": "pg-Secret-1"}, name="gallery-pg")
    try:
        until(lambda: "accepting" in subprocess.run(["docker", "exec", db.name, "pg_isready", "-U", "postgres"],
                                                    capture_output=True, text=True).stdout)
        until(lambda: db.exec("psql", "-U", "postgres", "-c", "SELECT 1") is not None)
        db.exec("psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", input="""
            CREATE SCHEMA sales; CREATE SCHEMA hr;
            CREATE TABLE sales.orders (id int PRIMARY KEY, amount numeric(10,2), placed timestamptz);
            CREATE VIEW sales.big_orders AS SELECT id, amount FROM sales.orders WHERE amount > 100;
            CREATE TABLE hr.salaries (id int, pay int);""")
        got = aspects(ingest("postgres", {"host_port": f"127.0.0.1:{p}", "database": "postgres",
                                          "username": "postgres", "password": "PG_PASSWORD", "schemas": ["sales"]},
                             {"PG_PASSWORD": "pg-Secret-1"}))
        ds = datasets(got, "postgres")
        assert set(ds) == {"postgres.sales.orders", "postgres.sales.big_orders"}, set(ds)   # hr was filtered out
        assert columns(ds["postgres.sales.orders"]) == ["id", "amount", "placed"]
        assert upstreams(ds["postgres.sales.big_orders"]) == ["postgres.sales.orders"]
    finally:
        db.stop()


@needs(IMAGES["mysql"])
def test_mysql():
    p = port()
    db = Container(IMAGES["mysql"], {3306: p}, {"MYSQL_ROOT_PASSWORD": "my-Secret-1", "MYSQL_DATABASE": "shop"},
                   name="gallery-mysql")
    try:
        until(lambda: db.exec("mysql", "-uroot", "-pmy-Secret-1", "-e", "SELECT 1", "shop") is not None, 240)
        db.exec("mysql", "-uroot", "-pmy-Secret-1", "shop", input="""
            CREATE TABLE customers (id INT PRIMARY KEY, name VARCHAR(50), born DATE);
            CREATE VIEW adults AS SELECT id, name FROM customers WHERE born < '2008-01-01';""")
        got = aspects(ingest("mysql", {"host_port": f"127.0.0.1:{p}", "database": "shop", "username": "root",
                                       "password": "MYSQL_PASSWORD", "schemas": ["shop"]},
                             {"MYSQL_PASSWORD": "my-Secret-1"}))
        ds = datasets(got, "mysql")
        assert {"shop.customers", "shop.adults"} <= set(ds), set(ds)
        assert columns(ds["shop.customers"]) == ["id", "name", "born"]
        assert upstreams(ds["shop.adults"]) == ["shop.customers"]
    finally:
        db.stop()


@needs(IMAGES["mssql"])
def test_sql_server():
    p = port()
    password = "Ms-Gallery-2026!"
    db = Container(IMAGES["mssql"], {1433: p}, {"ACCEPT_EULA": "Y", "MSSQL_SA_PASSWORD": password,
                                                "MSSQL_PID": "Developer"}, name="gallery-mssql")
    sqlcmd = ["/opt/mssql-tools18/bin/sqlcmd", "-S", "localhost", "-U", "sa", "-P", password, "-C", "-b"]
    try:
        until(lambda: db.exec(*sqlcmd, "-Q", "SELECT 1") is not None, 240)
        db.exec(*sqlcmd, "-Q", "CREATE DATABASE shop")
        db.exec(*sqlcmd, "-d", "shop", "-Q", "CREATE TABLE dbo.orders (id INT PRIMARY KEY, amount DECIMAL(10,2), "
                                             "note NVARCHAR(100))")
        db.exec(*sqlcmd, "-d", "shop", "-Q", "CREATE VIEW dbo.big_orders AS SELECT id, amount FROM dbo.orders "
                                             "WHERE amount > 100")
        got = aspects(ingest("mssql", {"host_port": f"127.0.0.1:{p}", "database": "shop", "username": "sa",
                                       "password": "MSSQL_PASSWORD"}, {"MSSQL_PASSWORD": password}))
        ds = datasets(got, "mssql")
        assert {"shop.dbo.orders", "shop.dbo.big_orders"} <= set(ds), set(ds)
        assert columns(ds["shop.dbo.orders"]) == ["id", "amount", "note"]
        assert upstreams(ds["shop.dbo.big_orders"]) == ["shop.dbo.orders"]
    finally:
        db.stop()


@needs(IMAGES["trino"])
def test_trino():
    """Trino with password sign-in, which Trino takes only over HTTPS: a certificate the run trusts (as an operator adds
    their CA), ``http_scheme`` through the form's advanced options, and a memory catalog with a table and a view."""
    import bcrypt

    d = tempfile.mkdtemp(prefix="gallery-trino-")
    os.makedirs(f"{d}/catalog")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
                    "-keyout", f"{d}/key.pem", "-out", f"{d}/cert.pem"], check=True, capture_output=True)
    with open(f"{d}/tls.pem", "w") as f:
        f.write(open(f"{d}/key.pem").read() + open(f"{d}/cert.pem").read())
    password = "trino-Secret-1"
    with open(f"{d}/password.db", "w") as f:
        f.write("analyst:" + bcrypt.hashpw(password.encode(), bcrypt.gensalt(10)).decode().replace("$2b$", "$2y$") + "\n")
    with open(f"{d}/password-authenticator.properties", "w") as f:
        f.write("password-authenticator.name=file\nfile.password-file=/etc/trino/password.db\n")
    with open(f"{d}/catalog/memory.properties", "w") as f:
        f.write("connector.name=memory\n")
    with open(f"{d}/config.properties", "w") as f:
        f.write("coordinator=true\nnode-scheduler.include-coordinator=true\nhttp-server.http.port=8080\n"
                "discovery.uri=http://localhost:8080\nhttp-server.https.enabled=true\nhttp-server.https.port=8443\n"
                "http-server.https.keystore.path=/etc/trino/tls.pem\nhttp-server.authentication.type=PASSWORD\n"
                "internal-communication.shared-secret=" + "s" * 32 + "\n")
    for name in os.listdir(d):
        os.chmod(os.path.join(d, name), 0o755 if os.path.isdir(os.path.join(d, name)) else 0o644)
    os.chmod(d, 0o755)
    p = port()
    name = f"cairn-gallery-trino-{secrets.token_hex(4)}"
    mounts = [x for f in ("config.properties", "password-authenticator.properties", "password.db", "tls.pem")
              for x in ("-v", f"{d}/{f}:/etc/trino/{f}:ro")] + ["-v", f"{d}/catalog:/etc/trino/catalog:ro"]
    ensure(IMAGES["trino"])
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{p}:8443", *mounts,
                    IMAGES["trino"]], check=True, capture_output=True)

    def sql(statement):
        out = subprocess.run(["docker", "exec", "-e", "TRINO_PASSWORD", name, "trino", "--server", "https://localhost:8443",
                              "--insecure", "--user", "analyst", "--password", "--execute", statement],
                             capture_output=True, text=True, timeout=120, env={**os.environ, "TRINO_PASSWORD": password})
        if out.returncode != 0:
            raise RuntimeError(out.stderr[-1000:])
        return out.stdout
    try:
        until(lambda: '"1"' in sql("SELECT 1") or "1" in sql("SELECT 1"), 240, "trino")
        sql("CREATE SCHEMA memory.sales")
        sql("CREATE TABLE memory.sales.orders (id bigint, amount double, region varchar)")
        sql("CREATE VIEW memory.sales.west AS SELECT id, amount FROM memory.sales.orders WHERE region = 'west'")
        got = aspects(ingest("trino", {"host_port": f"127.0.0.1:{p}", "database": "memory", "username": "analyst",
                                       "password": "TRINO_PASSWORD", "schemas": ["sales"]}, {"TRINO_PASSWORD": password},
                             advanced={"options": {"connect_args": {"http_scheme": "https"}}},
                             env={"REQUESTS_CA_BUNDLE": f"{d}/cert.pem"}))     # the server's CA, trusted
        ds = datasets(got, "trino")
        assert {"memory.sales.orders", "memory.sales.west"} <= set(ds), set(ds)
        assert columns(ds["memory.sales.orders"]) == ["id", "amount", "region"]
        assert upstreams(ds["memory.sales.west"]) == ["memory.sales.orders"]
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        release(IMAGES["trino"])


@needs(IMAGES["postgres"])
def test_dbt_core():
    """A dbt project run for real (dbt-core with dbt-postgres) against PostgreSQL: its manifest.json and catalog.json
    ingested through the dbt Core form give the models, their columns, and lineage from the source table through the
    models."""
    dbt = os.environ.get("CAIRN_DBT_BIN")
    if not dbt:
        pytest.skip("needs CAIRN_DBT_BIN (dbt with dbt-postgres)")
    p = port()
    db = Container(IMAGES["postgres"], {5432: p}, {"POSTGRES_PASSWORD": "dbt-Secret-1"}, name="gallery-dbt")
    proj = tempfile.mkdtemp(prefix="gallery-dbt-")
    try:
        until(lambda: db.exec("psql", "-U", "postgres", "-c", "SELECT 1") is not None)
        db.exec("psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", input="""
            CREATE SCHEMA raw; CREATE TABLE raw.orders (id int, amount numeric(10,2), region text);
            INSERT INTO raw.orders VALUES (1, 150, 'west'), (2, 20, 'east');""")
        os.makedirs(f"{proj}/models")
        files = {
            "dbt_project.yml": "name: shop\nversion: '1.0'\nprofile: shop\nmodel-paths: ['models']\n",
            "profiles.yml": (f"shop:\n  target: dev\n  outputs:\n    dev:\n      type: postgres\n      host: 127.0.0.1\n"
                             f"      port: {p}\n      user: postgres\n      password: \"{{{{ env_var('DBT_PG_PASSWORD') }}}}\"\n"
                             "      dbname: postgres\n      schema: analytics\n      threads: 1\n"),
            "models/sources.yml": "version: 2\nsources:\n  - name: raw\n    schema: raw\n    tables:\n      - name: orders\n",
            "models/big_orders.sql": "select id, amount from {{ source('raw', 'orders') }} where amount > 100",
            "models/big_order_count.sql": "select count(*) as n from {{ ref('big_orders') }}",
        }
        for name, text in files.items():
            with open(f"{proj}/{name}", "w") as f:
                f.write(text)
        env = {**os.environ, "DBT_PG_PASSWORD": "dbt-Secret-1", "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
        for cmd in (["run"], ["docs", "generate"]):
            out = subprocess.run([dbt, *cmd, "--project-dir", proj, "--profiles-dir", proj], capture_output=True,
                                 text=True, timeout=600, env=env)
            assert out.returncode == 0, out.stdout[-3000:]
        got = aspects(ingest("dbt", {"manifest_path": f"{proj}/target/manifest.json",
                                     "catalog_path": f"{proj}/target/catalog.json",
                                     "run_results_paths": [f"{proj}/target/run_results.json"],
                                     "target_platform": "postgres"}, {},
                             # PATCH (the default) merges into what the catalog holds, which a file sink has not.
                             advanced={"write_semantics": "OVERRIDE"}))
        ds = datasets(got, "dbt")
        assert {"postgres.analytics.big_orders", "postgres.analytics.big_order_count", "postgres.raw.orders"} <= set(ds)
        assert columns(ds["postgres.analytics.big_orders"]) == ["id", "amount"]
        assert upstreams(ds["postgres.analytics.big_order_count"]) == ["postgres.analytics.big_orders"]
        assert upstreams(ds["postgres.analytics.big_orders"]) == ["postgres.raw.orders"]
        # Each dbt node is tied to the warehouse table it builds (dbt's "siblings" to the postgres platform).
        pg = datasets(got, "postgres")
        assert "postgres.analytics.big_orders" in pg, set(pg)
    finally:
        db.stop()


@needs(IMAGES["clickhouse"])
def test_clickhouse():
    p = port()
    ch = Container(IMAGES["clickhouse"], {8123: p}, {"CLICKHOUSE_USER": "analyst", "CLICKHOUSE_PASSWORD": "ch-Secret-1",
                                                     "CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT": "1"}, name="gallery-ch")
    client = ["clickhouse-client", "--user", "analyst", "--password", "ch-Secret-1", "--multiquery", "--query"]
    try:
        until(lambda: ch.exec(*client, "SELECT 1") is not None, 120)
        ch.exec(*client, "CREATE DATABASE shop; CREATE TABLE shop.orders (id UInt64, amount Decimal(10,2), region String) "
                         "ENGINE = MergeTree ORDER BY id; CREATE VIEW shop.west AS SELECT id, amount FROM shop.orders "
                         "WHERE region = 'west'; CREATE DATABASE other; CREATE TABLE other.t (x UInt8) ENGINE = Memory")
        got = aspects(ingest("clickhouse", {"host_port": f"127.0.0.1:{p}", "username": "analyst",
                                            "password": "CH_PASSWORD", "databases": ["shop"]}, {"CH_PASSWORD": "ch-Secret-1"}))
        ds = datasets(got, "clickhouse")
        assert {"shop.orders", "shop.west"} <= set(ds) and "other.t" not in ds, set(ds)
        assert columns(ds["shop.orders"]) == ["id", "amount", "region"]
        assert upstreams(ds["shop.west"]) == ["shop.orders"]
    finally:
        ch.stop()


@needs(IMAGES["oracle"])
def test_oracle():
    p = port()
    db = Container(IMAGES["oracle"], {1521: p}, {"ORACLE_PASSWORD": "Sys-Gallery-1", "APP_USER": "shop",
                                                 "APP_USER_PASSWORD": "Shop-Gallery-1"}, name="gallery-oracle")
    try:
        def logs():
            out = subprocess.run(["docker", "logs", db.name], capture_output=True, text=True)
            return out.stdout + out.stderr
        until(lambda: "DATABASE IS READY" in logs(), 600, "oracle")
        db.exec("bash", "-c", "sqlplus -s shop/Shop-Gallery-1@localhost/FREEPDB1", input="""
            WHENEVER SQLERROR EXIT FAILURE
            CREATE TABLE orders (id NUMBER(10) PRIMARY KEY, amount NUMBER(10,2), region VARCHAR2(20));
            CREATE VIEW west AS SELECT id, amount FROM orders WHERE region = 'west';
            EXIT
            """)
        got = aspects(ingest("oracle", {"host_port": f"127.0.0.1:{p}", "service_name": "FREEPDB1", "username": "shop",
                                        "password": "ORACLE_PASSWORD", "schemas": ["SHOP"]},
                             {"ORACLE_PASSWORD": "Shop-Gallery-1"}))
        ds = {k.lower(): v for k, v in datasets(got, "oracle").items()}
        names = set(ds)
        orders = next(n for n in names if n.endswith("shop.orders"))
        west = next(n for n in names if n.endswith("shop.west"))
        assert [c.lower() for c in columns(ds[orders])] == ["id", "amount", "region"]
        assert [u.lower() for u in upstreams(ds[west])] == [orders], upstreams(ds[west])
    finally:
        db.stop()


@needs(IMAGES["dynamodb"])
def test_dynamodb():
    """Tables in DynamoDB Local, the endpoint and keys through the form and its advanced options; columns inferred."""
    import boto3

    p = port()
    db = Container(IMAGES["dynamodb"], {8000: p}, args=["-jar", "DynamoDBLocal.jar", "-inMemory"], name="gallery-ddb")
    try:
        ddb = boto3.client("dynamodb", endpoint_url=f"http://127.0.0.1:{p}", region_name="us-east-1",
                           aws_access_key_id="gallery", aws_secret_access_key="gallery")
        until(lambda: ddb.list_tables() is not None, 60)
        for table in ("orders", "audit"):
            ddb.create_table(TableName=table, KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
                             AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                             BillingMode="PAY_PER_REQUEST")
        ddb.put_item(TableName="orders", Item={"pk": {"S": "o1"}, "total": {"N": "9.5"}, "tags": {"SS": ["a"]}})
        got = aspects(ingest("dynamodb", {"aws_region": "us-east-1", "aws_access_key_id": "gallery",
                                          "aws_secret_access_key": "DDB_SECRET", "tables": [".*orders"]},
                             {"DDB_SECRET": "gallery-secret-1"}, advanced={"aws_endpoint_url": f"http://127.0.0.1:{p}"}))
        ds = datasets(got, "dynamodb")
        orders = [n for n in ds if n.endswith("orders")]
        assert len(orders) == 1 and not any(n.endswith("audit") for n in ds), set(ds)
        assert {"pk", "total", "tags"} <= set(columns(ds[orders[0]]))
    finally:
        db.stop()


@needs(IMAGES["hive"])
def test_hive():
    """HiveServer2 (embedded metastore): databases, tables with their columns, a view and its lineage."""
    p = port()
    hs2 = Container(IMAGES["hive"], {10000: p}, {"SERVICE_NAME": "hiveserver2"}, name="gallery-hive")
    beeline = ["beeline", "-u", "jdbc:hive2://localhost:10000/", "--silent=true", "-e"]
    try:
        until(lambda: hs2.exec(*beeline, "SELECT 1") is not None, 300, "hiveserver2")
        hs2.exec(*beeline, "CREATE DATABASE shop; CREATE TABLE shop.orders (id INT, amount DECIMAL(10,2), region STRING); "
                           "CREATE VIEW shop.west AS SELECT id, amount FROM shop.orders WHERE region = 'west'; "
                           "CREATE DATABASE other; CREATE TABLE other.t (x INT)")
        got = aspects(ingest("hive", {"host_port": f"127.0.0.1:{p}", "databases": ["shop"]}, {}))
        ds = datasets(got, "hive")
        assert {"shop.orders", "shop.west"} <= set(ds) and "other.t" not in ds, set(ds)
        assert columns(ds["shop.orders"]) == ["id", "amount", "region"]
        assert upstreams(ds["shop.west"]) == ["shop.orders"]
    finally:
        hs2.stop()


# ---- documents, topics, files, models ------------------------------------------------------------------
@needs(IMAGES["mongodb"])
def test_mongodb():
    p = port()
    db = Container(IMAGES["mongodb"], {27017: p}, {"MONGO_INITDB_ROOT_USERNAME": "admin",
                                                   "MONGO_INITDB_ROOT_PASSWORD": "mongo-Secret-1"}, name="gallery-mongo")
    shell = ["mongosh", "--quiet", "-u", "admin", "-p", "mongo-Secret-1", "--authenticationDatabase", "admin"]
    try:
        until(lambda: "1" in db.exec(*shell, "--eval", "db.runCommand({ping:1}).ok"), 120)
        db.exec(*shell, "shop", "--eval", "db.orders.insertMany([{_id: 1, total: 9.5, items: ['a'], customer: "
                                           "{name: 'Ana'}}, {_id: 2, total: 3, status: 'open'}])")
        got = aspects(ingest("mongodb", {"connect_uri": f"mongodb://127.0.0.1:{p}", "username": "admin",
                                         "password": "MONGO_PASSWORD"}, {"MONGO_PASSWORD": "mongo-Secret-1"},
                             advanced={"authMechanism": "SCRAM-SHA-256", "database_pattern": {"allow": ["shop"]}}))
        ds = datasets(got, "mongodb")
        assert set(ds) == {"shop.orders"}, set(ds)
        assert {"_id", "total", "items", "customer", "name", "status"} <= set(columns(ds["shop.orders"]))
    finally:
        db.stop()


@needs(IMAGES["kafka"])
def test_kafka_with_its_schema_registry():
    kp, sr = port(), port()
    rp = Container(IMAGES["kafka"], {19092: kp, 8081: sr}, args=[
        "redpanda", "start", "--mode", "dev-container", "--smp", "1",
        "--kafka-addr", "internal://0.0.0.0:9092,external://0.0.0.0:19092",
        "--advertise-kafka-addr", f"internal://localhost:9092,external://127.0.0.1:{kp}",
        "--schema-registry-addr", "0.0.0.0:8081"], name="gallery-kafka")
    try:
        until(lambda: "HEALTHY" in rp.exec("rpk", "cluster", "health").upper().replace(" ", "")
              or "Healthy:true" in rp.exec("rpk", "cluster", "health").replace(" ", ""), 120)
        rp.exec("rpk", "topic", "create", "orders", "clickstream")
        import httpx

        schema = {"type": "record", "name": "Order", "fields": [{"name": "id", "type": "long"},
                                                              {"name": "amount", "type": "double"}]}
        until(lambda: httpx.post(f"http://127.0.0.1:{sr}/subjects/orders-value/versions",
                                 json={"schema": json.dumps(schema)}).status_code == 200, 60)
        got = aspects(ingest("kafka", {"bootstrap": f"127.0.0.1:{kp}", "schema_registry_url": f"http://127.0.0.1:{sr}",
                                       "topics": ["orders", "clickstream"]}, {}))
        ds = datasets(got, "kafka")
        assert set(ds) == {"orders", "clickstream"}, set(ds)
        assert {"id", "amount"} <= set(columns(ds["orders"]))
    finally:
        rp.stop()


def test_s3_on_cairn_store():
    """Parquet files under a path spec, read through the S3 API of Cairn Store."""
    store_bin = os.environ.get("CAIRN_STORE_BIN")
    if not store_bin:
        pytest.skip("needs CAIRN_STORE_BIN")
    import pyarrow as pa
    import pyarrow.parquet as pq

    sp = port()
    d = tempfile.mkdtemp()
    key = ("gallerykey", "gallery-secret-key-0123456789")
    store = subprocess.Popen([store_bin], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
        **os.environ, "CAIRN_STORE_ACCESS_KEY": key[0], "CAIRN_STORE_SECRET_KEY": key[1], "CAIRN_STORE_DATA": d,
        "CAIRN_STORE_ADDR": f"127.0.0.1:{sp}", "CAIRN_STORE_BUCKETS": "lake", "CAIRN_STORE_FSYNC": "false",
        "CAIRN_STORE_META": ""})
    try:
        import boto3

        s3 = boto3.client("s3", endpoint_url=f"http://127.0.0.1:{sp}", aws_access_key_id=key[0],
                          aws_secret_access_key=key[1], region_name="us-east-1")
        until(lambda: s3.list_buckets() is not None, 30)
        for table, cols in (("orders", {"id": [1, 2], "amount": [9.5, 3.0]}), ("customers", {"id": [1], "name": ["Ana"]})):
            buf = io.BytesIO()
            pq.write_table(pa.table(cols), buf)
            s3.put_object(Bucket="lake", Key=f"sales/{table}/part-0.parquet", Body=buf.getvalue())
        got = aspects(ingest("s3", {"paths": ["s3://lake/sales/{table}/*.parquet"], "aws_region": "us-east-1"},
                             {"S3_KEY": key[0], "S3_SECRET": key[1]},
                             advanced={"aws_config": {"aws_endpoint_url": f"http://127.0.0.1:{sp}",
                                                      "aws_access_key_id": "${S3_KEY}",
                                                      "aws_secret_access_key": "${S3_SECRET}"}}))
        ds = datasets(got, "s3")
        assert set(ds) == {"lake/sales/orders", "lake/sales/customers"}, set(ds)
        assert set(columns(ds["lake/sales/orders"])) == {"id", "amount"}
    finally:
        store.terminate()


@needs(IMAGES["mlflow"])
def test_mlflow_model_registry():
    p = port()
    srv = Container(IMAGES["mlflow"], {5000: p}, args=["mlflow", "server", "--host", "0.0.0.0", "--port", "5000",
                                                       "--backend-store-uri", "sqlite:////tmp/mlflow.db"],
                    name="gallery-mlflow")
    try:
        import httpx

        base = f"http://127.0.0.1:{p}/api/2.0/mlflow"
        until(lambda: httpx.get(f"http://127.0.0.1:{p}/health").status_code == 200, 120)
        assert httpx.post(f"{base}/registered-models/create", json={"name": "churn"}).status_code == 200
        run = httpx.post(f"{base}/runs/create", json={"experiment_id": "0"}).json()["run"]["info"]
        v = httpx.post(f"{base}/model-versions/create", json={"name": "churn", "run_id": run["run_id"],
                                                               "source": run["artifact_uri"] + "/model"})
        assert v.status_code == 200, v.text
        got = ingest("mlflow", {"tracking_uri": f"http://127.0.0.1:{p}"}, {})
        urns = {e.get("entityUrn") or next(iter(e.get("proposedSnapshot", {}).values()), {}).get("urn") for e in got}
        assert any(u and u.startswith("urn:li:mlModelGroup:(urn:li:dataPlatform:mlflow,churn") for u in urns), urns
        assert any(u and u.startswith("urn:li:mlModel:(urn:li:dataPlatform:mlflow,churn_1") for u in urns), urns
    finally:
        srv.stop()


# ---- the executor image -------------------------------------------------------------------------------
def image_extras() -> tuple[list[str], str]:
    """The ingestion extras and constraints ``docker/agent.Dockerfile`` installs by default."""
    import re

    text = open(os.path.join(os.path.dirname(__file__), "..", "docker", "agent.Dockerfile")).read()
    extras = re.search(r"^ARG INGEST_EXTRAS=(\S+)$", text, re.M).group(1).split(",")
    constraints = re.search(r'^ARG INGEST_CONSTRAINTS="([^"]*)"$', text, re.M).group(1)
    return extras, constraints


def test_the_executor_image_can_run_every_gallery_connector():
    """Every gallery connector's source type is among the image's extras, and every one of them loads in the
    environment built from those extras and constraints (CI builds ``CAIRN_INGEST_PYTHON``'s from them)."""
    from cairn.sources.catalog import CONNECTORS

    extras, _ = image_extras()
    types = sorted({c.source_type for c in CONNECTORS})
    assert set(types) <= set(extras), sorted(set(types) - set(extras))
    probe = ("import json, sys\nfrom datahub.ingestion.source.source_registry import source_registry\nbad = {}\n"
             "for t in sys.argv[1:]:\n    try:\n        source_registry.get(t)\n    except Exception as e:\n"
             "        bad[t] = str(e)[:300]\nprint(json.dumps(bad))\n")
    out = subprocess.run([INGEST_PYTHON, "-c", probe, *types], capture_output=True, text=True, timeout=600,
                         env={**os.environ, "DATAHUB_TELEMETRY_ENABLED": "false"})
    assert out.returncode == 0, out.stderr[-3000:]
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {}


# ---- BI ------------------------------------------------------------------------------------------------
@needs(IMAGES["metabase"])
@needs(IMAGES["postgres"])
def test_metabase():
    """Metabase on PostgreSQL: a question (native SQL) on a dashboard. The Metabase form gives the dashboard, the chart
    and the chart's lineage to the PostgreSQL table its SQL reads."""
    import httpx

    net = f"cairn-gallery-{secrets.token_hex(4)}"
    subprocess.run(["docker", "network", "create", net], check=True, capture_output=True)
    db = Container(IMAGES["postgres"], {}, {"POSTGRES_PASSWORD": "pg-Secret-2"}, name="gallery-mbpg", network=net)
    p = port()
    mb = Container(IMAGES["metabase"], {3000: p}, {"MB_ANON_TRACKING_ENABLED": "false", "JAVA_OPTS": "-Xmx1g"},
                   name="gallery-metabase", network=net)
    try:
        until(lambda: db.exec("psql", "-U", "postgres", "-c", "SELECT 1") is not None)
        db.exec("psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1", input="""
            CREATE SCHEMA sales; CREATE TABLE sales.orders (id int, amount numeric(10,2));
            INSERT INTO sales.orders VALUES (1, 150), (2, 20);""")
        base = f"http://127.0.0.1:{p}/api"
        until(lambda: httpx.get(f"{base}/health", timeout=5).json().get("status") == "ok", 400, "metabase")
        token = httpx.get(f"{base}/session/properties").json()["setup-token"]
        email, password = "analyst@example.com", "Mb-Gallery-2026!"
        session = httpx.post(f"{base}/setup", json={
            "token": token, "user": {"email": email, "password": password, "first_name": "Ana", "last_name": "Lyst",
                                     "site_name": "Cairn"},
            "prefs": {"site_name": "Cairn", "allow_tracking": False}}, timeout=60).json()["id"]
        h = {"X-Metabase-Session": session}
        database = httpx.post(f"{base}/database", headers=h, timeout=120, json={
            "engine": "postgres", "name": "shop", "details": {"host": db.name, "port": 5432, "dbname": "postgres",
                                                              "user": "postgres", "password": "pg-Secret-2"}})
        assert database.status_code == 200, database.text
        card = httpx.post(f"{base}/card", headers=h, json={
            "name": "Big orders", "display": "table", "visualization_settings": {},
            "dataset_query": {"type": "native", "database": database.json()["id"],
                              "native": {"query": "SELECT id, amount FROM sales.orders WHERE amount > 100"}}})
        assert card.status_code == 200, card.text
        dash = httpx.post(f"{base}/dashboard", headers=h, json={"name": "Sales"}).json()["id"]
        put = httpx.put(f"{base}/dashboard/{dash}", headers=h, json={"dashcards": [
            {"id": -1, "card_id": card.json()["id"], "row": 0, "col": 0, "size_x": 6, "size_y": 4}]})
        assert put.status_code == 200, put.text
        got = aspects(ingest("metabase", {"connect_uri": f"http://127.0.0.1:{p}", "username": email,
                                          "password": "METABASE_PASSWORD"}, {"METABASE_PASSWORD": password}))
        dashboards = {u: a for u, a in got.items() if u.startswith("urn:li:dashboard:(metabase,")}
        charts = {u: a for u, a in got.items() if u.startswith("urn:li:chart:(metabase,")}
        ours = [a for a in dashboards.values() if a["dashboardInfo"]["title"] == "Sales"]   # beside Metabase's sample
        assert len(ours) == 1, [a["dashboardInfo"]["title"] for a in dashboards.values()]
        chart = charts[f"urn:li:chart:(metabase,{card.json()['id']})"]
        assert chart["chartInfo"]["title"] == "Big orders"
        info = ours[0]["dashboardInfo"]                      # chartEdges in newer releases, charts in older ones
        linked = list(info.get("charts") or []) + [e["destinationUrn"] for e in info.get("chartEdges") or []]
        assert f"urn:li:chart:(metabase,{card.json()['id']})" in linked, info
        # inputEdges in newer releases (with column-level inputFields), inputs in older ones.
        inputs = [i if isinstance(i, str) else i.get("string", "") for i in chart["chartInfo"].get("inputs", [])]
        inputs += [e["destinationUrn"] for e in chart["chartInfo"].get("inputEdges", [])]
        assert any(",postgres.sales.orders," in i and "dataPlatform:postgres" in i for i in inputs), chart["chartInfo"]
        if "inputFields" in chart:
            assert {f["schemaFieldUrn"].rsplit(",", 1)[1].rstrip(")") for f in chart["inputFields"]["fields"]} == \
                {"id", "amount"}
    finally:
        mb.stop()
        db.stop()
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


def test_every_gallery_form_makes_a_config_the_framework_accepts():
    """Each gallery form, filled with its required fields (and each select's options), gives a recipe whose source
    config the ingestion framework parses: field paths, types and enum values are the framework's own. Services that
    need an account (Snowflake, Fivetran, ...) are checked this far without one."""
    from cairn.sources.catalog import CONNECTORS, build_recipe

    def sample(f):                                       # a value of the shape the field asks for
        if f.kind == "select":
            return f.options[0]
        if f.kind != "text":
            return {"secret": "SOME_SECRET", "number": 1, "bool": True, "list": ["cairn-proj-1"]}[f.kind]
        k = f.key
        return ("host.example:1234" if k == "host_port" else "https://svc.example.com" if "url" in k or "uri" in k
                else "123456789012" if k == "aws_account_id" else "us-east-1" if "region" in k else "cairn-x1")

    # Fields one choice of another field makes required (the form shows both; the framework says which is missing).
    conditional = {"airbyte": {"oss": {"host_port": "airbyte.example:8000"},
                               "cloud": {"cloud_workspace_id": "ws-1", "oauth2_client_id": "cid-1",
                                         "oauth2_client_secret": "AIRBYTE_SECRET"}}}
    cases = []
    for c in CONNECTORS:
        base = {f.key: sample(f) for f in c.fields if f.required}
        base.update(conditional.get(c.id, {}).get(base.get("deployment_type") or "oss", {}))
        cases.append((c.id, c.source_type, build_recipe(c.id, base)["source"]["config"]))
        for f in c.fields:
            for o in f.options if f.kind == "select" else []:
                values = {**base, f.key: o, **conditional.get(c.id, {}).get(o, {})}
                cases.append((f"{c.id}:{f.key}={o}", c.source_type, build_recipe(c.id, values)["source"]["config"]))
    # Secret references expanded as the executor does, to values of the right shape (a private key is parsed).
    subprocess.run(["openssl", "genrsa", "-out", "/tmp/gallery-form-key.pem", "2048"], check=True, capture_output=True)
    pem = open("/tmp/gallery-form-key.pem").read()

    def expand(v):
        if isinstance(v, dict):
            return {k: (pem if k == "private_key" and isinstance(x, str) and x.startswith("${") else expand(x))
                    for k, x in v.items()}
        if isinstance(v, list):
            return [expand(x) for x in v]
        return "secret-value-1" if isinstance(v, str) and v.startswith("${") else v
    cases = [(name, t, expand(cfg)) for name, t, cfg in cases]
    probe = ("import json, sys\nfrom datahub.ingestion.source.source_registry import source_registry\nbad = {}\n"
             "for name, t, cfg in json.load(sys.stdin):\n"
             "    try:\n        source_registry.get(t).get_config_class().parse_obj(cfg)\n"
             "    except Exception as e:\n        bad[name] = str(e)[:400]\nprint(json.dumps(bad))\n")
    out = subprocess.run([INGEST_PYTHON, "-c", probe], input=json.dumps(cases), capture_output=True, text=True,
                         timeout=600, env={**os.environ, "DATAHUB_TELEMETRY_ENABLED": "false"})
    assert out.returncode == 0, out.stderr[-3000:]
    bad = json.loads(out.stdout.strip().splitlines()[-1])
    assert bad == {}, json.dumps(bad, indent=1)


# ---- APIs ------------------------------------------------------------------------------------------------
def test_openapi():
    """An API's OpenAPI document served over HTTP: its endpoints become assets with their response fields."""
    import http.server
    import threading

    # Responses are objects: the framework's parser fails on an example that is a JSON array (flatten2list).
    spec = {"openapi": "3.0.3", "info": {"title": "Shop", "version": "1.0"}, "paths": {
        "/orders": {"get": {"summary": "One order", "responses": {"200": {"description": "ok", "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/Order"},
                                 "example": {"id": 1, "amount": 9.5, "region": "west"}}}}}}},
        "/customers": {"get": {"summary": "One customer", "responses": {"200": {"description": "ok", "content": {
            "application/json": {"example": {"id": 1, "name": "Ana"}}}}}}}},
        "components": {"schemas": {"Order": {"type": "object", "properties": {
            "id": {"type": "integer"}, "amount": {"type": "number"}, "region": {"type": "string"}}}}}}
    body = json.dumps(spec).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/openapi.json":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        got = aspects(ingest("openapi", {"name": "shop", "url": url, "swagger_file": "openapi.json"}, {},
                             advanced={"enable_api_calls_for_schema_extraction": False}))
        ds = datasets(got, "OpenApi")
        orders = next((a for n, a in ds.items() if n.endswith("orders")), None)
        assert orders is not None, set(ds)
        assert {"id", "amount", "region"} <= set(columns(orders)), columns(orders)
        assert any(n.endswith("customers") for n in ds), set(ds)
    finally:
        server.shutdown()
