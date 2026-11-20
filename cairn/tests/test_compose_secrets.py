"""The Compose stack ships no working secrets: each one is required from .env (where .env.example has CHANGE_ME), so
a deployment never runs with a value published in this repository."""

import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
COMPOSE = os.path.join(ROOT, "docker-compose.cairn.yml")
SECRETS = ["CAIRN_TRINO_ACCESS_KEY", "CAIRN_STORE_STS_SECRET", "CAIRN_STORE_SECRET_KEY", "CAIRN_CATALOG_TOKEN",
           "CAIRN_TRINO_SHARED_SECRET", "CAIRN_TRINO_KEYSTORE_PASSWORD","CAIRN_TRINO_OAUTH_SECRET", "CAIRN_AIRFLOW_OAUTH_SECRET",
           "CAIRN_AIRFLOW_FERNET_KEY", "CAIRN_AIRFLOW_JWT_SECRET", "CAIRN_AIRFLOW_SECRET_KEY", "CAIRN_ONTOLOGY_TOKEN"]


def test_every_secret_is_required_and_listed_for_the_operator():
    compose = open(COMPOSE).read()
    example = open(os.path.join(ROOT, ".env.example")).read()
    for name in SECRETS:
        uses = re.findall(r"\$\{" + name + r"(:[-?][^}]*)?\}", compose)
        assert uses, name
        assert all(u.startswith(":?") for u in uses), f"{name} has a default in docker-compose.cairn.yml: {uses}"
        assert re.search(rf"^{name}=CHANGE_ME$", example, re.M), f"{name}=CHANGE_ME missing from .env.example"


@pytest.mark.skipif(not shutil.which("docker"), reason="needs docker compose")
def test_compose_refuses_to_start_without_them(tmp_path):
    """With every required value but one, docker compose names the missing one and starts nothing."""
    required = set(re.findall(r"\$\{([A-Z0-9_]+):\?", open(COMPOSE).read()))
    assert set(SECRETS) <= required
    for missing in ("CAIRN_TRINO_ACCESS_KEY", "CAIRN_STORE_STS_SECRET"):
        env = tmp_path / f"{missing}.env"
        env.write_text("".join(f"{k}=x{i}\n" for i, k in enumerate(sorted(required - {missing}))))
        clean = {k: v for k, v in os.environ.items() if k not in required}
        r = subprocess.run(["docker", "compose", "--env-file", str(env), "-f", COMPOSE, "config", "-q"],
                           capture_output=True, text=True, env=clean, cwd=ROOT)
        assert r.returncode != 0 and missing in r.stderr, (missing, r.stderr)
    env = tmp_path / "all.env"
    env.write_text("".join(f"{k}=x{i}\n" for i, k in enumerate(sorted(required))))
    r = subprocess.run(["docker", "compose", "--env-file", str(env), "-f", COMPOSE, "config", "-q"],
                       capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k not in required},
                       cwd=ROOT)
    assert r.returncode == 0, r.stderr
