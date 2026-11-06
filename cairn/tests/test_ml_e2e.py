"""Machine learning through Cairn, end to end, with a real MLflow server, the real MLflow client and a real
model server: a person trains a scikit-learn model on a lakehouse table and registers it through Cairn
(MLFLOW_TRACKING_URI=<cairn>/v1/ml, their Cairn token). The run is theirs whatever the client claims;
the model belongs to them and appears in the catalog with lineage from its training table; others
neither see nor change it without a policy; predictions go through Cairn for those allowed.

Needs CAIRN_MLFLOW_PYTHON: a Python with mlflow (3.x) and scikit-learn."""

import json
import os
import socket
import subprocess
import tempfile
import textwrap
import threading
import time

import httpx
import pytest

PY = os.environ.get("CAIRN_MLFLOW_PYTHON", "")
pytestmark = pytest.mark.skipif(not (PY and os.path.exists(PY)), reason="needs CAIRN_MLFLOW_PYTHON (mlflow, scikit-learn)")

TOKENS = {"tok-ana": "urn:li:corpuser:ana", "tok-bob": "urn:li:corpuser:bob", "tok-carol": "urn:li:corpuser:carol"}


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait(url, seconds=90):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{url} did not come up")


@pytest.fixture(scope="module")
def ml():
    import uvicorn
    from fastapi import FastAPI, HTTPException
    from sqlalchemy import create_engine
    from starlette.requests import Request

    from cairn.common.audit import AuditLog
    from cairn.common.core import InMemoryGraph
    from cairn.common.db import init_db
    from cairn.common.security import Principal
    from cairn.ml.mlflow_gateway import MlflowGateway, build_ml_router

    d = tempfile.mkdtemp(prefix="ml-")
    mp = port()
    bindir = os.path.dirname(PY)
    server = subprocess.Popen([f"{bindir}/mlflow", "server", "--backend-store-uri", f"sqlite:///{d}/mlflow.db",
                               "--artifacts-destination", f"{d}/artifacts", "--serve-artifacts",
                               "--host", "127.0.0.1", "--port", str(mp), "--workers", "1"],
                              stdout=open(f"{d}/mlflow.log", "w"), stderr=subprocess.STDOUT)
    wait(f"http://127.0.0.1:{mp}/health")
    engine = create_engine(f"sqlite:///{d}/cairn.db")
    init_db(engine)
    grants: set = set()
    graph = InMemoryGraph()
    gw = MlflowGateway(f"http://127.0.0.1:{mp}", engine, AuditLog(engine), graph,
                       lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{'.'.join(ns)}.{t},PROD)",
                       lambda p, priv, urn: (p.urn, priv, urn) in grants)

    from cairn.ml.monitoring import ModelMonitoring, build_monitoring_router

    sent: list = []
    mon = ModelMonitoring(engine, gw.may_view, gw.may_manage, gw.owner, None,
                          lambda *a: sent.append(a), AuditLog(engine))
    gw.monitoring = mon

    def who(request: Request) -> Principal:
        urn = TOKENS.get(request.headers.get("authorization", "").removeprefix("Bearer "))
        if urn is None:
            raise HTTPException(401, "sign in")
        return Principal(urn, "token")
    app = FastAPI()
    app.include_router(build_monitoring_router(mon, who))      # before MLflow's pass-through, as in Cairn
    app.include_router(build_ml_router(gw, who))
    cp = port()
    api = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cp, timeout_keep_alive=75, log_level="warning"))
    threading.Thread(target=api.run, daemon=True).start()
    wait(f"http://127.0.0.1:{cp}/docs")
    yield {"cairn": f"http://127.0.0.1:{cp}", "mlflow": f"http://127.0.0.1:{mp}", "gw": gw, "graph": graph,
           "grants": grants, "engine": engine, "dir": d,
           "mon": mon, "sent": sent}
    api.should_exit = True
    server.terminate()
    server.wait(15)


TRAIN = textwrap.dedent('''
    import json, mlflow, pandas as pd
    from sklearn.linear_model import LogisticRegression
    df = pd.DataFrame({"tenure": [1, 2, 3, 20, 30, 40], "spend": [5, 7, 6, 50, 60, 70], "churned": [1, 1, 1, 0, 0, 0]})
    mlflow.set_experiment("churn")
    with mlflow.start_run(tags={"mlflow.user": "mallory"}) as run:          # a claim Cairn overrides
        mlflow.log_input(mlflow.data.from_pandas(df, source="lakehouse.sales.customers", name="customers"),
                         context="training")
        model = LogisticRegression().fit(df[["tenure", "spend"]], df["churned"])
        mlflow.log_metric("accuracy", float(model.score(df[["tenure", "spend"]], df["churned"])))
        mlflow.sklearn.log_model(model, name="model", registered_model_name="churn",
                                 input_example=df[["tenure", "spend"]].head(2))
    print("CAIRN-RUN " + json.dumps({"run_id": run.info.run_id}))
''')


def client(ml, token, script, extra_env=None):
    env = {**os.environ, "MLFLOW_TRACKING_URI": f"{ml['cairn']}/v1/ml", "MLFLOW_TRACKING_TOKEN": token,
           "MLFLOW_HTTP_REQUEST_MAX_RETRIES": "0", **(extra_env or {})}
    r = subprocess.run([PY, "-c", script], env=env, capture_output=True, text=True, timeout=300, cwd=ml["dir"])
    return r


def call(ml, token, method, path, **kw):
    return httpx.request(method, f"{ml['cairn']}/v1/ml/{path}", headers={"Authorization": f"Bearer {token}"}, timeout=30, **kw)


def test_a_model_trained_through_cairn_is_owned_governed_and_in_the_catalog(ml):
    r = client(ml, "tok-ana", TRAIN)
    assert r.returncode == 0, r.stderr[-3000:]
    run_id = json.loads(r.stdout.split("CAIRN-RUN ", 1)[1])["run_id"]
    # The run is ana's, not what the client claimed.
    run = httpx.get(f"{ml['mlflow']}/api/2.0/mlflow/runs/get", params={"run_id": run_id}).json()["run"]
    assert {t["key"]: t["value"] for t in run["data"]["tags"]}["mlflow.user"] == "ana"
    assert call(ml, "tok-ana", "POST", "api/2.0/mlflow/runs/set-tag",
                json={"run_id": run_id, "key": "mlflow.user", "value": "mallory"}).status_code == 403
    assert ml["gw"].owner("churn") == "urn:li:corpuser:ana"

    # In the catalog: the model, its group (owned by ana), and lineage from the training table.
    g = ml["graph"]
    props = g.get_aspect("urn:li:mlModel:(urn:li:dataPlatform:mlflow,churn.v1,PROD)", "mlModelProperties")
    assert props["groups"] == ["urn:li:mlModelGroup:(urn:li:dataPlatform:mlflow,churn,PROD)"]
    (job,) = props["trainingJobs"]
    assert g.get_aspect(job, "dataJobInputOutput")["inputDatasets"] == \
        ["urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.customers,PROD)"]
    owners = g.get_aspect("urn:li:mlModelGroup:(urn:li:dataPlatform:mlflow,churn,PROD)", "ownership")["owners"]
    assert [o["owner"] for o in owners] == ["urn:li:corpuser:ana"]

    # bob neither sees nor changes it; his search lists nothing; he cannot take the name.
    assert call(ml, "tok-bob", "GET", "api/2.0/mlflow/registered-models/get", params={"name": "churn"}).status_code == 403
    assert call(ml, "tok-bob", "GET", "api/2.0/mlflow/registered-models/search").json().get("registered_models", []) == []
    assert [m["name"] for m in call(ml, "tok-ana", "GET", "api/2.0/mlflow/registered-models/search").json()["registered_models"]] \
        == ["churn"]
    alias = {"name": "churn", "alias": "champion", "version": "1"}
    assert call(ml, "tok-bob", "POST", "api/2.0/mlflow/registered-models/alias", json=alias).status_code == 403
    assert call(ml, "tok-bob", "POST", "api/2.0/mlflow/registered-models/create", json={"name": "churn"}).status_code == 403
    assert call(ml, "tok-bob", "DELETE", "api/2.0/mlflow/registered-models/delete", json={"name": "churn"}).status_code == 403
    # The owner may; and with a policy, so may bob.
    assert call(ml, "tok-ana", "POST", "api/2.0/mlflow/registered-models/alias", json=alias).status_code == 200
    ml["grants"].add(("urn:li:corpuser:bob", "VIEW_ENTITY_PAGE", "urn:li:mlModelGroup:(urn:li:dataPlatform:mlflow,churn,PROD)"))
    got = call(ml, "tok-bob", "GET", "api/2.0/mlflow/registered-models/get", params={"name": "churn"})
    assert got.status_code == 200 and got.json()["registered_model"]["aliases"][0]["alias"] == "champion"
    assert call(ml, "tok-bob", "POST", "api/2.0/mlflow/registered-models/alias",
                json={**alias, "alias": "mine"}).status_code == 403          # seeing is not changing
    # Without a token: nothing.
    assert httpx.get(f"{ml['cairn']}/v1/ml/api/2.0/mlflow/experiments/search").status_code == 401
    # Audited.
    from cairn.common.audit import AuditLog
    actions = {e["action"] for e in AuditLog(ml["engine"]).entries(category="ml", limit=50)}
    assert {"ml.model_created", "ml.model_versions.create", "ml.registered_models.alias"} <= actions


@pytest.fixture(scope="module")
def served(ml):
    """The registered model, served by MLflow's own model server, reached through Cairn."""
    sp = port()
    # The model server runs uvicorn from PATH: the MLflow Python's own.
    env = {**os.environ, "MLFLOW_TRACKING_URI": ml["mlflow"], "PATH": f"{os.path.dirname(PY)}:{os.environ['PATH']}"}
    server = subprocess.Popen([f"{os.path.dirname(PY)}/mlflow", "models", "serve", "-m", "models:/churn@champion",
                               "--env-manager", "local", "--host", "127.0.0.1", "-p", str(sp)],
                              env=env, stdout=open(f"{ml['dir']}/serve.log", "w"), stderr=subprocess.STDOUT)
    try:
        wait(f"http://127.0.0.1:{sp}/ping", 120)
        ml["gw"].serving["churn"] = f"http://127.0.0.1:{sp}"
        yield
    finally:
        server.terminate()
        server.wait(15)


def test_predictions_go_through_cairn_for_those_allowed(ml, served):
    rows = {"dataframe_split": {"columns": ["tenure", "spend"], "data": [[2, 6], [35, 65]]}}
    r = call(ml, "tok-ana", "POST", "serve/churn/invocations", json=rows)
    assert r.status_code == 200, r.text
    assert r.json()["predictions"] == [1, 0]
    assert len(r.headers["x-cairn-inference-id"]) == 32                # every call is logged
    assert call(ml, "tok-bob", "POST", "serve/churn/invocations", json=rows).status_code == 403
    ml["grants"].add(("urn:li:corpuser:bob", "USE_ML_MODEL", "urn:li:mlModelGroup:(urn:li:dataPlatform:mlflow,churn,PROD)"))
    assert call(ml, "tok-bob", "POST", "serve/churn/invocations", json=rows).status_code == 200
    assert call(ml, "tok-ana", "POST", "serve/nope/invocations", json=rows).status_code == 404


def test_a_served_model_is_monitored_for_drift_and_accuracy(ml, served):
    from sqlalchemy import update

    from cairn.ml.monitoring import ml_inferences

    ana, bob = "urn:li:corpuser:ana", "urn:li:corpuser:bob"
    ml["grants"].add((bob, "USE_ML_MODEL", "urn:li:mlModelGroup:(urn:li:dataPlatform:mlflow,churn,PROD)"))
    serve = lambda tok, data: call(ml, tok, "POST", "serve/churn/invocations",  # noqa: E731
                                   json={"dataframe_split": {"columns": ["tenure", "spend"], "data": data}})
    start = int(time.time() * 1000)
    body = {"baseline": {"since_ms": start, "until_ms": start + 4000}, "min_rows": 30, "window_minutes": 60,
            "notify": [bob]}
    # Only the model's managers monitor it; Bob may only use it.
    assert call(ml, "tok-bob", "PUT", "models/churn/monitor", json=body).status_code == 403
    for bad, msg in [({"baseline": {}}, "baseline"), ({**body, "drift_threshold": 0}, "drift_threshold"),
                     ({**body, "notify": ["bob"]}, "notify")]:
        r = call(ml, "tok-ana", "PUT", "models/churn/monitor", json=bad)
        assert r.status_code == 400 and msg in r.json()["detail"], r.text
    r = call(ml, "tok-ana", "PUT", "models/churn/monitor", json=body)
    assert r.status_code == 200, r.text
    # The reference period: calls like the training data. Checking before it ends just waits.
    normal = [[1, 5], [2, 7], [3, 6], [20, 50], [30, 60], [40, 70], [4, 8], [25, 55], [35, 66], [2, 5]]
    ids = [serve("tok-ana", normal).headers["x-cairn-inference-id"] for _ in range(4)]
    with pytest.raises(Exception, match="not over yet"):
        ml["mon"].check("churn")
    assert call(ml, "tok-ana", "GET", "models/churn/monitor").json()["status"] == "waiting"
    time.sleep(max(0, (start + 4100 - int(time.time() * 1000)) / 1000))

    # Then customers far outside what it learned from.
    shifted = [[t, s] for t, s in zip(range(200, 300, 10), range(900, 1000, 10))]
    drift_ids = [serve("tok-bob", shifted).headers["x-cairn-inference-id"] for _ in range(5)]
    r = call(ml, "tok-ana", "POST", "models/churn/monitor/run")
    assert r.status_code == 200, r.text
    check = r.json()
    assert check["status"] == "drift" and set(check["metrics"]["drifted"]) >= {"tenure", "spend"}, check
    assert check["metrics"]["features"]["tenure"]["mean"] > check["metrics"]["features"]["tenure"]["baseline_mean"]
    assert check["metrics"]["calls"] >= 9 and check["metrics"]["error_rate"] == 0
    assert check["metrics"]["latency_p95_ms"] is not None
    assert [s[0] for s in ml["sent"]] == ["ml.drift_detected"] and ml["sent"][0][3] == [ana, bob]
    # Bob may see the checks, not the calls (what the model was given is for its managers).
    assert call(ml, "tok-bob", "GET", "models/churn/monitor/checks").json()[0]["status"] == "drift"
    assert call(ml, "tok-bob", "GET", "models/churn/monitor/inferences").status_code == 403
    calls = call(ml, "tok-ana", "GET", "models/churn/monitor/inferences").json()
    assert calls[0]["inputs"][0] == {"tenure": 200, "spend": 900} and calls[0]["caller"] == bob

    # The truth arrives: by the caller for their call, by a manager for any. The shifted customers churned.
    preds = next(c for c in calls if c["id"] == drift_ids[0])["predictions"]
    r = call(ml, "tok-bob", "POST", "models/churn/feedback", json={"inference_id": drift_ids[0], "labels": [1] * len(preds)})
    assert r.status_code == 200, r.text
    assert call(ml, "tok-bob", "POST", "models/churn/feedback",
                json={"inference_id": ids[0], "labels": [1] * 10}).status_code == 404      # Ana's call
    assert call(ml, "tok-ana", "POST", "models/churn/feedback",
                json={"inference_id": ids[0], "labels": [1, 1, 1, 0, 0, 0, 1, 0, 0, 1]}).status_code == 200
    assert call(ml, "tok-ana", "POST", "models/churn/feedback",
                json={"inference_id": ids[1], "labels": [1]}).json()["detail"].startswith("that call had 10 rows")
    m = call(ml, "tok-ana", "POST", "models/churn/monitor/run").json()["metrics"]
    expected = (sum(p == 1 for p in preds) + 10) / (len(preds) + 10)
    assert m["labelled_rows"] == 20 and m["accuracy"] == round(expected, 4) and m["accuracy"] < 1
    assert len(ml["sent"]) == 1                                  # still drifting: no second alert

    # The drifting calls age out of the window; normal ones return: resolved, and said so.
    with ml["engine"].begin() as conn:
        conn.execute(update(ml_inferences).where(ml_inferences.c.id.in_(drift_ids)).values(ts_ms=start - 7_200_000))
    for _ in range(3):
        serve("tok-ana", normal)
    assert call(ml, "tok-ana", "POST", "models/churn/monitor/run").json()["status"] == "ok"
    assert [s[0] for s in ml["sent"]] == ["ml.drift_detected", "ml.drift_resolved"]
    # Turned off: the payloads go, the call log and checks stay.
    assert call(ml, "tok-ana", "DELETE", "models/churn/monitor").status_code == 204
    with ml["engine"].connect() as conn:
        from sqlalchemy import func, select
        assert conn.execute(select(func.count()).select_from(ml_inferences).where(
            ml_inferences.c.inputs.is_not(None))).scalar() == 0
    assert call(ml, "tok-ana", "GET", "models/churn/monitor").status_code == 404


@pytest.fixture(scope="module")
def serving(ml):
    """Managed serving (L63) on the same MLflow: deployments of registered versions, MLflow's model server as local
    processes, scaled by Cairn."""
    import uvicorn
    from fastapi import FastAPI, HTTPException
    from starlette.requests import Request

    from cairn.common.audit import AuditLog
    from cairn.common.security import Principal
    from cairn.ml.serving import ModelServing, ProcessRuntime, build_serving_router

    os.environ.setdefault("CAIRN_ML_SERVING_LOG_DIR", ml["dir"])
    ms = ModelServing(ml["engine"], ProcessRuntime(f"{os.path.dirname(PY)}/mlflow", ml["mlflow"]), ml["gw"],
                      AuditLog(ml["engine"]))
    ms.tick_s, ms.cold_s = 1.0, 150.0

    def who(request: Request) -> Principal:
        urn = TOKENS.get(request.headers.get("authorization", "").removeprefix("Bearer "))
        if urn is None:
            raise HTTPException(401, "sign in")
        return Principal(urn, "token")
    app = FastAPI()
    app.include_router(build_serving_router(ms, who))
    cp = port()
    api = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cp, timeout_keep_alive=75, log_level="warning"))
    threading.Thread(target=api.run, daemon=True).start()
    wait(f"http://127.0.0.1:{cp}/docs")
    yield {"url": f"http://127.0.0.1:{cp}/v1/ml/deployments", "ms": ms}
    for d in list(ms.runtime.procs):
        ms.runtime.scale({"id": d}, 0)
    api.should_exit = True


def dep(serving, token, method, path="", **kw):
    return httpx.request(method, serving["url"] + path, headers={"Authorization": f"Bearer {token}"}, timeout=200, **kw)


def test_a_registered_model_is_deployed_scales_with_load_and_is_invoked_as_the_caller(ml, serving):
    from concurrent.futures import ThreadPoolExecutor

    ms = serving["ms"]
    rows = {"dataframe_split": {"columns": ["tenure", "spend"], "data": [[2, 6], [35, 65]]}}
    body = {"name": "churn-prod", "model": "churn", "version": "1", "min_replicas": 0, "max_replicas": 2,
            "target_concurrency": 1, "idle_minutes": 1}
    # Deploying needs MANAGE_ML_MODEL (ana owns churn; carol has no grant); an unknown version is refused.
    assert dep(serving, "tok-carol", "POST", json=body).status_code == 403
    assert dep(serving, "tok-ana", "POST", json={**body, "version": "99"}).status_code == 404
    r = dep(serving, "tok-ana", "POST", json=body)
    assert r.status_code == 201, r.text
    assert (r.json()["replicas"], r.json()["running"]) == (0, 0)              # scaled to zero until used
    # The first call starts a replica and waits for it; predictions are the model's.
    r = dep(serving, "tok-ana", "POST", "/churn-prod/invocations", json=rows)
    assert r.status_code == 200, r.text
    assert r.json()["predictions"] == [1, 0] and len(r.headers["x-cairn-inference-id"]) == 32
    assert dep(serving, "tok-ana", "GET", "/churn-prod").json()["running"] == 1
    # Invoked as the caller: carol has no USE_ML_MODEL and neither sees nor calls it.
    assert dep(serving, "tok-carol", "POST", "/churn-prod/invocations", json=rows).status_code == 403
    assert dep(serving, "tok-carol", "GET", "/churn-prod").status_code == 404
    # Sustained concurrent load: the deployment scales to its maximum.
    ms.sample()
    stop = time.time() + 4

    def hammer():
        n = 0
        while time.time() < stop:
            assert dep(serving, "tok-ana", "POST", "/churn-prod/invocations", json=rows).status_code == 200
            n += 1
        return n
    with ThreadPoolExecutor(8) as pool:
        futs = [pool.submit(hammer) for _ in range(8)]
        time.sleep(3)
        ms.sample()                                    # concurrency measured while the load runs
        assert sum(f.result() for f in futs) > 8
    assert ms.autoscale()["churn-prod"] == 2
    end = time.time() + 120
    while time.time() < end and len(ms.runtime.endpoints(ms._get("churn-prod"))) < 2:
        time.sleep(1)
    assert len(ms.runtime.endpoints(ms._get("churn-prod"))) == 2
    assert dep(serving, "tok-ana", "POST", "/churn-prod/invocations", json=rows).status_code == 200
    # Idle past idle_minutes: one replica at a time back to zero.
    from sqlalchemy import update

    from cairn.ml.serving import model_deployments
    ms.last_request.clear()
    with ml["engine"].begin() as conn:
        conn.execute(update(model_deployments).where(model_deployments.c.name == "churn-prod")
                     .values(last_request_ms=int(time.time() * 1000) - 120_000))
    ms.sample()
    assert ms.autoscale()["churn-prod"] == 0 and ms.runtime.running(ms._get("churn-prod")) == 0
    # Only those who may manage the model delete the deployment.
    assert dep(serving, "tok-carol", "DELETE", "/churn-prod").status_code in (403, 404)
    assert dep(serving, "tok-ana", "DELETE", "/churn-prod").status_code == 204
    from cairn.common.audit import AuditLog

    actions = {e["action"] for e in AuditLog(ml["engine"]).entries(category="ml", limit=500)}
    assert {"ml.model_deployed", "ml.model_scaled", "ml.model_undeployed", "ml.model_invoked"} <= actions


def test_the_scaling_rule():
    from cairn.ml.serving import desired_replicas as want

    assert want(0, 2, 0, 0, 4, idle=True) == 0                    # idle: the minimum
    assert want(0, 2, 3, 1, 4, idle=True) == 1
    assert want(5, 2, 1, 0, 4, idle=False) == 3                   # up at once: ceil(5 / 2)
    assert want(50, 2, 1, 0, 4, idle=False) == 4                  # never past the maximum
    assert want(0.5, 2, 4, 0, 4, idle=False) == 3                 # down one at a time
    assert want(0, 2, 1, 0, 4, idle=False) == 1                   # in use lately: not below one
    assert want(0.1, 2, 0, 0, 4, idle=False) == 1                 # traffic from zero: one


SPARK_TRAINING = textwrap.dedent('''
    import json, pandas as pd
    from sklearn.linear_model import LinearRegression
    from cairn_spark.mlflow import use_mlflow, log_table_input
    mlflow = use_mlflow(experiment="spend")              # the job's token from CAIRN_TOKEN_FILE, as a driver has it
    df = pd.DataFrame({"tenure": [1, 2, 3, 4], "spend": [10, 20, 30, 40]})
    with mlflow.start_run() as run:
        log_table_input("sales.customers")
        model = LinearRegression().fit(df[["tenure"]], df["spend"])
        mlflow.sklearn.log_model(model, name="model", registered_model_name="spend")
    print("CAIRN-RUN " + json.dumps({"run_id": run.info.run_id, "uri": mlflow.get_tracking_uri()}))
''')


def test_a_spark_jobs_training_is_tracked_through_cairn_as_the_job(ml, tmp_path):
    """Queue row 20: cairn_spark.mlflow points MLflow at Cairn with the job's own token (from its token file, as a
    Spark driver gets it); the run, the registered model and its lineage are the job's user's."""
    spark_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "spark", "src")
    token = tmp_path / "token"
    token.write_text("tok-carol")                         # carol's job: no MLflow settings of its own
    env = {k: v for k, v in os.environ.items() if not k.startswith("MLFLOW_")}
    env.update({"CAIRN_URL": ml["cairn"], "CAIRN_TOKEN_FILE": str(token), "PYTHONPATH": spark_src,
                "MLFLOW_HTTP_REQUEST_MAX_RETRIES": "0"})
    r = subprocess.run([PY, "-c", SPARK_TRAINING], env=env, capture_output=True, text=True, timeout=300, cwd=ml["dir"])
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout.split("CAIRN-RUN ", 1)[1])
    assert out["uri"] == f"{ml['cairn']}/v1/ml"
    run = httpx.get(f"{ml['mlflow']}/api/2.0/mlflow/runs/get", params={"run_id": out["run_id"]}).json()["run"]
    assert {t["key"]: t["value"] for t in run["data"]["tags"]}["mlflow.user"] == "carol"
    assert ml["gw"].owner("spend") == "urn:li:corpuser:carol"
    props = ml["graph"].get_aspect("urn:li:mlModel:(urn:li:dataPlatform:mlflow,spend.v1,PROD)", "mlModelProperties")
    (job,) = props["trainingJobs"]
    assert ml["graph"].get_aspect(job, "dataJobInputOutput")["inputDatasets"] == \
        ["urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.customers,PROD)"]
    # Without a token the helper refuses before anything reaches MLflow.
    env.pop("CAIRN_TOKEN_FILE")
    r = subprocess.run([PY, "-c", SPARK_TRAINING], env=env, capture_output=True, text=True, timeout=120, cwd=ml["dir"])
    assert r.returncode != 0 and "CAIRN_TOKEN" in r.stderr
