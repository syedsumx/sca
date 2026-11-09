"""The ``bigquery`` flow step (L138) against the real BigQuery emulator (``ghcr.io/goccy/bigquery-emulator``, pinned in
CAIRN_BIGQUERY_EMULATOR_IMAGE): a query job inserted with Cairn's own job id (jobs.insert), followed with jobs.get to
DONE, its rows read with getQueryResults and kept as the step's output for the next step; a query on a table that is not
there fails the step with BigQuery's message. The service account's key is read from a secret scope as the owner and
exchanged for a token at its ``token_uri`` (the emulator does not check tokens, so the token endpoint is the stand-in of
tests/platform_standins.py, which checks the signed assertion).

Needs docker and the emulator image (the pipelines CI job pulls it)."""

from __future__ import annotations

import base64
import os
import shutil
import socket
import subprocess
import threading
import time

import httpx
import pytest

from cairn.common.db import get_engine, init_db
from cairn.flows.engine import Flows
from cairn.flows.steps import Backends
from platform_standins import Platforms

IMAGE = os.environ.get("CAIRN_BIGQUERY_EMULATOR_IMAGE", "ghcr.io/goccy/bigquery-emulator:0.6.6")
ANA = "urn:li:corpuser:ana"


def _ready() -> bool:
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode:
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=30).returncode == 0


pytestmark = pytest.mark.skipif(not _ready(), reason=f"needs docker and the image {IMAGE}")

DATA = """projects:
  - id: proj
    datasets:
      - id: ds
        tables:
          - id: orders
            columns:
              - {name: region, type: STRING}
              - {name: amount, type: INTEGER}
            data:
              - {region: EU, amount: 3}
              - {region: EU, amount: 4}
              - {region: US, amount: 1}
"""


@pytest.fixture(scope="module")
def emulator(tmp_path_factory):
    d = tmp_path_factory.mktemp("bq")
    (d / "data.yaml").write_text(DATA)
    os.chmod(d, 0o755)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    name = f"cairn-flows-bq-{os.getpid()}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "run", "-d", "--name", name, "--network", "host", "-v", f"{d}:/data:ro", IMAGE,
                    "--project=proj", f"--port={port}", f"--grpc-port={port + 1 if port < 65000 else port - 1}",
                    "--data-from-yaml=/data/data.yaml"], check=True, capture_output=True, timeout=120)
    url = f"http://127.0.0.1:{port}"
    end = time.time() + 120
    while True:
        try:
            if httpx.get(f"{url}/bigquery/v2/projects/proj/datasets", timeout=3, trust_env=False).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.time() > end:
            logs = subprocess.run(["docker", "logs", "--tail", "50", name], capture_output=True, text=True)
            raise AssertionError(logs.stdout[-3000:] + logs.stderr[-3000:])
        time.sleep(1)
    yield url
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


@pytest.fixture
def env(tmp_path, monkeypatch, emulator):
    from cairn.identity.secret_scopes import SecretScopes

    tokens = Platforms(str(tmp_path))                    # Google's token endpoint (the emulator checks no token)
    monkeypatch.setenv("CAIRN_SECRETS_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("SSL_CERT_FILE", tokens.ca)
    monkeypatch.setenv("CAIRN_FLOWS_ALLOW_PRIVATE_NETWORKS", "true")   # the emulator listens on 127.0.0.1
    meta = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(meta)
    ss = SecretScopes(meta, lambda a: [], lambda a: False)
    ss.create(ANA, {"name": "gcp"})
    ss.put(ANA, "gcp", "service_account_json", tokens.sa_json())
    f = Flows(meta, Backends(secrets=ss, notify=lambda *a: None), None, lambda *a: None, lambda a: False, lambda a: [],
              None, None, None, None, "one")
    stop = threading.Event()

    def tick():
        while not stop.wait(0.3):
            try:
                f.run_due()
            except Exception:  # noqa: BLE001 - shown by the assertions
                pass
    threading.Thread(target=tick, daemon=True).start()
    f.tokens = tokens
    yield f
    stop.set()
    tokens.close()


def steps_of(r):
    return {s["key"]: s for s in r["steps"]}


def test_a_query_job_on_the_emulator(env, emulator):
    bq = {"type": "bigquery", "project": "proj", "secret_scope": "gcp", "api_url": emulator, "poll_s": 1}
    env.create(ANA, {"name": "bq", "steps": [
        {"name": "by_region", **bq, "query": "SELECT region, SUM(amount) AS total FROM `proj.ds.orders` "
                                             "GROUP BY region ORDER BY region"},
        {"name": "check", "type": "branch", "after": ["by_region"], "condition": "steps.by_region.rows == 2",
         "then": ["done"]},
        {"name": "done", "type": "wait", "after": ["check"], "seconds": 1}]}, publish=True)
    done = env.wait_for(env.run(ANA, "bq")["id"], 120)
    assert done["status"] == "succeeded", [(s["key"], s["error"]) for s in done["steps"]]
    out = steps_of(done)["by_region"]["output"]
    assert out["columns"] == ["region", "total"] and out["preview"] == [["EU", "7"], ["US", "1"]], out
    assert out["job_id"].startswith("cairn_bq_by_region_")
    job = httpx.get(f"{emulator}/bigquery/v2/projects/proj/jobs/{out['job_id']}", trust_env=False, timeout=10).json()
    assert job["status"]["state"] == "DONE" and job["configuration"]["query"]["query"].startswith("SELECT region")
    assert any(r["path"] == "/token" for r in env.tokens.requests)          # the key's token, from its token_uri
    # A query BigQuery refuses fails the step with BigQuery's reason (the emulator answers a missing table with no
    # rows, so the refusal here is an unknown function, which its SQL analyser rejects).
    env.create(ANA, {"name": "bq-bad", "steps": [{"name": "q", **bq, "query": "SELECT no_such_function(1) AS x"}]},
               publish=True)
    done = env.wait_for(env.run(ANA, "bq-bad")["id"], 120)
    err = steps_of(done)["q"]["error"] or ""
    assert done["status"] == "failed" and "no_such_function" in err.lower(), (done["status"], err)
