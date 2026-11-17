"""cairn_spark.mlflow: a job signed in with its workload's token keeps a valid Cairn token for MLflow for as long as it
runs (the exchanged token lasts an hour by default; training often takes longer)."""

import os
import sys
import time
import types

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "spark", "src"))

from cairn_spark import mlflow as cmlflow  # noqa: E402


class _Resp:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def test_a_workload_jobs_mlflow_token_is_renewed_before_it_expires(tmp_path, monkeypatch):
    """The token MLflow sent was exchanged once, at use_mlflow(): after its lifetime every tracking call (the model
    logged at the end of a long training run, above all) was refused with 401. It is now exchanged again, with the
    workload's current token, before it expires."""
    workload = tmp_path / "token"
    workload.write_text("platform-jwt-1")
    seen, issued = [], iter(range(1, 1000))

    def post(url, data=None, **kw):
        assert url == "https://cairn.test/v1/auth/workload-token"
        seen.append(data["subject_token"])
        return _Resp({"access_token": f"cairn-{next(issued)}", "expires_in": 2, "token_type": "Bearer"})

    fake = types.ModuleType("mlflow")
    fake.set_tracking_uri = fake.set_registry_uri = fake.set_experiment = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "mlflow", fake)
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setenv("CAIRN_WORKLOAD_TOKEN_FILE", str(workload))
    monkeypatch.delenv("CAIRN_TOKEN", raising=False)
    monkeypatch.delenv("CAIRN_TOKEN_FILE", raising=False)
    monkeypatch.setenv("MLFLOW_TRACKING_TOKEN", "")
    try:
        assert cmlflow.use_mlflow(cairn_url="https://cairn.test") is fake
        assert os.environ["MLFLOW_TRACKING_TOKEN"] == "cairn-1"
        workload.write_text("platform-jwt-2")                    # the platform rotated its token
        deadline = time.time() + 10
        while os.environ["MLFLOW_TRACKING_TOKEN"] == "cairn-1" and time.time() < deadline:
            time.sleep(0.1)
        assert os.environ["MLFLOW_TRACKING_TOKEN"] != "cairn-1"
        assert seen[0] == "platform-jwt-1" and seen[-1] == "platform-jwt-2"
    finally:
        if hasattr(cmlflow, "_renew"):
            cmlflow._renew.set()                                    # stop renewing
