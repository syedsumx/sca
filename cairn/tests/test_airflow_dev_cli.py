"""``cairn airflow dev`` (local development, like ``astro dev``) with a real Airflow image and Cairn's real API:

* ``init`` makes a project (dags/, requirements.txt, a Dockerfile FROM the Airflow image, an integrity test);
* ``parse`` imports every DAG in the project's image and fails on an import error, naming the file;
* ``pytest`` runs the project's tests in its image (the integrity test fails on a broken DAG);
* ``start`` runs a local Airflow whose ``cairn`` connection is the developer (their Cairn token, from a file only they
  can read, never a command line): a task calls Cairn as them; ``stop`` removes it and the file.

Needs docker and an Airflow image with cairn-airflow-provider (CAIRN_AIRFLOW_IMAGE)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time

import pytest

from test_trino_e2e import cairn, port  # noqa: F401  (fixtures)

IMAGE = os.environ.get("CAIRN_AIRFLOW_IMAGE", "")
pytestmark = pytest.mark.skipif(not IMAGE or not shutil.which("docker"), reason="needs docker and CAIRN_AIRFLOW_IMAGE")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PAT = "pat-dev-raj-" + "d" * 24
RAJ = "urn:li:corpuser:raj"
WHOAMI = '''
from datetime import datetime

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG

from cairn_airflow.hooks import CairnHook


def whoami():
    print("CAIRN_DEV_IDENTITY=" + CairnHook().api("GET", "/me")["urn"])


with DAG("whoami", start_date=datetime(2026, 1, 1), schedule=None, is_paused_upon_creation=False):
    PythonOperator(task_id="whoami", python_callable=whoami)
'''


def cli(*args, cwd, env=None, timeout=1800):
    return subprocess.run([sys.executable, "-m", "cairn.cli", "airflow", *args], cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, env={**os.environ, "PYTHONPATH": os.path.join(ROOT, "src"), **(env or {})})


def test_local_development(cairn, tmp_path):  # noqa: F811
    url, s = cairn
    project = tmp_path / "finance-dags"
    r = cli("dev", "init", str(project), "--image", IMAGE, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    for f in ("dags/example_lakehouse.py", "requirements.txt", "Dockerfile", "tests/test_dag_integrity.py"):
        assert (project / f).exists(), f
    assert f"FROM {IMAGE}" in (project / "Dockerfile").read_text()
    tag = "cairn-airflow-dev-finance-dags:dev"
    try:
        r = cli("dev", "parse", "--dir", str(project), cwd=tmp_path)
        assert r.returncode == 0 and "example_lakehouse" in r.stdout and "0 import error" in r.stdout, r.stdout + r.stderr
        assert cli("dev", "pytest", "--dir", str(project), cwd=tmp_path).returncode == 0
        (project / "dags" / "broken.py").write_text("from airflow.sdk import DAG\n\nwith DAG('broken':\n")
        r = cli("dev", "parse", "--dir", str(project), cwd=tmp_path)
        assert r.returncode == 1 and "broken.py" in r.stdout, r.stdout + r.stderr
        assert cli("dev", "pytest", "--dir", str(project), cwd=tmp_path).returncode != 0
        (project / "dags" / "broken.py").unlink()

        # ---- a local Airflow, its tasks signed in to Cairn as the developer ----------------------------------
        real = s.identity._me_lookup
        s.identity._me_lookup = lambda u, h, c: RAJ if h.get("Authorization") == f"Bearer {PAT}" else real(u, h, c)
        (project / "dags" / "whoami.py").write_text(WHOAMI)
        token = tmp_path / "token"
        token.write_text(PAT)
        p = port()
        env = {"CAIRN_URL": url, "CAIRN_TOKEN_FILE": str(token), "CAIRN_TOKEN": ""}
        r = cli("dev", "start", "--dir", str(project), "--port", str(p), cwd=tmp_path, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        envfile = project / ".cairn" / "dev.env"
        assert stat.S_IMODE(envfile.stat().st_mode) == 0o600
        name = "cairn-airflow-dev-finance-dags"
        inspect = subprocess.run(["docker", "inspect", name], capture_output=True, text=True).stdout
        assert PAT not in subprocess.run(["docker", "inspect", "-f", "{{.Args}} {{.Path}}", name],
                                         capture_output=True, text=True).stdout
        del inspect
        end = time.time() + 420
        out = ""
        while time.time() < end:
            q = subprocess.run(["docker", "exec", name, "bash", "-c",
                                "airflow dags list-runs whoami -o plain 2>/dev/null | grep -E 'success|failed' | head -1 || "
                                "true; airflow dags trigger whoami >/dev/null 2>&1 || true"],
                               capture_output=True, text=True, timeout=120)
            out = q.stdout
            if "success" in out or "failed" in out:
                break
            time.sleep(10)
        assert "success" in out, subprocess.run(["docker", "logs", "--tail", "80", name], capture_output=True,
                                                text=True).stdout[-4000:]
        logs = subprocess.run(["docker", "exec", name, "bash", "-c", "grep -rh CAIRN_DEV_IDENTITY /opt/airflow/logs | head -3"],
                              capture_output=True, text=True, timeout=60).stdout
        assert f"CAIRN_DEV_IDENTITY={RAJ}" in logs, logs
        s.identity._me_lookup = real
        r = cli("dev", "stop", "--dir", str(project), cwd=tmp_path)
        assert r.returncode == 0 and not envfile.exists()
        assert subprocess.run(["docker", "inspect", name], capture_output=True).returncode != 0
    finally:
        subprocess.run(["docker", "rm", "-f", "cairn-airflow-dev-finance-dags"], capture_output=True)
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
