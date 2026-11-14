"""``cairn airflow ...``: managed Airflow from the command line (and CI), and local development like ``astro dev``.

Deployments (Cairn's API as you: ``CAIRN_URL`` and ``CAIRN_TOKEN``/``CAIRN_TOKEN_FILE``, or ``cairn login``)::

    cairn airflow list
    cairn airflow create deployment.yaml            # {name, environment, team, secrets, hibernation, ...}
    cairn airflow status finance-dev
    cairn airflow deploy finance-dev --dags-only    # the project's dags/ as a versioned bundle (no restart)
    cairn airflow deploy finance-dev --image registry.example.com/finance/airflow:7   # a team image, then the DAGs
    cairn airflow deploys finance-dev
    cairn airflow rollback finance-dev <deploy id>
    cairn airflow promote finance-dev [--to finance-prod]  # waits for an approver when the target says so
    cairn airflow approve <promotion id>
    cairn airflow upgrade finance-dev 3.3.2          # refused when the DAGs do not parse on it
    cairn airflow hibernate|wake finance-dev
    cairn airflow logs finance-dev [--component scheduler] [--task dag_id/run_id/task_id]

Local development (Docker)::

    cairn airflow dev init [DIR]     # dags/, requirements.txt, Dockerfile FROM cairn/airflow, tests/
    cairn airflow dev parse          # every DAG imports (DagBag, no import errors)
    cairn airflow dev pytest         # the project's tests in its image
    cairn airflow dev start          # a local Airflow (http://localhost:8080) whose tasks are you in Cairn
    cairn airflow dev logs | stop

The local Airflow's ``cairn`` connection carries your own Cairn token, from a file only you can read
(``.cairn/dev.env``), never a command line; ``cairn airflow dev stop`` removes it.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from typing import Any
from urllib.parse import quote

DEFAULT_IMAGE = "cairn/airflow:0.1.0"
DOCKERFILE = """# The deployment's image: Cairn's Airflow (sign-in through Cairn, the cairn connection, OpenLineage) and your
# requirements. Build it and deploy it with `cairn airflow deploy <deployment> --image <registry>/<name>:<tag>`.
FROM {image}
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
"""
EXAMPLE_DAG = '''"""An example DAG: a query on the lakehouse, as whoever the Airflow's cairn connection is (you, locally; the
deployment's pipeline user once deployed)."""

from datetime import datetime

from airflow.sdk import DAG

from cairn_airflow.operators import CairnTrinoOperator

with DAG("example_lakehouse", start_date=datetime(2026, 1, 1), schedule="@daily", catchup=False, tags=["example"]):
    CairnTrinoOperator(task_id="count_orders", sql="SELECT count(*) FROM sales.orders")
'''
INTEGRITY_TEST = '''"""Every DAG in dags/ imports (what Cairn checks before it takes a deploy)."""

import os

DAGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dags")


def test_no_import_errors():
    try:
        from airflow.dag_processing.dagbag import DagBag
    except ImportError:
        from airflow.models.dagbag import DagBag
    bag = DagBag(DAGS)
    assert not bag.import_errors, bag.import_errors
    assert bag.dags, "no DAGs found"
'''


class CliError(Exception):
    pass


def _client():
    from cairn.flows.cli import CliError as FlowsError
    from cairn.flows.cli import _client as flows_client

    try:
        call = flows_client()
    except FlowsError as e:
        raise CliError(str(e))

    def wrapped(method: str, path: str, body: Any = None) -> Any:
        try:
            return call(method, path, body)
        except FlowsError as e:
            raise CliError(str(e))
    return wrapped


def _raw(method: str, path: str, content: bytes, ctype: str, params: dict) -> Any:
    """A request with a body that is not JSON (a DAG bundle)."""
    import httpx

    url, token, verify = _credentials()
    r = httpx.request(method, f"{url}{path}", params=params, content=content, timeout=900, verify=verify,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": ctype})
    if r.status_code >= 300:
        try:
            doc = r.json()
        except ValueError:
            raise CliError(f"{method} {path}: {r.status_code} {r.text[:500]}")
        errs = (doc.get("check") or {}).get("import_errors") or {}
        lines = [f"{method} {path}: {r.status_code} {doc.get('detail')}"] + [f"  {f}: {e.strip().splitlines()[-1] if e.strip() else e}"
                                                                          for f, e in errs.items()]
        raise CliError("\n".join(lines))
    return r.json() if r.content else None


def _credentials() -> tuple[str, str, Any]:
    try:
        from cairn_cli.errors import CliError as SignInError
        from cairn_cli.session import api_credentials
    except ImportError:  # pragma: no cover - the CLI package ships with Cairn
        api_credentials = None
    if api_credentials is not None:
        try:
            return api_credentials()
        except SignInError as e:
            raise CliError(f"{e} (or set CAIRN_URL and CAIRN_TOKEN)")
    url, token = os.environ.get("CAIRN_URL", "").rstrip("/"), os.environ.get("CAIRN_TOKEN", "")
    if not token and os.environ.get("CAIRN_TOKEN_FILE"):
        token = open(os.environ["CAIRN_TOKEN_FILE"]).read().strip()
    if not url or not token:
        raise CliError("set CAIRN_URL and CAIRN_TOKEN (or CAIRN_TOKEN_FILE), or run `cairn login`")
    return url, token, os.environ.get("CAIRN_CA_FILE") or True


def _git_sha(path: str) -> str:
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
    except OSError:
        return ""
    if r.returncode != 0:
        return ""
    sha = r.stdout.strip()
    dirty = subprocess.run(["git", "-C", path, "status", "--porcelain"], capture_output=True, text=True, timeout=30)
    if dirty.returncode == 0 and dirty.stdout.strip():
        print(f"note: {path} has changes not committed; recording {sha[:12]} anyway", file=sys.stderr)
    return sha


def _load(path: str) -> dict:
    import yaml

    try:
        doc = yaml.safe_load(open(path).read())
    except (OSError, yaml.YAMLError) as e:
        raise CliError(f"{path}: {e}")
    if not isinstance(doc, dict):
        raise CliError(f"{path}: a deployment is a mapping (name, environment, ...)")
    return doc


def _deploy_line(d: dict) -> str:
    what = f"DAGs ({d.get('files') or 0} files)" if d["kind"] == "dags" else d.get("image") or ""
    sha = f" git {d['git_sha'][:12]}" if d.get("git_sha") else ""
    src = f" [{d['source']}]" if d.get("source") else ""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["created_ms"] / 1000))
    return f"#{d['seq']:<4} {d['id']}  {d['state']:<10} {when}  {what}{sha}{src}  by {d['created_by']}"


# ---- local development -------------------------------------------------------------------------------------------
def _project(path: str) -> str:
    p = os.path.abspath(path or ".")
    if not os.path.isdir(os.path.join(p, "dags")):
        raise CliError(f"{p} is not an Airflow project (no dags/): run `cairn airflow dev init`")
    return p


def _dev_name(project: str) -> str:
    return "cairn-airflow-dev-" + (re.sub(r"[^a-z0-9-]", "-", os.path.basename(project).lower()).strip("-") or "project")


def _docker(*args: str, check: bool = True, capture: bool = True, timeout: int = 1800) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(["docker", *args], capture_output=capture, text=True, timeout=timeout)
    except OSError as e:
        raise CliError(f"docker: {e}")
    if check and r.returncode != 0:
        raise CliError(f"docker {args[0]}: {(r.stderr or r.stdout or '').strip()[-1500:]}")
    return r


def dev_init(path: str, image: str) -> list[str]:
    p = os.path.abspath(path or ".")
    files = {"dags/example_lakehouse.py": EXAMPLE_DAG, "tests/test_dag_integrity.py": INTEGRITY_TEST,
             "requirements.txt": "# Python packages your DAGs need (pinned), installed into the deployment's image\n",
             "Dockerfile": DOCKERFILE.format(image=image), ".airflowignore": "__pycache__/\n",
             ".gitignore": ".cairn/\n__pycache__/\n", ".dockerignore": ".cairn/\n.git/\n"}
    made = []
    for rel, text in files.items():
        full = os.path.join(p, rel)
        if os.path.exists(full):
            continue
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(text)
        made.append(rel)
    return made


def _dev_image(project: str) -> str:
    tag = _dev_name(project) + ":dev"
    _docker("build", "-q", "-t", tag, project, timeout=3600)
    return tag


def dev_check(project: str, image: str = "") -> dict:
    """Parses the project's DAGs in its image (no database): the DAGs found and the import errors."""
    from cairn.airflow_deploy.docker_runtime import CHECK, dagbag_result

    image = image or _dev_image(project)
    script = CHECK.replace("set -a; . /opt/cairn/airflow.env; set +a\n", "")
    r = _docker("run", "--rm", "-e", "CONNECTION_CHECK_MAX_COUNT=0", "-v", f"{project}/dags:/opt/cairn/check:ro",
                image, "bash", "-c", script, check=False)
    result = dagbag_result(r.stdout or "")
    if result is None:
        raise CliError(f"the DAG check gave no result: {(r.stderr or r.stdout)[-1500:]}")
    return result


def dev_pytest(project: str, image: str = "", args: tuple[str, ...] = ()) -> int:
    image = image or _dev_image(project)
    r = _docker("run", "--rm", "-e", "CONNECTION_CHECK_MAX_COUNT=0", "-e", "AIRFLOW__CORE__LOAD_EXAMPLES=false",
                "-v", f"{project}:/opt/project:ro", "-w", "/opt/project", image, "bash", "-c",
                "python -c 'import pytest' 2>/dev/null || { echo 'the image has no pytest: build FROM cairn/airflow (it "
                "ships pytest) or add pytest to requirements.txt' >&2; exit 2; }; "
                "export AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=sqlite:////tmp/airflow.db; airflow db migrate >/dev/null 2>&1; "
                "python -m pytest -q -p no:cacheprovider " + " ".join(f"'{a}'" for a in args or ("tests",)),
                check=False, capture=False, timeout=3600)
    return r.returncode


def dev_start(project: str, port: int = 8080) -> str:
    """A local Airflow in Docker (standalone: API server, scheduler, DAG processor, triggerer), the project's dags/
    mounted, its cairn connection and secrets backend signed in to Cairn as you (your token in a file)."""
    url, token, verify = _credentials()
    # Cairn on this machine is the host from inside the container.
    url = re.sub(r"^(https?://)(localhost|127\.0\.0\.1)([:/]|$)", r"\1host.docker.internal\3", url)
    image = _dev_image(project)
    name = _dev_name(project)
    d = os.path.join(project, ".cairn")
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o700)
    env_path = os.path.join(d, "dev.env")
    trino = json.loads(os.environ.get("CAIRN_AIRFLOW_TRINO", "") or "{}")
    backend = {"cairn_url": url, "token_env": "CAIRN_TOKEN", "trino_host": trino.get("host", "localhost"),
               "trino_port": int(trino.get("port", 8443)), "trino_catalog": trino.get("catalog", "lakehouse")}
    env = {"CAIRN_TOKEN": token, "CAIRN_URL": url, "AIRFLOW__CORE__LOAD_EXAMPLES": "false",
           "AIRFLOW__SECRETS__BACKEND": "cairn_airflow.secrets.CairnSecretsBackend",
           "AIRFLOW__SECRETS__BACKEND_KWARGS": json.dumps(backend),
           "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_ALL_ADMINS": "true", "CONNECTION_CHECK_MAX_COUNT": "0",
           "AIRFLOW__API__BASE_URL": f"http://localhost:{port}",
           # Tasks reach the API server inside the container, whatever port the browser uses.
           "AIRFLOW__CORE__EXECUTION_API_SERVER_URL": "http://localhost:8080/execution/"}
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as fh:
        fh.write("".join(f"{k}={v}\n" for k, v in env.items()))
    _docker("rm", "-f", name, check=False)
    _docker("run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8080", "--env-file", env_path,
            "--add-host", "host.docker.internal:host-gateway",
            "-v", f"{project}/dags:/opt/airflow/dags:ro", image, "airflow", "standalone")
    return f"http://localhost:{port}"


def dev_stop(project: str) -> None:
    _docker("rm", "-f", _dev_name(project), check=False)
    try:
        os.remove(os.path.join(project, ".cairn", "dev.env"))
    except OSError:
        pass


# ---- the commands ------------------------------------------------------------------------------------------------
def main(a) -> int:
    try:
        return _main(a)
    except CliError as e:
        print(f"cairn airflow: {e}", file=sys.stderr)
        return 1


def _main(a) -> int:
    cmd = a.airflow_cmd
    if cmd == "dev":
        return _dev(a)
    call = _client()
    base = "/v1/airflow/deployments"
    if cmd == "list":
        for d in call("GET", base)["deployments"]:
            print(f"{d['name']:<24} {d['environment']:<8} {d['state']:<12} Airflow {d['airflow_version']:<8} {d.get('url') or ''}")
        return 0
    if cmd == "create":
        out = call("POST", base, _load(a.file))
        print(f"{out['name']}: {out['state']} at {out.get('url')}")
        return 0 if out["state"] in ("ready", "creating") else 1
    if cmd == "status":
        s = call("GET", f"{base}/{quote(a.name)}/status")
        print(f"{a.name}: {s.get('health')} ({s.get('state')})")
        for k, v in (s.get("components") or {}).items():
            print(f"  {k:<14} {v.get('status')}")
        if s.get("tasks"):
            print(f"  tasks          {s['tasks']['running']} running, {s['tasks']['queued']} queued")
        for e in s.get("import_errors") or []:
            print(f"  import error   {e['file']}")
        for p in s.get("problems") or []:
            print(f"  problem        {p}")
        return 0 if s.get("health") in ("healthy", "hibernating") else 1
    if cmd == "deploy":
        project = os.path.abspath(a.project)
        sha = a.git_sha or _git_sha(project)
        if a.image:
            out = call("POST", f"{base}/{quote(a.name)}/deploys", {"image": a.image, "note": a.note or "", "git_sha": sha})
            print(f"{a.name}: image {a.image} live (deploy {out['id']}, #{out['seq']})")
            if a.image_only:
                return 0
        from cairn.airflow_deploy.bundle import BundleError, pack

        try:
            data = pack(os.path.join(project, a.dags))
        except BundleError as e:
            raise CliError(str(e))
        out = _raw("POST", f"{base}/{quote(a.name)}/deploys", data, "application/gzip",
                   {"note": a.note or "", "git_sha": sha, **({"force": "true"} if a.force else {})})
        dags = ", ".join((out.get("checked") or {}).get("dags") or [])
        print(f"{a.name}: DAGs live (deploy {out['id']}, #{out['seq']}, {out['files']} files{', git ' + sha[:12] if sha else ''})"
              + (f": {dags}" if dags else ""))
        return 0
    if cmd == "deploys":
        for d in call("GET", f"{base}/{quote(a.name)}/deploys")["deploys"]:
            print(_deploy_line(d))
        return 0
    if cmd == "rollback":
        out = call("POST", f"{base}/{quote(a.name)}/deploys/{quote(a.deploy)}/rollback")
        print(f"{a.name}: rolled back (deploy {out['id']}, #{out['seq']})")
        return 0
    if cmd == "promote":
        body = {k: v for k, v in (("to", a.to), ("deploy", a.deploy), ("comment", a.comment)) if v}
        out = call("POST", f"{base}/{quote(a.name)}/promote", body)
        print(f"promotion {out['id']} {a.name} -> {out['target']}: {out['state']}"
              + (" (waiting for an approver: cairn airflow approve " + out["id"] + ")" if out["state"] == "pending" else ""))
        return 0 if out["state"] in ("pending", "applied") else 1
    if cmd in ("approve", "reject"):
        out = call("POST", f"/v1/airflow/promotions/{quote(a.promotion)}/{cmd}", {"comment": a.comment or ""})
        print(f"promotion {out['id']}: {out['state']}")
        return 0
    if cmd == "upgrade":
        out = call("PATCH", f"{base}/{quote(a.name)}", {"airflow_version": a.version})
        print(f"{a.name}: Airflow {out['airflow_version']} ({out['state']})")
        return 0
    if cmd in ("hibernate", "wake"):
        out = call("POST", f"{base}/{quote(a.name)}/{cmd}", {} if cmd == "hibernate" else {"minutes": a.minutes})
        print(f"{a.name}: {out['state']}")
        return 0
    if cmd == "logs":
        if a.task:
            parts = a.task.split("/")
            if len(parts) != 3:
                raise CliError("--task is dag_id/run_id/task_id")
            q = f"dag_id={quote(parts[0])}&run_id={quote(parts[1])}&task_id={quote(parts[2])}&try_number={a.try_number}"
            print(call("GET", f"{base}/{quote(a.name)}/task-logs?{q}")["log"])
        else:
            print(call("GET", f"{base}/{quote(a.name)}/logs?component={quote(a.component)}&tail={a.tail}")["log"])
        return 0
    if cmd == "delete":
        call("DELETE", f"{base}/{quote(a.name)}")
        print(f"{a.name}: deleted")
        return 0
    return 2


def _dev(a) -> int:
    sub = a.dev_cmd
    if sub == "init":
        made = dev_init(a.dir, a.image)
        print("made " + ", ".join(made) if made else "nothing to do: the project's files are there")
        return 0
    project = _project(a.dir)
    if sub == "parse":
        out = dev_check(project, a.image_tag)
        for f, e in (out["import_errors"] or {}).items():
            print(f"{f}: {e.strip().splitlines()[-1] if e.strip() else e}")
        print(f"{len(out['dags'])} DAG(s) on Airflow {out['airflow']}: {', '.join(out['dags'])}; "
              f"{len(out['import_errors'])} import error(s)")
        return 1 if out["import_errors"] else 0
    if sub == "pytest":
        return dev_pytest(project, a.image_tag, tuple(a.args or ()))
    if sub == "start":
        url = dev_start(project, a.port)
        print(f"local Airflow starting at {url} (tasks are you in Cairn); `cairn airflow dev logs` to follow it")
        return 0
    if sub == "logs":
        _docker("logs", "--tail", str(a.tail), _dev_name(project), capture=False, check=False)
        return 0
    if sub == "stop":
        dev_stop(project)
        print("stopped")
        return 0
    return 2


def add_parser(sub) -> None:
    af = sub.add_parser("airflow", help="managed Airflow deployments (deploy, promote, roll back) and local development")
    s = af.add_subparsers(dest="airflow_cmd", required=True)
    s.add_parser("list", help="your deployments")
    p = s.add_parser("create", help="a deployment from a YAML or JSON file")
    p.add_argument("file")
    for name, what in (("status", "health: components, heartbeat, import errors, tasks"), ("deploys", "its deploys"),
                       ("delete", "delete it"), ("hibernate", "stop everything (non-prod)")):
        s.add_parser(name, help=what).add_argument("name")
    p = s.add_parser("wake", help="start a hibernating deployment")
    p.add_argument("name")
    p.add_argument("--minutes", type=int, default=None, help="stay up this long, even outside its hours")
    p = s.add_parser("deploy", help="deploy the project's DAGs (and, with --image, a team image) to a deployment")
    p.add_argument("name")
    p.add_argument("--dags-only", action="store_true", help="only the DAGs (the default without --image)")
    p.add_argument("--image", help="a team image built FROM cairn/airflow (pushed where the deployment pulls)")
    p.add_argument("--image-only", action="store_true", help="with --image: not the DAGs")
    p.add_argument("--project", default=".", help="the project directory (default: here)")
    p.add_argument("--dags", default="dags", help="the DAG folder in it")
    p.add_argument("--note", help="what this deploy is")
    p.add_argument("--git-sha", help="the commit (default: the project's HEAD)")
    p.add_argument("--force", action="store_true", help="deploy DAGs even when some do not import")
    p = s.add_parser("rollback", help="back to an earlier deploy")
    p.add_argument("name")
    p.add_argument("deploy")
    p = s.add_parser("promote", help="promote a deploy to the next environment")
    p.add_argument("name")
    p.add_argument("--to", help="the target deployment (default: its promotes_to)")
    p.add_argument("--deploy", help="the deploy (default: its live DAGs)")
    p.add_argument("--comment")
    for verb in ("approve", "reject"):
        p = s.add_parser(verb, help=f"{verb} a promotion waiting for approval")
        p.add_argument("promotion")
        p.add_argument("--comment")
    p = s.add_parser("upgrade", help="a new Airflow version (its DAGs parsed on it first)")
    p.add_argument("name")
    p.add_argument("version")
    p = s.add_parser("logs", help="a component's log, or a task's (--task dag/run/task)")
    p.add_argument("name")
    p.add_argument("--component", default="scheduler")
    p.add_argument("--tail", type=int, default=200)
    p.add_argument("--task", help="dag_id/run_id/task_id")
    p.add_argument("--try-number", type=int, default=1)
    dev = s.add_parser("dev", help="local development: init, parse, pytest, start, logs, stop")
    ds = dev.add_subparsers(dest="dev_cmd", required=True)
    p = ds.add_parser("init", help="a project skeleton: dags/, requirements.txt, Dockerfile, tests/")
    p.add_argument("dir", nargs="?", default=".")
    p.add_argument("--image", default=DEFAULT_IMAGE, help=f"the base image (default {DEFAULT_IMAGE})")
    for name, what in (("parse", "every DAG imports (DagBag)"), ("pytest", "the project's tests in its image"),
                       ("start", "a local Airflow signed in to Cairn as you"), ("logs", "its log"),
                       ("stop", "stop it")):
        p = ds.add_parser(name, help=what)
        p.add_argument("--dir", default=".", help="the project directory")
        if name in ("parse", "pytest"):
            p.add_argument("--image-tag", default="", help="an image already built (default: build the project's)")
        if name == "pytest":
            p.add_argument("args", nargs="*", help="pytest's arguments (default: tests)")
        if name == "start":
            p.add_argument("--port", type=int, default=8080)
        if name == "logs":
            p.add_argument("--tail", type=int, default=200)
    af.set_defaults(fn=lambda a: sys.exit(main(a)))
