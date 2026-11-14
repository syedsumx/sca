"""Agent apps tracked in MLflow: deploying makes an experiment ``agents/<name>``; each invocation is a run there, its
``mlflow.user`` the person who asked, with the app version and session as tags and latency and sizes as metrics,
never the conversation itself. Against a real MLflow server. Needs CAIRN_MLFLOW_PYTHON (a Python with mlflow 3.x)."""

import json
import os
import subprocess
import tempfile

import httpx
import pytest

from test_agent_apps import ANA, BOB, stack  # noqa: F401 - the fixture
from test_ml_e2e import wait as wait_url
from test_trino_e2e import port

PY = os.environ.get("CAIRN_MLFLOW_PYTHON", "")
pytestmark = pytest.mark.skipif(not (PY and os.path.exists(PY)), reason="needs CAIRN_MLFLOW_PYTHON (mlflow)")


@pytest.fixture
def mlflow_url(monkeypatch):
    d = tempfile.mkdtemp(prefix="agent-ml-")
    mp = port()
    server = subprocess.Popen([f"{os.path.dirname(PY)}/mlflow", "server", "--backend-store-uri",
                               f"sqlite:///{d}/mlflow.db", "--artifacts-destination", f"{d}/artifacts",
                               "--host", "127.0.0.1", "--port", str(mp), "--workers", "1"],
                              stdout=open(f"{d}/mlflow.log", "w"), stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{mp}"
    wait_url(f"{url}/health")
    monkeypatch.setenv("CAIRN_MLFLOW_URL", url)
    yield url
    server.terminate()
    server.wait(20)


@pytest.fixture
def tracked(mlflow_url, request):
    return request.getfixturevalue("stack"), mlflow_url


def test_invocations_are_mlflow_runs(tracked):
    from cairn.agentkit.project import deploy, init

    (s, url, tmp), ml = tracked
    assert s.agent_apps.mlflow is not None
    proj = init(str(tmp / "triage"), template="basic")
    app = deploy(str(proj), url, {"X-Cairn-Actor": ANA})
    exp = httpx.get(f"{ml}/api/2.0/mlflow/experiments/get-by-name", params={"experiment_name": "agents/triage"})
    assert exp.status_code == 200 and app["mlflow_experiment"] == exp.json()["experiment"]["experiment_id"]

    r = httpx.post(f"{url}/v1/agent-apps/triage/invocations", json={"input": "Secret question?", "session_id": "s9"},
                   headers={"X-Cairn-Actor": BOB}, timeout=120)
    assert r.status_code == 200, r.text
    runs = httpx.post(f"{ml}/api/2.0/mlflow/runs/search",
                      json={"experiment_ids": [app["mlflow_experiment"]]}).json()["runs"]
    assert len(runs) == 1
    run = runs[0]
    tags = {t["key"]: t["value"] for t in run["data"]["tags"]}
    assert run["info"]["status"] == "FINISHED"
    assert tags["mlflow.user"] == BOB and tags["cairn.app_version"] == "1" and tags["cairn.session"] == "s9"
    metrics = {m["key"]: m["value"] for m in run["data"]["metrics"]}
    assert {"latency_ms", "input_chars", "output_chars", "turns"} <= set(metrics) and metrics["turns"] == 1
    assert "Secret question?" not in json.dumps(run)                 # sizes, not the conversation
