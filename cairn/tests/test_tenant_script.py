"""deploy/hosting/tenant.sh run against stand-ins for kubectl, helm and psql that record every call.

The real cluster run is deploy/hetzner/ci/k3s-smoke.sh (CI job ``hetzner``); this checks what the script itself
does with its secrets and with the namespaces it is told to remove, on any machine."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# kubectl, helm and psql: each call's argv, the contents of any --from-file it was given (read through the file
# descriptor it inherited, as kubectl would) and its stdin, one JSON line per call.
STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
files = {}
for a in args:
    if a.startswith("--from-file="):
        key, _, path = a[len("--from-file="):].partition("=")
        with open(path) as f:
            files[key] = f.read()
stdin = ""
if "-i" in args or args[-2:] == ["-f", "-"]:
    stdin = sys.stdin.read()
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({"tool": os.path.basename(sys.argv[0]), "argv": args, "files": files, "stdin": stdin}) + "\n")
if args[:1] == ["-n"]:
    args = args[2:]
if os.path.basename(sys.argv[0]) == "kubectl" and args[:2] == ["get", "namespace"]:
    ns = os.environ.get("STUB_NAMESPACES", "")
    owners = dict(p.split("=", 1) for p in ns.split(",") if p)
    if args[2] not in owners:
        sys.exit(1)
    if "jsonpath={.metadata.labels.cairn\\.io/tenant}" in args:
        sys.stdout.write(owners[args[2]])
    sys.exit(0)
if os.path.basename(sys.argv[0]) == "kubectl" and args[:2] == ["get", "pod"]:
    print("pod/cairn-pg-1")
'''


@pytest.fixture()
def stubs(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("kubectl", "helm", "psql"):
        p = bin_dir / tool
        p.write_text(STUB.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
        p.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    log.touch()
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB_LOG": str(log), "HELM": str(bin_dir / "helm"),
           "CAIRN_HOST_DOMAIN": "cairn.test", "CAIRN_PG_HOST": "pg.test", "CAIRN_PG_KUBECTL": "cairn-platform/cairn-pg",
           "CAIRN_TENANT_SERVICES": "1", "PYTHON": sys.executable}
    for k in ("CAIRN_INGRESS_NAMESPACE", "CAIRN_TENANT_SERVICES_HA", "CAIRN_GATEWAY", "CAIRN_TLS_ISSUER"):
        env.pop(k, None)

    def run(*args, **extra):
        log.write_text("")
        r = subprocess.run(["deploy/hosting/tenant.sh", *args], cwd=ROOT, env={**env, **extra},
                           capture_output=True, text=True, timeout=120)
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return r, calls

    return run, tmp_path


def _licence(tmp_path, tenant: str) -> str:
    out = subprocess.run([sys.executable, "deploy/ops/licence.py", "issue", "--key", "deploy/licence/dev-private-key.pem",
                          "--kid", "cairn-dev", "--customer", "Test", "--tenant", tenant, "--days", "30"],
                         cwd=ROOT, capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": "src"})
    assert out.returncode == 0, out.stderr
    path = tmp_path / f"{tenant}.licence"
    path.write_text(out.stdout)
    return str(path)


def test_create_keeps_every_generated_secret_off_command_lines(stubs):
    """Command lines are readable by every user of the machine (ps, /proc/<pid>/cmdline): the tenant's
    database password and the release's keys go to kubectl through file descriptors, never as arguments."""
    run, tmp_path = stubs
    r, calls = run("create", "acme", "--licence", _licence(tmp_path, "acme"))
    assert r.returncode == 0, r.stderr
    secrets = {}
    for c in calls:
        if c["tool"] == "kubectl" and "secret" in c["argv"] and "generic" in c["argv"]:
            name = c["argv"][c["argv"].index("generic") + 1]
            secrets[name] = c
    core, app = secrets["cairn-core-secrets"], secrets["cairn-secrets"]
    assert set(core["files"]) == {"admin-password", "system-client-secret", "session-signing-key", "token-salt",
                                  "web-secret", "db-password"}
    assert set(app["files"]) == {"service-token", "database-url", "action-link-secret", "embed-signing-secret"}
    pw = core["files"]["db-password"]
    assert len(pw) == 64 and pw in app["files"]["database-url"]
    assert app["files"]["database-url"] == f"postgresql+psycopg://cairn_acme:{pw}@pg.test:5432/cairn_acme"
    assert all(v and "\n" not in v for v in [*core["files"].values(), *app["files"].values()])
    # The role's password reached PostgreSQL on psql's stdin.
    assert any(f"password '{pw}'" in c["stdin"] for c in calls if "psql" in c["argv"])
    values = [*core["files"].values(), *app["files"].values()]
    for c in calls:
        for a in c["argv"]:
            assert not any(v in a for v in values), f"a secret on {c['tool']}'s command line: {a[:40]}..."
