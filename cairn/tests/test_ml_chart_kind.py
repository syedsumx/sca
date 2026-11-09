"""The chart's machine learning workloads (L7) on a real Kubernetes cluster (kind): pods really run.

The chart's Spark training job (templates/ml-workloads.yaml, cairn.mlflow.trainingJobs) runs spark-submit in the
cairn/spark image as a Kubernetes Job, signs in to Cairn with its service account's projected token (no Secret in
the cluster), trains with pyspark.ml and registers the model through Cairn's MLflow as the bound pipeline user, with
the table it trained on as lineage. The chart's model server (cairn.mlflow.modelServers) then serves that registered
version with MLflow's model server; predictions go through Cairn (USE_ML_MODEL checked), and a pod that is not
Cairn's API is refused by the chart's NetworkPolicy.

Needs a kind cluster with the cairn/spark and MLflow images loaded (CAIRN_KIND_KUBECONFIG, CAIRN_KIND_HOST_IP,
CAIRN_SPARK_IMAGE, CAIRN_ML_SERVING_TEST_IMAGE), helm, docker, the store and catalog binaries (Cairn's fixture) and
CAIRN_MLFLOW_PYTHON (the tracking server). CI: job spark-kind."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time

import httpx
import pytest
import yaml

from test_spark_kind_e2e import kind, kubectl  # noqa: F401  (fixture)
from test_trino_e2e import _ready, cairn  # noqa: F401  (fixture)

KUBECONFIG = os.environ.get("CAIRN_KIND_KUBECONFIG", "")
HOST = os.environ.get("CAIRN_KIND_HOST_IP", "")
HELM = os.environ.get("CAIRN_HELM", "helm")
SPARK = os.environ.get("CAIRN_SPARK_IMAGE", "")
SERVER_IMAGE = os.environ.get("CAIRN_ML_SERVING_TEST_IMAGE", "")
PY = os.environ.get("CAIRN_MLFLOW_PYTHON", "")
pytestmark = pytest.mark.skipif(
    not (_ready() and KUBECONFIG and HOST and SPARK and SERVER_IMAGE and shutil.which(HELM) and PY and os.path.exists(PY)),
    reason="needs a kind cluster (CAIRN_KIND_KUBECONFIG, CAIRN_KIND_HOST_IP), CAIRN_SPARK_IMAGE, "
           "CAIRN_ML_SERVING_TEST_IMAGE, helm and CAIRN_MLFLOW_PYTHON")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NS, MODELS = "spark-ml", "cairn"
ADMIN = {"X-Cairn-Actor": "urn:li:corpuser:admin"}

# Trained with pyspark.ml; the fitted line is registered as a model from code (MLflow's own image serves it).
TRAIN = '''
import json
from pyspark.ml.regression import LinearRegression
from pyspark.ml.feature import VectorAssembler
from pyspark.sql import SparkSession
from cairn_spark.mlflow import use_mlflow, log_table_input

spark = SparkSession.builder.appName("spend-training").getOrCreate()
df = spark.createDataFrame([(1, 12.0), (2, 22.0), (3, 32.0), (4, 42.0)], ["tenure", "spend"])
fit = LinearRegression(featuresCol="features", labelCol="spend").fit(
    VectorAssembler(inputCols=["tenure"], outputCol="features").transform(df))
a, b = float(fit.coefficients[0]), float(fit.intercept)
code = ("import mlflow\\nfrom mlflow.pyfunc import PythonModel\\n"
        "class Line(PythonModel):\\n"
        "    def predict(self, context, model_input, params=None):\\n"
        f"        return [round({a!r} * float(x) + {b!r}, 6) for x in model_input['tenure']]\\n"
        "mlflow.models.set_model(Line())\\n")
open("/tmp/line.py", "w").write(code)
mlflow = use_mlflow(experiment="spend-k8s")
with mlflow.start_run() as run:
    log_table_input("sales.customers")
    mlflow.log_metric("coefficient", a)
    mlflow.pyfunc.log_model(name="model", python_model="/tmp/line.py", registered_model_name="spend-spark")
print("CAIRN-RUN " + json.dumps({"run_id": run.info.run_id}), flush=True)
spark.stop()
'''


def _port() -> int:
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]


def _up(url: str, seconds: float, proc=None, log: str = "") -> None:
    end = time.time() + seconds
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(open(log).read()[-3000:])
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{url} did not come up")


@pytest.fixture(scope="module")
def mlflow():
    """MLflow's tracking server where the pods reach it (this host); Cairn fronts it (set before Cairn starts)."""
    d = tempfile.mkdtemp(prefix="mlchart-")
    p = _port()
    server = subprocess.Popen([os.path.join(os.path.dirname(PY), "mlflow"), "server", "--backend-store-uri",
                               f"sqlite:///{d}/mlflow.db", "--artifacts-destination", f"{d}/artifacts",
                               "--serve-artifacts", "--host", "0.0.0.0", "--port", str(p), "--workers", "1"],
                              stdout=open(f"{d}/mlflow.log", "w"), stderr=subprocess.STDOUT)
    _up(f"http://127.0.0.1:{p}/health", 240, server, f"{d}/mlflow.log")
    os.environ["CAIRN_MLFLOW_URL"] = f"http://127.0.0.1:{p}"
    yield {"url": f"http://{HOST}:{p}", "local": f"http://127.0.0.1:{p}", "dir": d}
    os.environ.pop("CAIRN_MLFLOW_URL", None)
    server.terminate()
    server.wait(15)


def render(values: dict, *templates: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(values, f)
    args = [HELM, "template", "t", "helm/cairn", "--namespace", MODELS, "-f", f.name]
    for t in templates:
        args += ["-s", t]
    out = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    os.unlink(f.name)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_the_charts_spark_training_and_model_server_run_on_kubernetes(kind, mlflow, cairn):  # noqa: F811
    url, s = cairn
    assert s.ml is not None, "CAIRN_MLFLOW_URL was not set before Cairn started"
    cairn_url = f"http://{HOST}:{url.rsplit(':', 1)[1]}"
    for ns in (NS, MODELS):
        kubectl("delete", "namespace", ns, "--ignore-not-found", "--wait=true")
        kubectl("create", "namespace", ns)
    r = httpx.post(f"{url}/v1/workload-identity/bindings", headers=ADMIN, timeout=10, json={
        "issuer": "kind", "subject": f"system:serviceaccount:{NS}:svc-spark", "user": "carol"})
    assert r.status_code == 201, r.text
    base = {"cairn": {"workloadIdentity": {"issuers": [{"name": "kind", "issuer": kind["issuer"], "audience": "cairn",
                                                        "jwks_url": f"{kind['issuer']}/openid/v1/jwks"}]},
                      "spark": {"enabled": True, "namespace": NS, "image": SPARK},
                      "mlflow": {"enabled": True, "database": {"secretName": "unused"},
                                 "artifacts": {"credentialsSecret": "unused"}}}}

    # ---- the training job: the chart's Job, its service account, no Secret
    values = json.loads(json.dumps(base))
    values["cairn"]["mlflow"]["trainingJobs"] = {"cairnUrl": cairn_url, "items": [{
        "name": "spend", "script": TRAIN, "master": "local[1]",
        "env": {"MLFLOW_HTTP_REQUEST_MAX_RETRIES": "0"},
        "resources": {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"memory": "2Gi"}}}]}
    manifest = render(values, "templates/spark.yaml", "templates/ml-workloads.yaml")
    docs = [d for d in yaml.safe_load_all(manifest) if d]
    job = next(d for d in docs if d["kind"] == "Job")
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False and pod["serviceAccountName"] == "svc-spark"
    assert not [v for v in pod["volumes"] if "secret" in v], pod["volumes"]
    kubectl("apply", "-f", "-", stdin=manifest)
    name = job["metadata"]["name"]
    end = time.time() + 900
    while True:
        st = json.loads(kubectl("-n", NS, "get", "job", name, "-o", "json")).get("status", {})
        if st.get("succeeded"):
            break
        if st.get("failed") or time.time() > end:
            print(kubectl("-n", NS, "logs", f"job/{name}", "--tail=200"))
            raise AssertionError(f"the training job did not succeed: {st}")
        time.sleep(3)
    log = kubectl("-n", NS, "logs", f"job/{name}")
    run_id = json.loads(log.split("CAIRN-RUN ", 1)[1].splitlines()[0])["run_id"]
    # The run and the model are the bound pipeline user's, with the table it trained on as lineage.
    run = httpx.get(f"{mlflow['local']}/api/2.0/mlflow/runs/get", params={"run_id": run_id}).json()["run"]
    assert {t["key"]: t["value"] for t in run["data"]["tags"]}["mlflow.user"] == "carol"
    assert abs({m["key"]: m["value"] for m in run["data"]["metrics"]}["coefficient"] - 10.0) < 1e-6
    assert s.ml.owner("spend-spark") == "urn:li:corpuser:carol"
    props = s.graph.get_aspect("urn:li:mlModel:(urn:li:dataPlatform:mlflow,spend-spark.v1,PROD)", "mlModelProperties")
    (training,) = props["trainingJobs"]
    # Named as the rest of the catalog names the table: through the lakehouse (Trino's name) when there is one.
    lake = getattr(s, "lakehouse", None)
    want = lake.urn(["sales"], "customers") if lake is not None else \
        "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.customers,PROD)"
    assert s.graph.get_aspect(training, "dataJobInputOutput")["inputDatasets"] == [want]

    # ---- the model server: the chart's Deployment of the registered version, reached only by Cairn's API
    values = json.loads(json.dumps(base))
    values["cairn"]["mlflow"]["modelServers"] = {"trackingUri": mlflow["url"], "image": SERVER_IMAGE,
                                                 "items": [{"name": "spend", "model": "spend-spark", "version": "1"}]}
    manifest = render(values, "templates/ml-workloads.yaml")
    kubectl("-n", MODELS, "apply", "-f", "-", stdin=manifest)
    try:
        kubectl("-n", MODELS, "rollout", "status", "deployment/t-cairn-model-spend", "--timeout=600s")
    except RuntimeError:
        print(kubectl("-n", MODELS, "logs", "deployment/t-cairn-model-spend", "--tail=200"))
        raise
    # Cairn's API forwards to the Service (the chart wires CAIRN_ML_SERVING); here through a port-forward.
    api_env = [d for d in yaml.safe_load_all(render(values, "templates/api.yaml")) if d and d["kind"] == "Deployment"]
    serving = json.loads(next(e["value"] for e in api_env[0]["spec"]["template"]["spec"]["containers"][0]["env"]
                              if e["name"] == "CAIRN_ML_SERVING"))
    assert serving == {"spend-spark": "http://t-cairn-model-spend:8080"}
    fp = _port()
    pf = subprocess.Popen(["kubectl", "--kubeconfig", KUBECONFIG, "-n", MODELS, "port-forward", "svc/t-cairn-model-spend",
                           f"{fp}:8080"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _up(f"http://127.0.0.1:{fp}/ping", 60)
        s.ml.serving["spend-spark"] = f"http://127.0.0.1:{fp}"
        rows = {"dataframe_split": {"columns": ["tenure"], "data": [[5], [10]]}}
        r = httpx.post(f"{url}/v1/ml/serve/spend-spark/invocations", json=rows, timeout=120,
                       headers={"X-Cairn-Actor": "urn:li:corpuser:carol"})
        assert r.status_code == 200, r.text
        assert r.json()["predictions"] == [52.0, 102.0]
        assert httpx.post(f"{url}/v1/ml/serve/spend-spark/invocations", json=rows, timeout=60,
                          headers={"X-Cairn-Actor": "urn:li:corpuser:bob"}).status_code == 403    # no USE_ML_MODEL
    finally:
        pf.terminate()
        pf.wait(15)
    # Only Cairn's API pods reach the model server: anything else is refused by the chart's NetworkPolicy.
    probe = subprocess.run(["kubectl", "--kubeconfig", KUBECONFIG, "-n", MODELS, "run", "intruder", "--rm", "-i",
                            "--restart=Never", f"--image={SERVER_IMAGE}", "--image-pull-policy=IfNotPresent", "--command",
                            "--", "python", "-c",
                            "import urllib.request\ntry:\n urllib.request.urlopen('http://t-cairn-model-spend:8080/ping',"
                            " timeout=5)\n print('REACHED')\nexcept Exception as e:\n print('REFUSED', type(e).__name__)"],
                           capture_output=True, text=True, timeout=300)
    assert "REFUSED" in probe.stdout and "REACHED" not in probe.stdout, (probe.stdout, probe.stderr)
    for ns in (NS, MODELS):
        kubectl("delete", "namespace", ns, "--ignore-not-found", "--wait=false")
