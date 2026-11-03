"""Spark on Kubernetes without a secret in the cluster, against a real Kubernetes API server and the real
cairn/spark image: cluster-mode spark-submit through cairn-spark (what lands in the driver pod and its
ConfigMap), the driver signing itself in with the pod's projected token, Spark Operator
SparkApplications checked by the operator's own CRD, and the Airflow task that runs them.

Needs CAIRN_K8S_BIN_DIR (kube-apiserver, etcd, kubectl), docker with CAIRN_SPARK_IMAGE, the store and
catalog binaries; the Airflow test also needs CAIRN_AIRFLOW_IMAGE and CAIRN_SPARK_OPERATOR_CRD (the
Spark Operator's sparkapplications CRD file). No kubelet runs, so no pod starts: what Spark and the
operator write to the cluster is what is checked, and the driver runs in a container with the
environment and token its pod spec gives it."""

import json
import os
import shutil
import subprocess
import threading
import time

import httpx
import pytest

from test_spark_e2e import ANA, DEV, IMAGE, JOB, lake, people  # noqa: F401  (fixtures)
from test_trino_e2e import _ready, cairn, port, wait  # noqa: F401  (fixtures)
from test_workload_identity_e2e import BIN, k8s  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not (_ready() and IMAGE and BIN and os.path.exists(os.path.join(BIN, "kube-apiserver"))),
                                reason="needs docker, CAIRN_SPARK_IMAGE, the binaries and CAIRN_K8S_BIN_DIR")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AIRFLOW = os.environ.get("CAIRN_AIRFLOW_IMAGE", "")
CRD = os.environ.get("CAIRN_SPARK_OPERATOR_CRD", "")
ADMIN = {"X-Cairn-Actor": "urn:li:corpuser:admin"}


def client_files(k8s, tmp_path):  # noqa: F811
    """The admin's client certificate, readable by the container's user."""
    d = tmp_path / "kube"
    d.mkdir()
    for f in ("ca.crt", "admin.crt", "admin.key"):
        shutil.copy(os.path.join(k8s["dir"], f), d / f)
        os.chmod(d / f, 0o644)
    os.chmod(d, 0o755)
    (d / "config").write_text(open(os.path.join(k8s["dir"], "kubeconfig")).read().replace(k8s["dir"], "/kube"))
    os.chmod(d / "config", 0o644)
    return d


@pytest.fixture(scope="module")
def spark_sa(k8s, cairn, lake):  # noqa: F811
    url, _ = cairn
    k8s["kubectl"]("-n", "pipelines", "create", "serviceaccount", "svc-spark")
    r = httpx.post(f"{url}/v1/workload-identity/bindings", headers=ADMIN, timeout=10, json={
        "issuer": "k8s", "subject": "system:serviceaccount:pipelines:svc-spark", "user": "ana"})
    assert r.status_code == 201, r.text
    return "svc-spark"


def test_cluster_mode_puts_no_secret_in_the_cluster_and_the_driver_signs_in_itself(k8s, cairn, lake, spark_sa,  # noqa: F811
                                                                                    tmp_path, monkeypatch):
    url, s = cairn
    s.lakehouse.catalog_uris = (lake["catalog"],)
    kube = client_files(k8s, tmp_path)
    submit = ["--master", f"k8s://{k8s['issuer']}", "--deploy-mode", "cluster", "--name", "orders",
              "--conf", "spark.kubernetes.namespace=pipelines", "--conf", f"spark.kubernetes.container.image={IMAGE}",
              "--conf", "spark.kubernetes.authenticate.driver.serviceAccountName=svc-spark",
              "--conf", "spark.kubernetes.submission.waitAppCompletion=false",
              "--conf", "spark.kubernetes.authenticate.submission.caCertFile=/kube/ca.crt",
              "--conf", "spark.kubernetes.authenticate.submission.clientCertFile=/kube/admin.crt",
              "--conf", "spark.kubernetes.authenticate.submission.clientKeyFile=/kube/admin.key"]

    def cairn_spark(*args):
        # The submitter has a Cairn token in its environment: none of it may reach the cluster.
        return subprocess.run(["docker", "run", "--rm", "--network", "host", "-v", f"{kube}:/kube:ro",
                               "-e", f"CAIRN_URL={url}", "-e", f"CAIRN_TOKEN={ANA}", "-e", f"CAIRN_CATALOG_URI={lake['catalog']}",
                               IMAGE, "cairn-spark", "submit", *args], capture_output=True, text=True, timeout=600)

    r = cairn_spark(*submit, "local:///job/job.py", "count=analytics.products")
    assert r.returncode == 0, (r.stdout + r.stderr)[-5000:]
    assert "its driver signs in as service account svc-spark" in r.stderr
    pods = json.loads(k8s["kubectl"]("-n", "pipelines", "get", "pods", "-o", "json"))["items"]
    driver = [p for p in pods if p["metadata"]["labels"].get("spark-role") == "driver"]
    assert len(driver) == 1, [p["metadata"]["name"] for p in pods]
    spec = driver[0]["spec"]
    assert spec["serviceAccountName"] == "svc-spark"
    vol = [v for v in spec["volumes"] if v["name"] == "cairn-identity"][0]
    assert vol["projected"]["sources"][0]["serviceAccountToken"]["audience"] == "cairn"
    c = spec["containers"][0]
    assert {"name": "cairn-identity", "mountPath": "/var/run/secrets/cairn", "readOnly": True} in c["volumeMounts"]
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert env["CAIRN_WORKLOAD_TOKEN_FILE"] == "/var/run/secrets/cairn/token" and env["CAIRN_URL"] == url
    assert env["CAIRN_CATALOG_URI"] == lake["catalog"]
    # Nothing secret in what Spark stored in the cluster: pods, ConfigMaps, Secrets.
    stored = k8s["kubectl"]("-n", "pipelines", "get", "pods,configmaps,secrets,services", "-o", "json")
    conf = [cm for cm in json.loads(k8s["kubectl"]("-n", "pipelines", "get", "configmaps", "-o", "json"))["items"]
            if "spark.properties" in (cm.get("data") or {})]
    assert conf, "spark-submit stores the driver's settings in a ConfigMap"
    props = conf[0]["data"]["spark.properties"]
    assert "spark.kubernetes.driverEnv.CAIRN_WORKLOAD_TOKEN_FILE" in props
    assert ANA not in stored
    for leak in (".credential", "apiKey", "auth.apiKey", "oauth2", "CAIRN_TOKEN"):
        assert leak not in props, leak

    # A JVM program: its driver pod runs cairn-spark-driver, which signs in inside the pod; still no secret.
    r = cairn_spark(*submit, "--class", "org.apache.spark.examples.SparkPi", "local:///opt/spark/examples/jars/spark-examples.jar")
    assert r.returncode == 0, (r.stdout + r.stderr)[-4000:]
    after = json.loads(k8s["kubectl"]("-n", "pipelines", "get", "pods", "-o", "json"))["items"]
    jvm = [p for p in after if p["metadata"]["labels"].get("spark-role") == "driver"
           and p["metadata"]["name"] not in {d["metadata"]["name"] for d in driver}]
    assert len(jvm) == 1, [p["metadata"]["name"] for p in after]
    assert jvm[0]["spec"]["containers"][0]["command"] == ["cairn-spark-driver"]
    assert ANA not in k8s["kubectl"]("-n", "pipelines", "get", "pods,configmaps,secrets", "-o", "json")

    # That driver, as its pod runs it: cairn-spark-driver with the pod's environment, the token the
    # kubelet projects and Spark's own driver arguments. A JVM program (spark-sql) reads as ana.
    sa_token = k8s["kubectl"]("-n", "pipelines", "create", "token", "svc-spark", "--audience", "cairn", "--duration", "1h")
    (tmp_path / "jvm").mkdir()
    (tmp_path / "jvm" / "token").write_text(sa_token)
    (tmp_path / "jvm" / "spark.properties").write_text("spark.master=local[1]\nspark.app.name=jvm-on-k8s\nspark.ui.enabled=false\n")
    for f in ("", "token", "spark.properties"):
        os.chmod(tmp_path / "jvm" / f if f else tmp_path / "jvm", 0o755 if not f else 0o644)
    jenv = {e["name"]: e.get("value") for e in jvm[0]["spec"]["containers"][0]["env"] if e["name"].startswith("CAIRN_")}
    r = subprocess.run(["docker", "run", "--rm", "--network", "host", "-v", f"{tmp_path / 'jvm'}:/var/run/secrets/cairn:ro",
                        "-v", f"{tmp_path / 'jvm' / 'spark.properties'}:/opt/spark/conf/spark.properties:ro",
                        "-e", "SPARK_DRIVER_BIND_ADDRESS=127.0.0.1", *DEV,
                        *[a for k, v in jenv.items() for a in ("-e", f"{k}={v}")],
                        "--entrypoint", "cairn-spark-driver", IMAGE, "driver", "--properties-file",
                        "/opt/spark/conf/spark.properties", "--class",
                        "org.apache.spark.sql.hive.thriftserver.SparkSQLCLIDriver", "spark-internal",
                        "-e", "SELECT count(*) FROM lakehouse.analytics.products"],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0 and r.stdout.strip().splitlines()[-1] == "3", (r.stdout + r.stderr)[-4000:]
    assert "the driver runs as ana" in r.stderr

    # The driver, as its pod would run it: the env above and the token the kubelet projects (the cluster
    # issues it: TokenRequest). It signs in as ana, reads and writes governed tables, and its run is priced.
    monkeypatch.setenv("CAIRN_COST_SPARK_USD_PER_CORE_HOUR", "0.10")
    token = k8s["kubectl"]("-n", "pipelines", "create", "token", "svc-spark", "--audience", "cairn", "--duration", "1h")
    (tmp_path / "sa").mkdir()
    (tmp_path / "sa" / "token").write_text(token)
    (tmp_path / "job.py").write_text(JOB)
    for p in (tmp_path / "sa", tmp_path / "sa" / "token", tmp_path / "job.py"):
        os.chmod(p, 0o755)
    r = subprocess.run(["docker", "run", "--rm", "--network", "host", "-v", f"{tmp_path / 'sa'}:/var/run/secrets/cairn:ro",
                        "-v", f"{tmp_path / 'job.py'}:/job/job.py:ro",
                        *[a for k, v in env.items() if k.startswith("CAIRN_") for a in ("-e", f"{k}={v}")],
                        IMAGE, "/opt/spark/bin/spark-submit", "--master", "local[2]", "--conf", "spark.ui.enabled=false",
                        "/job/job.py", "count=analytics.products",
                        "sql=CREATE TABLE lakehouse.analytics.k8s_products AS SELECT * FROM lakehouse.analytics.products",
                        "sql=SELECT count(*) FROM lakehouse.hr.salaries"],
                       capture_output=True, text=True, timeout=900)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("CAIRN-RESULT ")), None)
    assert line, (r.stdout + r.stderr)[-6000:]
    out = json.loads(line[len("CAIRN-RESULT "):])
    assert out["count=analytics.products"] == {"ok": 3}, out
    if "ok" not in out["sql=CREATE TABLE lakehouse.analytics.k8s_products AS SELECT * FROM lakehouse.analytics.products"]:
        pytest.fail("the write was refused:\n" + json.dumps(out, indent=1))   # in full: pytest shortens assertion text
    assert "error" in out["sql=SELECT count(*) FROM lakehouse.hr.salaries"], out   # ana may not read hr
    ev = [e for e in s.audit.entries("identity", limit=50) if e["action"] == "workload.token_issued"
          and e["details"]["subject"] == "system:serviceaccount:pipelines:svc-spark"]
    assert ev, "the driver signed in with its workload token"

    # Its compute: 2 cores (local[2]) for the application's wall-clock time, charged to the table it wrote.
    for _ in range(60):
        jobs = [j for j in s.costs.jobs(days=2) if j["engine"] == "spark"]
        if jobs:
            break
        time.sleep(1)
    assert jobs and jobs[0]["usd"] > 0 and jobs[0]["seconds"] > 1, s.costs.jobs(days=2)
    from sqlalchemy import select

    from cairn.reports.costs import asset_costs, run_costs
    with s.engine.connect() as conn:
        charged = {r.urn: r.compute_usd for r in conn.execute(select(asset_costs).where(asset_costs.c.source == "spark"))}
        app = conn.execute(select(run_costs).where(run_costs.c.parent.is_(None))).first()
    assert app.cores == 2 and any("k8s_products" in u for u in charged), charged
    assert sum(charged.values()) == pytest.approx(app.usd, abs=1e-5)


@pytest.mark.skipif(not (AIRFLOW and CRD), reason="needs CAIRN_AIRFLOW_IMAGE and CAIRN_SPARK_OPERATOR_CRD")
def test_spark_applications_from_airflow_through_the_spark_operator(k8s, tmp_path):  # noqa: F811
    kubectl = k8s["kubectl"]
    kubectl("create", "-f", CRD)
    for _ in range(60):  # the API server reports the CRD established shortly after it is created
        crd = json.loads(kubectl("get", "crd", "sparkapplications.sparkoperator.k8s.io", "-o", "json"))
        if any(c["type"] == "Established" and c["status"] == "True" for c in (crd.get("status") or {}).get("conditions") or []):
            break
        time.sleep(1)
    else:
        pytest.fail("the SparkApplication CRD was never established")
    import sys
    sys.path.insert(0, os.path.join(ROOT, "spark", "src"))
    from cairn_spark.kubernetes import spark_application

    # The operator's own schema accepts what cairn_spark builds (strict: no unknown fields).
    app = spark_application("check", image=IMAGE, main="/jobs/orders.py", service_account="svc-spark",
                            cairn_url="https://cairn.example.com/api", catalog_uri="https://catalog.example.com",
                            namespace="pipelines", executors=3, executor_cores=2)
    (tmp_path / "app.json").write_text(json.dumps(app))
    out = json.loads(kubectl("create", "--dry-run=server", "--validate=strict", "-o", "json", "-f", str(tmp_path / "app.json")))
    assert out["spec"]["driver"]["serviceAccount"] == "svc-spark" and out["spec"]["executor"]["instances"] == 3
    assert set(out["spec"]["sparkConf"]) == {"spark.redaction.regex"}   # no credential, no token
    # A JVM program: its driver's command is cairn-spark-driver (the operator's schema takes the template).
    jvm = spark_application("check-jvm", image=IMAGE, main="local:///jobs/orders.jar", main_class="com.acme.Orders",
                            service_account="svc-spark", cairn_url="https://cairn.example.com/api",
                            catalog_uri="https://catalog.example.com", namespace="pipelines")
    (tmp_path / "jvm.json").write_text(json.dumps(jvm))
    out = json.loads(kubectl("create", "--dry-run=server", "--validate=strict", "-o", "json", "-f", str(tmp_path / "jvm.json")))
    assert out["spec"]["type"] == "Scala" and out["spec"]["mainClass"] == "com.acme.Orders"
    assert out["spec"]["driver"]["template"]["spec"]["containers"][0]["command"] == ["cairn-spark-driver"]
    with pytest.raises(ValueError):
        spark_application("bad", image=IMAGE, main="/jobs/x.jar", service_account="s", cairn_url="u", catalog_uri="c")
    with pytest.raises(ValueError):
        spark_application("bad", image=IMAGE, main="/jobs/x.py", service_account="s", cairn_url="u", catalog_uri="c",
                          spark_conf={"spark.sql.catalog.lakehouse.credential": "x:y"})

    # The Airflow task creates the application and follows it; here the test is the Spark Operator's
    # controller, moving each application to the state the operator would.
    kube = client_files(k8s, tmp_path)
    script = '''
import sys, types
from cairn_airflow.spark import CairnSparkApplicationOperator
def run(name, try_number):
    op = CairnSparkApplicationOperator(task_id=name, name=name, namespace="pipelines", service_account="svc-spark",
        image="cairn/spark:0.1.0", main="/jobs/orders.py", cairn_url="https://cairn.example.com/api",
        catalog_uri="https://catalog.example.com", kube_config_file="/kube/config", poll_interval=0.5)
    ti = types.SimpleNamespace(try_number=try_number, dag_id="shop", task_id=name)
    try:
        print("RESULT", name, op.execute({"run_id": "manual__2026-11-02", "ti": ti}), flush=True)
    except Exception as e:
        print("FAILED", name, type(e).__name__, e, flush=True)
run("orders", 1)
run("refunds", 1)
'''
    dev = ["-v", f"{ROOT}/airflow-provider/src:/opt/cairn-provider:ro", "-v", f"{ROOT}/spark/src:/opt/cairn-spark:ro",
           "-e", "PYTHONPATH=/opt/cairn-provider:/opt/cairn-spark"] if os.environ.get("CAIRN_AIRFLOW_DEV_SRC") == "1" else []
    proc = subprocess.Popen(["docker", "run", "--rm", "--network", "host", "-v", f"{kube}:/kube:ro", *dev, AIRFLOW,
                             "python", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    seen, errors = {}, []

    def controller():
        try:
            follow()
        except Exception as e:  # noqa: BLE001 - reported below, and the task is stopped
            errors.append(e)
            proc.kill()

    def follow():
        deadline = time.time() + 300
        while time.time() < deadline and len(seen) < 2:
            items = json.loads(kubectl("-n", "pipelines", "get", "sparkapplications", "-o", "json"))["items"]
            for a in items:
                n = a["metadata"]["name"]
                if n in seen:
                    continue
                seen[n] = a
                driver = {"podName": f"{n}-driver"}
                status = ({"applicationState": {"state": "COMPLETED"}, "sparkApplicationId": "spark-0001"}
                          if n.startswith("orders") else
                          {"applicationState": {"state": "FAILED", "errorMessage": "driver container failed with ExitCode: 1"}})
                for state in ({"applicationState": {"state": "SUBMITTED"}}, {"applicationState": {"state": "RUNNING"}}, status):
                    kubectl("-n", "pipelines", "patch", "sparkapplication", n, "--subresource=status", "--type=merge",
                            "-p", json.dumps({"status": {"driverInfo": driver, **state}}))
                    time.sleep(1)
            time.sleep(0.5)

    t = threading.Thread(target=controller, daemon=True)
    t.start()
    log, _ = proc.communicate(timeout=600)
    t.join(10)
    assert not errors, errors
    assert "RESULT orders" in log and "'state': 'COMPLETED'" in log and "spark-0001" in log, log[-4000:]
    assert "FAILED refunds AirflowException" in log and "ExitCode: 1" in log, log[-4000:]
    orders = [a for n, a in seen.items() if n.startswith("orders")][0]
    assert orders["metadata"]["labels"]["airflow.apache.org/dag-id"] == "shop"
    assert orders["spec"]["driver"]["serviceAccount"] == "svc-spark"
    assert {"name": "CAIRN_WORKLOAD_TOKEN_FILE", "value": "/var/run/secrets/cairn/token"} in orders["spec"]["driver"]["env"]
    assert not [v for v in orders["spec"]["volumes"] if v["name"] != "cairn-identity"]
    assert "volumeMounts" not in orders["spec"]["executor"]   # executors get no identity
