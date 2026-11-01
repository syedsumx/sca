"""``cairn dbt <dbt arguments>``: runs dbt on the lakehouse as a Cairn identity, then records the run in Cairn.

1. Signs in to Cairn with the pipeline's Cairn token (``CAIRN_TOKEN`` or ``CAIRN_TOKEN_FILE``: a personal
   access token of a Cairn user such as ``svc-dbt``) and gets a short-lived Trino token for that user,
   so Trino's access control (Cairn's policies) applies to every query dbt sends, and lineage and usage
   name the pipeline's user. No long-lived database credential sits in a profile.
2. Writes a dbt profile (``trino``, ``method: jwt``) for the project's profile name into a private
   temporary directory, unless the arguments name their own ``--profiles-dir``.
3. Runs dbt with those arguments (dbt-trino's source, ``dbt-trino-<version>``, puts its queries in
   Trino's ``pipelines`` resource group).
4. Sends ``manifest.json`` and ``run_results.json`` to ``POST /pipelines/dbt/runs``: docs, tags, owners,
   lineage and test results land on the datasets the run built.

The exit code is dbt's. If dbt succeeded but the run could not be recorded, it is 3 (set
``CAIRN_DBT_RECORD=optional`` to only warn).

Configuration::

    CAIRN_URL              Cairn's API, e.g. https://cairn.example.com/cairn/api
    CAIRN_TOKEN[_FILE]     the pipeline's Cairn token
    CAIRN_CA_FILE          a CA for Cairn's and Trino's certificates (default: the system's)
    CAIRN_TRINO_HOST       Trino (cairn-trino); CAIRN_TRINO_PORT (8443)
    CAIRN_TRINO_CATALOG    the dbt "database" (lakehouse)
    CAIRN_DBT_SCHEMA       the default schema (analytics); CAIRN_DBT_THREADS (4)
    CAIRN_DBT_TOKEN_TTL    seconds the Trino token lasts (14400; at most 43200): the longest run
    CAIRN_DBT_RECORD       required (default) or optional
    CAIRN_DBT_TARGET_PATH  where dbt writes its artifacts unless --target-path is given (the image: /tmp/cairn/target)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import httpx
import yaml

class RunnerError(Exception):
    pass


def _env(name: str, default: Optional[str] = None) -> str:
    v = os.environ.get(name, "")
    if not v and default is None:
        raise RunnerError(f"set {name}")
    return v or (default or "")


def _token() -> str:
    path = os.environ.get("CAIRN_TOKEN_FILE")
    if path:
        return Path(path).read_text().strip()
    return _env("CAIRN_TOKEN")


def _arg(args: list[str], flag: str) -> Optional[str]:
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def _verify():
    return os.environ.get("CAIRN_CA_FILE") or True


def trino_token(client: httpx.Client, base: str, token: str, ttl: int) -> dict:
    r = client.post(f"{base}/v1/trino/token", json={"ttl_seconds": ttl}, headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        raise RunnerError(f"Cairn refused a Trino token ({r.status_code}): {r.text[:300]}")
    return r.json()


def profile(name: str, tt: dict) -> dict:
    ca = os.environ.get("CAIRN_CA_FILE")
    return {name: {"target": "cairn", "outputs": {"cairn": {
        "type": "trino", "method": "jwt", "jwt_token": tt["token"], "user": tt["user"],
        "host": _env("CAIRN_TRINO_HOST", "cairn-trino"), "port": int(_env("CAIRN_TRINO_PORT", "8443")),
        "http_scheme": "https", "cert": ca if ca else True,
        "database": _env("CAIRN_TRINO_CATALOG", "lakehouse"), "schema": _env("CAIRN_DBT_SCHEMA", "analytics"),
        "threads": int(_env("CAIRN_DBT_THREADS", "4")), "client_tags": ["cairn"],
        "session_properties": {}, "prepared_statements_enabled": True}}}}


def record(client: httpx.Client, base: str, token: str, target: Path) -> dict:
    manifest, results = target / "manifest.json", target / "run_results.json"
    if not manifest.exists():
        raise RunnerError(f"{manifest} is missing: nothing to record")
    body = {"manifest": json.loads(manifest.read_text())}
    if results.exists():
        body["run_results"] = json.loads(results.read_text())
    r = client.post(f"{base}/pipelines/dbt/runs", json=body, headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        raise RunnerError(f"Cairn did not record the run ({r.status_code}): {r.text[:300]}")
    return r.json()


def _dbt_cmd() -> list[str]:
    # dbt from PATH, else the dbt installed beside this Python.
    return [shutil.which("dbt")] if shutil.which("dbt") else [sys.executable, "-c", "from dbt.cli.main import cli; cli()"]


def run(args: list[str]) -> int:
    if not args or args[0] in ("--version", "-v", "--help", "-h"):
        return subprocess.call([*_dbt_cmd(), *args])  # nothing to sign in for
    base = _env("CAIRN_URL").rstrip("/")
    if _arg(args, "--target-path") is None and os.environ.get("CAIRN_DBT_TARGET_PATH"):
        args = [*args, "--target-path", os.environ["CAIRN_DBT_TARGET_PATH"]]  # a read-only project still runs
    token = _token()
    ttl = int(_env("CAIRN_DBT_TOKEN_TTL", "14400"))
    project = Path(_arg(args, "--project-dir") or ".")
    spec = project / "dbt_project.yml"
    if not spec.exists():
        raise RunnerError(f"no dbt_project.yml in {project.resolve()} (pass --project-dir)")
    proj = yaml.safe_load(spec.read_text()) or {}
    given = _arg(args, "--target-path")
    target_path = Path(given) if given and Path(given).is_absolute() else project / (given or proj.get("target-path") or "target")
    tmp = None
    with httpx.Client(timeout=60, verify=_verify()) as client:
        cmd = [*_dbt_cmd(), *args]
        env = {**os.environ, "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
        if os.environ.get("CAIRN_CA_FILE"):
            # requests lets these override the CA the profile names: Trino's certificate is checked
            # against CAIRN_CA_FILE only.
            for k in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
                env.pop(k, None)
        try:
            if _arg(args, "--profiles-dir") is None:
                tt = trino_token(client, base, token, ttl)
                tmp = tempfile.mkdtemp(prefix="cairn-dbt-")
                os.chmod(tmp, 0o700)
                path = Path(tmp) / "profiles.yml"
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    yaml.safe_dump(profile(proj.get("profile") or "default", tt), f)
                cmd += ["--profiles-dir", tmp]
                print(f"cairn dbt: running as {tt['user']} (Trino token until {tt.get('expires_at')})", file=sys.stderr)
            code = subprocess.call(cmd, env=env)
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        sub = next((a for a in args if not a.startswith("-")), "")
        if sub not in ("run", "build", "test", "seed", "snapshot"):
            return code
        try:
            out = record(client, base, token, target_path)
            print(f"cairn dbt: recorded {out.get('models')} models, {out.get('lineage_edges')} lineage edges"
                  + (f", {(out.get('tests') or {}).get('checks', 0)} tests" if out.get("tests") else ""), file=sys.stderr)
        except (RunnerError, httpx.HTTPError, ValueError) as e:
            print(f"cairn dbt: {e}", file=sys.stderr)
            if code == 0 and os.environ.get("CAIRN_DBT_RECORD", "required") != "optional":
                return 3
        return code


def main(args: list[str]) -> int:
    try:
        return run(args)
    except RunnerError as e:
        print(f"cairn dbt: {e}", file=sys.stderr)
        return 2
