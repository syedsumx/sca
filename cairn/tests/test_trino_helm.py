"""The Helm chart's Trino, as rendered: `helm template` with TLS and a worker, then the coordinator, the
worker and their init containers run in Docker exactly as the rendered pods would (the rendered config
files, environment and commands), against a real Cairn API, Cairn Catalog and Cairn Store. Sign-in with
a Cairn token over the chart's TLS, Cairn's access control, a query spread over the worker, and lineage.

Needs docker, helm (CAIRN_HELM, or helm on PATH) and the store and catalog binaries."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid

import pytest
import yaml

from test_trino_e2e import KEY, CATALOG_TOKEN, STORE_KEY, _ready, cairn, lake  # noqa: F401  (fixtures)

ROOT = os.path.join(os.path.dirname(__file__), "..")
HELM = os.environ.get("CAIRN_HELM") or shutil.which("helm")
RELEASE = "t"

pytestmark = pytest.mark.skipif(not (_ready() and HELM), reason="needs docker, helm and the store and catalog binaries")


def render(api_port, catalog_port, store_port, extra=()):
    sets = [a for kv in extra for a in ("--set", kv)]
    out = subprocess.run([HELM, "template", RELEASE, "helm/cairn", "--set", "cairn.store.enabled=true",
                          "--set", "cairn.catalog.enabled=true", "--set", "cairn.trino.enabled=true",
                          "--set", "cairn.trino.tls.secretName=trino-tls", "--set", "cairn.trino.workers=1", *sets,
                          "--set", "cairn.trino.queryMaxMemory=1GB", "--set", "cairn.trino.gracefulShutdownSeconds=5",
                          "--set", f"cairn.api.port={api_port}", "--set", f"cairn.catalog.port={catalog_port}",
                          "--set", f"cairn.store.port={store_port}"],
                         cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    docs = {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(out.stdout) if d}
    return docs


def env_of(container):
    # The chart's secretKeyRefs, filled as the cluster would.
    values = {"CAIRN_CATALOG_TOKEN": CATALOG_TOKEN, "CAIRN_STORE_ACCESS_KEY": STORE_KEY[0],
              "CAIRN_STORE_SECRET_KEY": STORE_KEY[1], "CAIRN_TRINO_ACCESS_KEY": KEY,
              "CAIRN_TRINO_EXCHANGE_ACCESS_KEY": STORE_KEY[0], "CAIRN_TRINO_EXCHANGE_SECRET_KEY": STORE_KEY[1]}
    out = []
    for e in container.get("env") or []:
        v = e.get("value", values.get(e["name"]))
        assert v is not None, f"no value for {e['name']}"
        out += ["-e", f"{e['name']}={v}"]
    return out


def tls_secret(d):
    """A kubernetes.io/tls Secret's two files (tls.crt, tls.key) for the chart's TLS."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{RELEASE}-cairn-trino")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(f"{RELEASE}-cairn-trino"), x509.DNSName("localhost")]),
                           critical=False)
            .sign(key, hashes.SHA256()))
    os.makedirs(f"{d}/tls-src")
    open(f"{d}/tls-src/tls.crt", "wb").write(cert.public_bytes(serialization.Encoding.PEM))
    # PKCS#1 ("BEGIN RSA PRIVATE KEY"), as cert-manager and `kubectl create secret tls` usually hold it.
    open(f"{d}/tls-src/tls.key", "wb").write(key.private_bytes(serialization.Encoding.PEM,
                                                               serialization.PrivateFormat.TraditionalOpenSSL,
                                                               serialization.NoEncryption()))
    os.chmod(f"{d}/tls-src/tls.key", 0o644)


@pytest.fixture(scope="module")
def cluster(lake, cairn):  # noqa: F811
    yield from start_cluster(lake, cairn)


def start_cluster(lake, cairn, extra=()):  # noqa: F811
    """The rendered chart's coordinator and one worker, as Docker containers; yields a SQL runner."""
    cairn_url, services = cairn
    api_port = int(cairn_url.rsplit(":", 1)[1])
    docs = render(api_port, lake["catalog_port"], lake["store_port"], extra)
    cm = docs[("ConfigMap", f"{RELEASE}-cairn-trino")]["data"]
    coord = docs[("Deployment", f"{RELEASE}-cairn-trino")]["spec"]["template"]["spec"]
    worker = docs[("Deployment", f"{RELEASE}-cairn-trino-worker")]["spec"]["template"]["spec"]
    # The worker mounts a ConfigMap that exists.
    assert {v["configMap"]["name"] for v in worker["volumes"] if "configMap" in v} == {f"{RELEASE}-cairn-trino"}

    d = tempfile.mkdtemp(prefix="trino-helm-")
    for k, v in cm.items():
        open(f"{d}/{k}", "w").write(v)
    tls_secret(d)
    os.chmod(d, 0o755)
    for e in ("tls", "spill", "cache"):
        os.makedirs(f"{d}/{e}", exist_ok=True)
        os.chmod(f"{d}/{e}", 0o777)  # the pod's emptyDirs, written as uid 1000
    net = f"cairn-trino-helm-{uuid.uuid4().hex[:6]}"
    subprocess.run(["docker", "network", "create", net], check=True, capture_output=True)
    hosts = [f"--add-host={RELEASE}-cairn-{c}:host-gateway" for c in ("api", "catalog", "store")]
    started = []

    def mounts(container, files, node="init"):
        out = []
        for e in ("spill", "cache"):  # each pod its own emptyDir
            os.makedirs(f"{d}/{e}/{node}", exist_ok=True)
            os.chmod(f"{d}/{e}/{node}", 0o777)
        for m in container.get("volumeMounts") or []:
            src = {"config": f"{d}/{m.get('subPath', '')}", "tls": f"{d}/tls", "tls-src": f"{d}/tls-src",
                   "spill": f"{d}/spill/{node}", "cache": f"{d}/cache/{node}"}[m["name"]]
            assert m["name"] != "config" or m.get("subPath") in cm, f"{m.get('subPath')} is not in the ConfigMap"
            out += ["-v", f"{src}:{m['mountPath']}{':ro' if m.get('readOnly') else ''}"]
        return out

    # The coordinator pod's init containers, in order, as the kubelet would run them.
    for ic in coord["initContainers"]:
        r = subprocess.run(["docker", "run", "--rm", "--network", net, *hosts, "--user", "1000", *env_of(ic),
                            *mounts(ic, cm), "--entrypoint", ic["command"][0], ic["image"], *ic["command"][1:]],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, f"init container {ic['name']}: {r.stdout}{r.stderr}"
    assert os.path.getsize(f"{d}/tls/keystore.pem") > 0

    def run(name, spec, alias=None):
        c = spec["containers"][0]
        cname = f"{net}-{name}"
        subprocess.run(["docker", "run", "-d", "--rm", "--name", cname, "--network", net,
                        *(["--network-alias", alias] if alias else []), *hosts, "--user", "1000", *env_of(c),
                        *mounts(c, cm, name), c["image"]], check=True, capture_output=True, timeout=600)
        started.append(cname)
        return cname

    def start_coordinator():
        name = run(f"coordinator-{len(started)}", coord, alias=f"{RELEASE}-cairn-trino")
        end = time.time() + 300
        while True:
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr
            if "SERVER STARTED" in logs:
                return name
            assert "Configuration errors" not in logs and "Error injecting" not in logs, logs[-4000:]
            assert time.time() < end, "the coordinator did not start"
            time.sleep(1)

    https = cm["config.properties"].split("http-server.https.port=")[1].split()[0]

    def command(statement, user="admin", source=None):
        token = services.trino_tokens.mint(f"urn:li:corpuser:{user}")["token"]
        return ["docker", "exec", sql.coordinator, "trino", "--server", f"https://localhost:{https}", "--insecure",
                "--access-token", token, "--user", user, "--catalog", "lakehouse", "--output-format", "JSON",
                *(["--source", source] if source else []), "--execute", statement]

    def sql(statement, user="admin", source=None):
        out = subprocess.run(command(statement, user, source), capture_output=True, text=True, timeout=300)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip().splitlines()[-1] if out.stderr.strip() else out.stdout)
        return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]

    sql.coordinator = start_coordinator()
    sql.worker = run("worker-0", worker)
    sql.command, sql.run, sql.start_coordinator = command, run, start_coordinator
    sql.worker_spec, sql.started, sql.dir, sql.config = worker, started, d, cm
    yield sql
    for c in started:
        subprocess.run(["docker", "rm", "-f", c], capture_output=True, timeout=60)
    subprocess.run(["docker", "network", "rm", net], capture_output=True, timeout=60)
    shutil.rmtree(d, ignore_errors=True)


def test_the_rendered_chart_runs_trino_with_sign_in_access_control_and_a_worker(cluster, cairn):  # noqa: F811
    sql = cluster
    # The worker joined (it is the only node that runs queries: the coordinator only plans).
    end = time.time() + 120
    while len(sql("SELECT node_id FROM system.runtime.nodes WHERE state = 'active'")) < 2:
        assert time.time() < end, "the worker did not join"
        time.sleep(2)
    sql("CREATE SCHEMA lakehouse.sales")
    sql("CREATE TABLE lakehouse.sales.orders AS SELECT * FROM (VALUES (1, 'jane@acme.com', 'EU'), "
        "(2, 'bo@example.org', 'US')) AS t(id, email, region)")
    # Cairn's policies through the chart's access-control file: the lakehouse catalog is Trino's platform.
    _, s = cairn
    s.graph.aspects[("urn:li:dataset:(urn:li:dataPlatform:trino,lakehouse.sales.orders,PROD)", "schemaMetadata")] = {
        "fields": [{"fieldPath": "id"}, {"fieldPath": "email", "glossaryTerms": {"terms": [
            {"urn": "urn:li:glossaryTerm:Classification.PII.Email"}]}}, {"fieldPath": "region"}]}
    rows = sql("SELECT id, email FROM sales.orders ORDER BY id", user="ana")
    assert [r["email"] for r in rows] == ["j•••@•••.com", "b•••@•••.org"]
    assert [r["id"] for r in sql("SELECT id FROM sales.orders ORDER BY id", user="bob")] == [1]
    # A query big enough to need the worker.
    assert sql("SELECT count(*) AS n FROM UNNEST(sequence(1, 1000)) AS a(x) "
               "CROSS JOIN UNNEST(sequence(1, 1000)) AS b(y)") == [{"n": 1000000}]
    # Lineage through the chart's event listener.
    sql("CREATE TABLE lakehouse.sales.by_region AS SELECT region, count(*) AS n FROM lakehouse.sales.orders GROUP BY region")
    job = f"urn:li:dataJob:(urn:li:dataFlow:(trino,{RELEASE}-cairn-trino:8080,PROD),trino:lakehouse.sales.by_region)"
    end = time.time() + 30
    while time.time() < end and not s.graph.get_aspect(job, "dataJobInputOutput"):
        time.sleep(0.5)
    assert s.graph.get_aspect(job, "dataJobInputOutput"), [u for u, a in s.graph.aspects if "dataJob" in u]
    # Usage through the chart's HTTP event listener: ana's read above is counted.
    orders = "urn:li:dataset:(urn:li:dataPlatform:trino,lakehouse.sales.orders,PROD)"
    end = time.time() + 30
    while time.time() < end and not s.graph.get_timeseries(orders, "datasetUsageStatistics", 1):
        time.sleep(0.5)
    usage = s.graph.get_timeseries(orders, "datasetUsageStatistics", 1)
    assert usage and "urn:li:corpuser:ana" in {u["user"] for u in usage[0]["userCounts"]}, usage



def active_nodes(sql):
    return sql("SELECT node_id, coordinator FROM system.runtime.nodes WHERE state = 'active'")


def test_resource_groups_from_the_chart(cluster):
    sql = cluster
    # Compose runs the same groups as the chart.
    deploy = json.load(open(os.path.join(ROOT, "deploy", "trino", "resource-groups.json")))
    rendered = render(8800, 8181, 8333)[("ConfigMap", f"{RELEASE}-cairn-trino")]["data"]["resource-groups.json"]
    assert deploy == json.loads(rendered)
    sql("SELECT 1", source="cairn")
    sql("SELECT 2", user="ana")
    sql("SELECT 3", source="dbt-core")
    rows = sql("SELECT query, array_join(resource_group_id, '.') AS resource_group_id FROM system.runtime.queries "
               "WHERE query IN ('SELECT 1', 'SELECT 2', 'SELECT 3')")
    groups = {r["query"]: r["resource_group_id"] for r in rows}
    assert groups["SELECT 1"] == "global.cairn"
    assert groups["SELECT 2"] == "global.adhoc.ana"
    assert groups["SELECT 3"] == "global.pipelines"


def test_a_worker_drains_before_it_leaves_and_a_new_coordinator_takes_over(cluster, cairn):  # noqa: F811
    """Scaling in runs the worker's preStop hook: the worker stops taking work, finishes the running query
    and leaves. Then the coordinator is replaced (as Kubernetes would after a crash): a new one starts from
    the same spec, a worker joins it, and the lakehouse, sign-in and policies are all still there."""
    sql = cluster
    end = time.time() + 120
    while len(active_nodes(sql)) < 2:
        assert time.time() < end, "the worker did not join"
        time.sleep(2)
    # A query long enough to be running when the worker is told to go.
    long = subprocess.Popen(sql.command("SELECT count(*) AS n FROM UNNEST(sequence(1, 10000)) AS a(x) "
                                        "CROSS JOIN UNNEST(sequence(1, 10000)) AS b(y) CROSS JOIN UNNEST(sequence(1, 8)) AS c(z)"),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    end = time.time() + 60
    while not sql("SELECT query_id FROM system.runtime.queries WHERE state = 'RUNNING' AND query LIKE '%sequence(1, 8)%'"):
        assert time.time() < end and long.poll() is None, "the query did not start"
        time.sleep(0.5)
    hook = sql.worker_spec["containers"][0]["lifecycle"]["preStop"]["exec"]["command"]
    drain = subprocess.Popen(["docker", "exec", sql.worker, *hook], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = long.communicate(timeout=300)
    assert long.returncode == 0, err
    assert json.loads(out.strip().splitlines()[-1]) == {"n": 800000000}  # finished on the draining worker
    drain.wait(timeout=180)
    end = time.time() + 120
    while subprocess.run(["docker", "inspect", sql.worker], capture_output=True).returncode == 0:
        if time.time() > end:
            logs = subprocess.run(["docker", "logs", "--tail", "40", sql.worker], capture_output=True, text=True)
            raise AssertionError(f"the drained worker did not leave: hook rc={drain.returncode} {drain.stdout.read()} "
                                 f"{drain.stderr.read()}\n{logs.stdout}{logs.stderr}")
        time.sleep(2)
    # The coordinator goes; a new one comes up from the same spec and a new worker joins it.
    subprocess.run(["docker", "rm", "-f", sql.coordinator], capture_output=True, timeout=60)
    sql.coordinator = sql.start_coordinator()
    sql.worker = sql.run("worker-1", sql.worker_spec)
    end = time.time() + 120
    while len(active_nodes(sql)) < 2:
        assert time.time() < end, "no worker joined the new coordinator"
        time.sleep(2)
    # Tables live in Cairn Catalog and Cairn Store, sign-in and policies in Cairn: nothing was lost.
    assert [r["email"] for r in sql("SELECT email FROM sales.orders ORDER BY id", user="ana")] == ["j•••@•••.com", "b•••@•••.org"]


FT = ("cairn.trino.faultTolerance.enabled=true", "cairn.trino.spill.enabled=true", "cairn.trino.cache.enabled=true",
      "cairn.trino.workers=2")


def test_fault_tolerant_execution_spill_and_cache(lake, cairn):  # noqa: F811
    """The chart with fault tolerance, spill and the file cache on: a worker killed in the middle of a query
    does not fail it (its tasks run again on the other worker, from data staged in the exchange bucket);
    a repeated read is served from the cache directory."""
    import boto3
    from botocore.config import Config

    gen = start_cluster(lake, cairn, FT)
    sql = next(gen)
    try:
        cm = sql.config
        assert "retry-policy=TASK" in cm["config.properties"] and "retry-policy=TASK" in cm["worker.properties"]
        assert "spill-enabled=true" in cm["worker.properties"]
        assert "fs.cache.enabled=true" in cm["lakehouse.properties"]
        workers = [sql.worker, sql.run("worker-b", sql.worker_spec)]
        end = time.time() + 120
        while len(active_nodes(sql)) < 3:
            assert time.time() < end, "the workers did not join"
            time.sleep(2)
        sql("CREATE SCHEMA IF NOT EXISTS lakehouse.ft")
        sql("CREATE TABLE lakehouse.ft.nums AS SELECT a.x * 10000 + b.y AS x FROM UNNEST(sequence(0, 19)) AS a(x) "
            "CROSS JOIN UNNEST(sequence(1, 10000)) AS b(y)")
        # The cache: two reads of the same table; the files land on the worker's local disk.
        for _ in range(2):
            assert sql("SELECT sum(x) AS s FROM lakehouse.ft.nums") == [{"s": 200000 * 200001 // 2}]
        cached = [f for _, _, fs in os.walk(f"{sql.dir}/cache") for f in fs]
        assert cached, "nothing was cached"
        # A long query with a shuffle (a GROUP BY), so tasks on both workers hand data over the exchange.
        q = ("SELECT count(*) AS n FROM (SELECT a.x % 1000 AS k, count(*) AS c FROM UNNEST(sequence(1, 10000)) AS a(x) "
             "CROSS JOIN UNNEST(sequence(1, 10000)) AS b(y) CROSS JOIN UNNEST(sequence(1, 24)) AS c(z) GROUP BY a.x % 1000)")
        long = subprocess.Popen(sql.command(q), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        end = time.time() + 60
        while not (running := sql("SELECT query_id FROM system.runtime.queries WHERE state = 'RUNNING' "
                                  "AND query LIKE '%sequence(1, 24)%'")):
            assert time.time() < end and long.poll() is None, "the query did not start"
            time.sleep(0.3)
        # Kill a worker that is running some of its tasks (Trino names a node after its pod's hostname).
        hosts = {subprocess.run(["docker", "inspect", "-f", "{{.Config.Hostname}}", w], capture_output=True,
                                text=True).stdout.strip(): w for w in workers}
        end = time.time() + 60
        while not (busy := [r["node_id"] for r in sql(
                f"SELECT node_id FROM system.runtime.tasks WHERE query_id = '{running[0]['query_id']}' "
                # With fault-tolerant execution tasks are short: any task not yet finished on a worker will do.
                "AND state IN ('PLANNED', 'RUNNING', 'FLUSHING')") if r["node_id"] in hosts]):
            assert time.time() < end and long.poll() is None, (
                "no task ran on a worker", hosts, sql(f"SELECT node_id, state FROM system.runtime.tasks "
                                                       f"WHERE query_id = '{running[0]['query_id']}'"))
            time.sleep(0.3)
        time.sleep(2)
        subprocess.run(["docker", "kill", hosts[busy[0]]], capture_output=True, timeout=60)
        assert long.poll() is None, "the query ended before the worker was killed"
        # Intermediate data is staged in the exchange bucket in Cairn Store while it runs.
        s3 = boto3.client("s3", endpoint_url=lake["store"], aws_access_key_id=STORE_KEY[0],
                          aws_secret_access_key=STORE_KEY[1], region_name="us-east-1",
                          config=Config(s3={"addressing_style": "path"}))
        staged = 0
        while long.poll() is None:
            staged = max(staged, s3.list_objects_v2(Bucket="trino-exchange").get("KeyCount", 0))
            time.sleep(0.2)
        out, err = long.communicate(timeout=60)
        assert long.returncode == 0, err
        assert json.loads(out.strip().splitlines()[-1]) == {"n": 1000}
        assert staged, "nothing was staged in the exchange bucket"
        # It really lost tasks on the killed worker and ran them again.
        token = cairn[1].trino_tokens.mint("urn:li:corpuser:admin")["token"]
        https = cm["config.properties"].split("http-server.https.port=")[1].split()[0]
        info = json.loads(subprocess.run(
            ["docker", "exec", sql.coordinator, "curl", "-sk", "-H", f"Authorization: Bearer {token}",
             f"https://localhost:{https}/v1/query/{running[0]['query_id']}"], capture_output=True, text=True, timeout=60).stdout)
        stats = info["queryStats"]
        assert stats["failedTasks"] > 0, {k: v for k, v in stats.items() if "ailed" in k}
    finally:
        for _ in gen:
            pass
