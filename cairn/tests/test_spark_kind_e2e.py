"""Spark on a full Kubernetes cluster (kind), end to end: pods really run. cairn-spark submits a job in
cluster mode; the driver pod starts, signs itself in to Cairn with its projected service-account token
(no secret in the cluster), starts executor pods, reads a governed lakehouse table through Cairn Catalog,
is refused a table its user may not read, and finishes; Cairn records the sign-in.

Needs a kind cluster with the cairn/spark image loaded (CAIRN_KIND_KUBECONFIG), the address pods reach this
host at (CAIRN_KIND_HOST_IP, the kind network's gateway), CAIRN_SPARK_IMAGE, docker and the store and
catalog binaries. CI runs it (the kind job); a laptop with kind can too."""

import base64
import json
import os
import subprocess
import tempfile
import httpx
import pytest
import yaml

from test_spark_e2e import ANA, IMAGE, lake, people  # noqa: F401  (fixtures)
from test_trino_e2e import CATALOG_TOKEN, _ready, cairn, port, wait  # noqa: F401  (fixtures)

KUBECONFIG = os.environ.get("CAIRN_KIND_KUBECONFIG", "")
HOST = os.environ.get("CAIRN_KIND_HOST_IP", "")
pytestmark = pytest.mark.skipif(not (_ready() and IMAGE and KUBECONFIG and HOST),
                                reason="needs a kind cluster (CAIRN_KIND_KUBECONFIG, CAIRN_KIND_HOST_IP), CAIRN_SPARK_IMAGE")
ADMIN = {"X-Cairn-Actor": "urn:li:corpuser:admin"}

JOB = """
import json, sys
from cairn_spark import lakehouse_session

spark = lakehouse_session("orders-on-k8s")            # the master is spark-submit's: Kubernetes
out = {"executors": spark.sparkContext._jsc.sc().getExecutorMemoryStatus().size() - 1}
try:
    out["products"] = spark.sql("SELECT count(*) AS n FROM lakehouse.analytics.products").collect()[0].n
except Exception as e:
    out["products_error"] = str(e)[:300]
try:
    spark.sql("SELECT count(*) FROM lakehouse.hr.salaries").collect()
    out["salaries"] = "read"
except Exception as e:
    out["salaries"] = "refused" if "not let" in str(e) or "Forbidden" in str(e) or "403" in str(e) else str(e)[:300]
print("CAIRN-RESULT " + json.dumps(out), flush=True)
spark.stop()
"""


def kubectl(*args, stdin=None):
    r = subprocess.run(["kubectl", "--kubeconfig", KUBECONFIG, *args], capture_output=True, text=True, input=stdin)
    if r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr}")
    return r.stdout


@pytest.fixture(scope="module")
def kind():
    """The cluster's service-account issuer, trusted by Cairn (set before Cairn starts)."""
    issuer = json.loads(kubectl("get", "--raw", "/.well-known/openid-configuration"))["issuer"]
    d = tempfile.mkdtemp(prefix="kind-")
    open(f"{d}/jwks.json", "w").write(kubectl("get", "--raw", "/openid/v1/jwks"))
    os.environ["CAIRN_WORKLOAD_ISSUERS"] = json.dumps([{"name": "kind", "issuer": issuer, "audience": "cairn",
                                                        "jwks_file": f"{d}/jwks.json"}])
    yield {"issuer": issuer, "dir": d}
    os.environ.pop("CAIRN_WORKLOAD_ISSUERS", None)


def credentials(d):
    """The admin's client certificate from the kubeconfig, as files for spark-submit."""
    cfg = yaml.safe_load(open(KUBECONFIG))
    cluster, user = cfg["clusters"][0]["cluster"], cfg["users"][0]["user"]
    os.makedirs(f"{d}/kube", exist_ok=True)
    for name, data in (("ca.crt", cluster["certificate-authority-data"]), ("admin.crt", user["client-certificate-data"]),
                       ("admin.key", user["client-key-data"])):
        open(f"{d}/kube/{name}", "wb").write(base64.b64decode(data))
        os.chmod(f"{d}/kube/{name}", 0o644)
    os.chmod(f"{d}/kube", 0o755)
    return cluster["server"]


@pytest.fixture(scope="module")
def pod_catalog(lake, kind):  # noqa: F811
    """A catalog that hands pods the store's address as they reach it (this host), with the tables."""
    import pyarrow as pa
    from pyiceberg.catalog.rest import RestCatalog

    cp = port()
    sp = lake["store"].rsplit(":", 1)[1]
    env = {**lake["env"], "CAIRN_CATALOG_ADDR": f"0.0.0.0:{cp}", "CAIRN_CATALOG_DATA": f"{kind['dir']}/catalog",
           "CAIRN_CATALOG_CLIENT_S3_ENDPOINT": f"http://{HOST}:{sp}"}
    p = subprocess.Popen([os.environ["CAIRN_CATALOG_BIN"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    wait(cp)
    admin = RestCatalog("admin", uri=f"http://127.0.0.1:{cp}", token=CATALOG_TOKEN)
    for ns in ("analytics", "hr"):
        admin.create_namespace(ns)
    prod = pa.table({"sku": ["a", "b", "c"], "price": pa.array([5, 7, 9], pa.int64())})
    admin.create_table("analytics.products", schema=prod.schema).append(prod)
    sal = pa.table({"name": ["jane"], "salary": pa.array([100], pa.int64())})
    admin.create_table("hr.salaries", schema=sal.schema).append(sal)
    yield f"http://{HOST}:{cp}"
    p.terminate()
    p.wait(15)


def test_a_cluster_mode_job_runs_in_pods_signed_in_by_its_service_account(kind, cairn, pod_catalog):  # noqa: F811
    url, s = cairn
    # What pods reach: this host, at the kind network's gateway.
    cairn_url, catalog = f"http://{HOST}:{url.rsplit(':', 1)[1]}", pod_catalog
    s.lakehouse.catalog_uris = (catalog,)
    kubectl("create", "namespace", "pipelines")
    kubectl("-n", "pipelines", "create", "serviceaccount", "svc-spark")
    # The driver starts its executors: it may manage pods, services and config maps in its namespace.
    kubectl("-n", "pipelines", "create", "role", "spark-driver", "--verb=*", "--resource=pods,services,configmaps,persistentvolumeclaims")
    kubectl("-n", "pipelines", "create", "rolebinding", "spark-driver", "--role=spark-driver", "--serviceaccount=pipelines:svc-spark")
    r = httpx.post(f"{url}/v1/workload-identity/bindings", headers=ADMIN, timeout=10, json={
        "issuer": "kind", "subject": "system:serviceaccount:pipelines:svc-spark", "user": "ana"})
    assert r.status_code == 201, r.text
    server = credentials(kind["dir"])
    kubectl("-n", "pipelines", "create", "configmap", "job", f"--from-literal=job.py={JOB}")
    open(f"{kind['dir']}/kube/driver.yaml", "w").write(yaml.safe_dump({
        "apiVersion": "v1", "kind": "Pod",
        "spec": {"volumes": [{"name": "job", "configMap": {"name": "job"}}],
                 "containers": [{"name": "spark-kubernetes-driver", "volumeMounts": [{"name": "job", "mountPath": "/job"}]}]}}))
    os.chmod(f"{kind['dir']}/kube/driver.yaml", 0o644)

    submit = ["--master", f"k8s://{server}", "--deploy-mode", "cluster", "--name", "orders",
              "--conf", "spark.kubernetes.namespace=pipelines", "--conf", f"spark.kubernetes.container.image={IMAGE}",
              "--conf", "spark.kubernetes.container.image.pullPolicy=Never",
              "--conf", "spark.kubernetes.authenticate.driver.serviceAccountName=svc-spark",
              "--conf", "spark.executor.instances=1", "--conf", "spark.kubernetes.executor.deleteOnTermination=false",
              "--conf", "spark.driver.memory=1g", "--conf", "spark.executor.memory=1g",
              "--conf", "spark.kubernetes.driver.request.cores=0.5", "--conf", "spark.kubernetes.executor.request.cores=0.5",
              # The job's file comes from a config map (Spark's volume confs do not take config maps; a pod template does).
              "--conf", "spark.kubernetes.driver.podTemplateFile=/kube/driver.yaml",
              "--conf", "spark.kubernetes.driver.podTemplateContainerName=spark-kubernetes-driver",
              "--conf", "spark.kubernetes.submission.waitAppCompletion=true",
              "--conf", "spark.kubernetes.authenticate.submission.caCertFile=/kube/ca.crt",
              "--conf", "spark.kubernetes.authenticate.submission.clientCertFile=/kube/admin.crt",
              "--conf", "spark.kubernetes.authenticate.submission.clientKeyFile=/kube/admin.key"]
    r = subprocess.run(["docker", "run", "--rm", "--network", "host", "-v", f"{kind['dir']}/kube:/kube:ro",
                        "-e", f"CAIRN_URL={cairn_url}", "-e", f"CAIRN_TOKEN={ANA}", "-e", f"CAIRN_CATALOG_URI={catalog}",
                        IMAGE, "cairn-spark", "submit", *submit, "local:///job/job.py"],
                       capture_output=True, text=True, timeout=1200)
    pods = json.loads(kubectl("-n", "pipelines", "get", "pods", "-o", "json"))["items"]
    driver = [p for p in pods if p["metadata"]["labels"].get("spark-role") == "driver"]
    assert driver, (r.stdout + r.stderr)[-4000:]
    logs = kubectl("-n", "pipelines", "logs", driver[0]["metadata"]["name"])
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:] + logs[-5000:]
    # It ran in the cluster: the driver pod finished, and it had an executor pod.
    assert driver[0]["status"]["phase"] == "Succeeded", driver[0]["status"]
    assert [p for p in pods if p["metadata"]["labels"].get("spark-role") == "executor"], [p["metadata"]["name"] for p in pods]
    line = next(ln for ln in logs.splitlines() if ln.startswith("CAIRN-RESULT "))
    out = json.loads(line[len("CAIRN-RESULT "):])
    assert out["products"] == 3 and out["salaries"] == "refused" and out["executors"] >= 1, out
    # Signed in with its pod's token; no Cairn token anywhere in the cluster.
    assert ANA not in kubectl("-n", "pipelines", "get", "pods,configmaps,secrets", "-o", "json")
    ev = [e for e in s.audit.entries("identity", limit=50) if e["action"] == "workload.token_issued"
          and e["details"]["subject"] == "system:serviceaccount:pipelines:svc-spark"]
    assert ev, "the driver signed in with its workload token"
