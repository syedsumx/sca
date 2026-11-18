"""The MLflow gateway's registry checks see the path MLflow will route: an encoded dot segment, an empty segment or an
encoded ``?`` cannot carry a registry call past them."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from cairn.common.db import init_db
from cairn.ml.mlflow_gateway import MlflowGateway, build_ml_router, ml_models


@pytest.fixture
def gateway(tmp_path):
    seen = []

    async def mlflow(req):
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={})

    engine = create_engine(f"sqlite:///{tmp_path}/c.db")
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(ml_models.insert().values(name="churn", owner="urn:li:corpuser:bob", created_s=1))
        conn.execute(ml_models.insert().values(name="mine", owner="urn:li:corpuser:ana", created_s=1))
    gw = MlflowGateway("http://mlflow", engine, http=httpx.AsyncClient(transport=httpx.MockTransport(mlflow)))
    app = FastAPI()
    ana = SimpleNamespace(urn="urn:li:corpuser:ana", is_service=False, via="")
    app.include_router(build_ml_router(gw, lambda: ana))
    return TestClient(app), seen


@pytest.mark.parametrize("path", ["api/2.0/mlflow/registered-models/delete",
                                  "api/2.0/mlflow/x/%2e%2e/registered-models/delete",
                                  "api/2.0/mlflow/registered-models/delete%3Fx=1",
                                  "api/2.0/mlflow//registered-models/delete",
                                  "api/2.0/mlflow/registered-models/%2e/delete"])
def test_another_persons_model_is_not_reached_by_any_spelling(gateway, path):
    c, seen = gateway
    r = c.request("DELETE", f"/v1/ml/{path}", json={"name": "churn"})
    assert r.status_code in (403, 404) and seen == []


def test_ordinary_calls_pass(gateway):
    c, seen = gateway
    assert c.get("/v1/ml/api/2.0/mlflow/experiments/search").status_code == 200
    assert seen == [("GET", "/api/2.0/mlflow/experiments/search")]


@pytest.mark.parametrize("ctype, body", [("text/plain", b'{"name": "churn"}'), ("", b'{"name": "churn"}'),
                                         ("application/json", b'"{\\"name\\": \\"churn\\"}"')])
def test_the_body_mlflow_reads_is_the_one_checked(gateway, ctype, body):
    """MLflow reads a DELETE's JSON body whatever its content type (and decodes double-encoded JSON): naming one's own
    model in the query must not let a body naming another's through."""
    c, seen = gateway
    r = c.request("DELETE", "/v1/ml/api/2.0/mlflow/registered-models/delete?name=mine", content=body,
                  headers={"content-type": ctype} if ctype else {})
    assert r.status_code in (400, 403) and seen == []


def test_artifact_bytes_still_pass(gateway):
    c, seen = gateway
    r = c.put("/v1/ml/api/2.0/mlflow-artifacts/artifacts/0/abc/artifacts/model.pkl", content=b"\x80\x04binary",
              headers={"content-type": "application/octet-stream"})
    assert r.status_code == 200 and seen == [("PUT", "/api/2.0/mlflow-artifacts/artifacts/0/abc/artifacts/model.pkl")]


def test_process_serving_logs_neither_follow_links_nor_leak_descriptors(tmp_path, monkeypatch):
    """A model server's log is not written through a link left at its name, and starting and stopping replicas
    leaves no descriptor open in Cairn."""
    import os
    import stat
    import sys

    from cairn.ml import serving
    from cairn.ml.serving import ProcessRuntime

    fake = tmp_path / "mlflow"
    fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    fake.chmod(0o755)
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("CAIRN_ML_SERVING_LOG_DIR", str(logs))

    class FixedPort:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def bind(self, addr):
            pass

        def getsockname(self):
            return ("127.0.0.1", 45678)
    monkeypatch.setattr(serving.socket, "socket", FixedPort)
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (logs / "serve-churn-45678.log").symlink_to(victim)
    rt, d = ProcessRuntime(str(fake), "http://mlflow"), {"id": "d1", "name": "churn", "model": "churn", "version": "1"}
    with pytest.raises(OSError):
        rt.scale(d, 1)
    assert victim.read_text() == "keep"
    (logs / "serve-churn-45678.log").unlink()
    before = len(os.listdir("/proc/self/fd"))
    rt.scale(d, 1)
    rt.scale(d, 0)
    for p, _ in rt.procs.get("d1", []):
        p.wait()
    assert len(os.listdir("/proc/self/fd")) == before
    assert stat.S_IMODE((logs / "serve-churn-45678.log").stat().st_mode) == 0o600


def test_process_model_servers_get_none_of_cairns_secrets(tmp_path, monkeypatch):
    """A model's code is its owner's: the local model server starts with an allowlisted environment (the basics,
    MLflow's settings, what the operator names in CAIRN_ML_SERVING_ENV), never Cairn's secrets."""
    import json
    import sys
    import time

    from cairn.ml.serving import ProcessRuntime

    seen = tmp_path / "env.json"
    fake = tmp_path / "mlflow"
    fake.write_text(f"#!{sys.executable}\nimport json, os, time\n"
                    f"open({str(seen) + '.tmp'!r}, 'w').write(json.dumps(dict(os.environ)))\n"
                    f"os.rename({str(seen) + '.tmp'!r}, {str(seen)!r})\ntime.sleep(30)\n")
    fake.chmod(0o755)
    monkeypatch.setenv("CAIRN_ML_SERVING_LOG_DIR", str(tmp_path))
    for k, v in {"CAIRN_SERVICE_TOKEN": "svc-token-must-not-leak", "CAIRN_APPS_SECRET": "apps-secret-must-not-leak",
                 "CAIRN_DATABASE_URL": "postgresql://cairn:pw@db/cairn", "AWS_SECRET_ACCESS_KEY": "aws-must-not-leak",
                 "ANTHROPIC_API_KEY": "sk-must-not-leak", "MLFLOW_S3_ENDPOINT_URL": "http://store:9000",
                 "AWS_REGION": "eu-central-1", "CAIRN_ML_SERVING_ENV": "AWS_REGION,CAIRN_SERVICE_TOKEN"}.items():
        monkeypatch.setenv(k, v)
    rt, d = ProcessRuntime(str(fake), "http://mlflow"), {"id": "d2", "name": "churn", "model": "churn", "version": "1"}
    rt.scale(d, 1)
    try:
        end = time.time() + 20
        while not seen.exists():
            assert time.time() < end, "the stand-in model server did not start"
            time.sleep(0.1)
        env = json.loads(seen.read_text())
    finally:
        rt.scale(d, 0)
    assert not [k for k in env if k.startswith("CAIRN_")]
    assert "must-not-leak" not in json.dumps(env) and "pw@db" not in json.dumps(env)
    assert env["MLFLOW_TRACKING_URI"] == "http://mlflow" and env["MLFLOW_S3_ENDPOINT_URL"] == "http://store:9000"
    assert env["AWS_REGION"] == "eu-central-1" and env["PATH"].startswith(str(tmp_path))
