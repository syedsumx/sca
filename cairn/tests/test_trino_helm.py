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


def render(api_port, catalog_port, store_port):
    out = subprocess.run([HELM, "template", RELEASE, "helm/cairn", "--set", "cairn.store.enabled=true",
                          "--set", "cairn.catalog.enabled=true", "--set", "cairn.trino.enabled=true",
                          "--set", "cairn.trino.tls.secretName=trino-tls", "--set", "cairn.trino.workers=1",
                          "--set", "cairn.trino.queryMaxMemory=1GB",
                          "--set", f"cairn.api.port={api_port}", "--set", f"cairn.catalog.port={catalog_port}",
                          "--set", f"cairn.store.port={store_port}"],
                         cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    docs = {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(out.stdout) if d}
    return docs


def env_of(container):
    # The chart's secretKeyRefs, filled as the cluster would.
    values = {"CAIRN_CATALOG_TOKEN": CATALOG_TOKEN, "CAIRN_STORE_ACCESS_KEY": STORE_KEY[0],
              "CAIRN_STORE_SECRET_KEY": STORE_KEY[1], "CAIRN_TRINO_ACCESS_KEY": KEY}
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
    cairn_url, services = cairn
    api_port = int(cairn_url.rsplit(":", 1)[1])
    docs = render(api_port, lake["catalog_port"], lake["store_port"])
    cm = docs[("ConfigMap", f"{RELEASE}-cairn-trino")]["data"]
    coord = docs[("Deployment", f"{RELEASE}-cairn-trino")]["spec"]["template"]["spec"]
    worker = docs[("Deployment", f"{RELEASE}-cairn-trino-worker")]["spec"]["template"]["spec"]
    # The worker mounts a ConfigMap that exists.
    assert {v["configMap"]["name"] for v in worker["volumes"] if "configMap" in v} == {f"{RELEASE}-cairn-trino"}

    d = tempfile.mkdtemp(prefix="trino-helm-")
    for k, v in cm.items():
        open(f"{d}/{k}", "w").write(v)
    tls_secret(d)
    os.makedirs(f"{d}/tls")
    os.chmod(d, 0o755)
    os.chmod(f"{d}/tls", 0o777)  # the pod's emptyDir, written by the init container as uid 1000
    net = f"cairn-trino-helm-{uuid.uuid4().hex[:6]}"
    subprocess.run(["docker", "network", "create", net], check=True, capture_output=True)
    hosts = [f"--add-host={RELEASE}-cairn-{c}:host-gateway" for c in ("api", "catalog", "store")]
    started = []

    def mounts(container, files):
        out = []
        for m in container.get("volumeMounts") or []:
            src = {"config": f"{d}/{m.get('subPath', '')}", "tls": f"{d}/tls", "tls-src": f"{d}/tls-src"}[m["name"]]
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
                        *mounts(c, cm), c["image"]], check=True, capture_output=True, timeout=600)
        started.append(cname)
        return cname

    coordinator = run("coordinator", coord, alias=f"{RELEASE}-cairn-trino")
    run("worker", worker)
    end = time.time() + 300
    while True:
        logs = subprocess.run(["docker", "logs", coordinator], capture_output=True, text=True).stderr
        if "SERVER STARTED" in logs:
            break
        assert "Configuration errors" not in logs and "Error injecting" not in logs, logs[-4000:]
        assert time.time() < end, "the coordinator did not start"
        time.sleep(1)
    https = cm["config.properties"].split("http-server.https.port=")[1].split()[0]

    def sql(statement, user="admin"):
        token = services.trino_tokens.mint(f"urn:li:corpuser:{user}")["token"]
        out = subprocess.run(["docker", "exec", coordinator, "trino", "--server", f"https://localhost:{https}", "--insecure",
                              "--access-token", token, "--user", user, "--catalog", "lakehouse", "--output-format", "JSON",
                              "--execute", statement], capture_output=True, text=True, timeout=300)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip().splitlines()[-1] if out.stderr.strip() else out.stdout)
        return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]

    sql.coordinator = coordinator
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
