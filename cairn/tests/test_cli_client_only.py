"""The light distribution (cli/: `cairn-cli`) installs and works with only the client's dependencies: its wheel is
built from this checkout, installed into a new virtual environment, and run. None of Cairn's server dependencies
(FastAPI, SQLAlchemy, Alembic, sqlglot, uvicorn) is installed there or imported by any command.

Needs pip access to the package index (httpx, PyYAML, keyring, pydantic, croniter, hatchling)."""

import os
import subprocess
import sys
import venv

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = ("fastapi", "starlette", "sqlalchemy", "alembic", "sqlglot", "uvicorn", "trino", "anthropic")


def run(*cmd, **kw):
    out = subprocess.run(list(cmd), capture_output=True, text=True, timeout=900, **kw)
    assert out.returncode == 0, f"{cmd}: {out.stdout[-2000:]}\n{out.stderr[-2000:]}"
    return out.stdout


@pytest.fixture(scope="module")
def client_venv(tmp_path_factory):
    d = tmp_path_factory.mktemp("cli-only")
    run(sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(d / "wheel"), os.path.join(ROOT, "cli"))
    wheel = next((d / "wheel").glob("cairn_cli-*.whl"))
    venv.EnvBuilder(with_pip=True).create(d / "venv")
    bindir = d / "venv" / ("Scripts" if os.name == "nt" else "bin")
    run(str(bindir / "python"), "-m", "pip", "install", "-q", str(wheel))
    return bindir


def test_only_the_client_is_installed_and_no_command_imports_the_server(client_venv, tmp_path):
    py, cairn = str(client_venv / "python"), str(client_venv / "cairn")
    installed = {line.split("==")[0].lower() for line in run(py, "-m", "pip", "freeze").splitlines()}
    assert not installed & set(SERVER), installed & set(SERVER)
    assert {"httpx", "keyring", "pyyaml"} <= installed
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "CAIRN_URL", "CAIRN_TOKEN")}
    env["CAIRN_CONFIG_DIR"] = str(tmp_path / "cfg")
    assert run(cairn, "--version", env=env).startswith("cairn ")
    for args in (["login", "--help"], ["query", "--help"], ["completion", "bash"], ["flows", "--help"],
                 ["iac", "plan", "--help"], ["ontology", "--help"]):
        run(cairn, *args, env=env)
    out = run(cairn, "iac", "validate", os.path.join(ROOT, "examples", "bundles", "cairn.yaml"), "--var", "ENV=prod", env=env)
    assert "valid" in out                                                     # policies validate without the server
    assert "valid" in run(cairn, "flows", "validate", os.path.join(ROOT, "tests", "data", "airflow_golden", "nightly.yaml"),
                          env=env)
    not_signed_in = subprocess.run([cairn, "whoami"], env=env, capture_output=True, text=True, timeout=60)
    assert not_signed_in.returncode == 3 and "cairn login" in not_signed_in.stderr
    probe = ("import sys, cairn_cli.main as m, cairn_cli.commands, cairn_cli.auth, cairn_cli.completion, "
             "cairn.iac.bundle, cairn.iac.export, cairn.flows.cli, cairn.governance.abac\n"
             "m.build_parser()\n"
             f"print(sorted(x for x in {SERVER!r} if x in sys.modules))")
    assert run(py, "-c", probe, env=env).strip() == "[]"
