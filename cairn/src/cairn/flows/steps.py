"""What each kind of flow step does, always as the flow's owner.

An executor gets a :class:`Ctx` (the owner, the step with its templates filled in, what earlier steps gave)
and returns :class:`Done` (an output, kept with the run, and a log) or :class:`Wait` (look again at
``next_ms``: an approval, a wait, an Airflow DAG run or a stream catching up), or raises
:class:`StepFailed`. Executors only call Cairn's own services, as the owner, so every grant, mask, row
filter, quota and audit rule of those services applies: SQL through the SQL workspace (Trino as the owner),
derived tables only the owner may refresh, jobs in a container with the owner's Trino token, agent apps as
the owner. When a token or a decision cannot be had, the step fails; nothing runs as anyone else.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from cairn.flows import expr

logger = logging.getLogger(__name__)

OPS: dict[str, Callable[[float, float], bool]] = {
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
}


class StepFailed(Exception):
    pass


@dataclass
class Done:
    output: dict = field(default_factory=dict)
    log: str = ""


@dataclass
class Wait:
    next_ms: int
    state: dict = field(default_factory=dict)
    log: str = ""


@dataclass
class Ctx:
    owner: str
    flow: str
    run_id: str
    step: dict
    attempt: int
    values: dict                       # params, steps, run, flow (what templates and expressions see)
    started_ms: int
    deadline_ms: int
    timezone: str = "UTC"
    state: dict = field(default_factory=dict)          # what a waiting step kept between looks
    decision: Optional[dict] = None                    # an approval's decision, once someone made it
    cancelled: Any = None                              # a threading.Event, set when the run is cancelled
    children: Optional[Callable[[str, dict], None]] = None   # a for-each records each item's status
    lines: list = field(default_factory=list)

    def log(self, msg: str) -> None:
        self.lines.append(f"{time.strftime('%H:%M:%S', time.gmtime())} {msg}")

    def is_cancelled(self) -> bool:
        return bool(self.cancelled is not None and self.cancelled.is_set())


class Backends:
    """The services steps run through (any may be None: a step that needs one then fails, saying what is off)."""

    def __init__(self, **kw: Any):
        self.workspace = kw.get("workspace")                # SqlWorkspace
        self.derived = kw.get("derived")                    # DerivedTables
        self.jobs = kw.get("jobs")                          # Jobs
        self.agent_apps = kw.get("agent_apps")              # AgentApps
        self.pipes = kw.get("pipes")                        # Pipes
        self.streams = kw.get("streams")                    # Streams
        self.notify = kw.get("notify")                      # (type, title, body, recipients, payload)
        self.is_operator: Callable[[str], bool] = kw.get("is_operator") or (lambda actor: False)
        self.lakehouse_sessions = kw.get("lakehouse_sessions")
        self.airflow: dict = kw.get("airflow") if kw.get("airflow") is not None else airflow_from_env()
        self.dbt_image: str = kw.get("dbt_image", os.environ.get("CAIRN_FLOWS_DBT_IMAGE", ""))
        self.spark_image: str = kw.get("spark_image", os.environ.get("CAIRN_FLOWS_SPARK_IMAGE", ""))
        self.cairn_url: str = kw.get("cairn_url", os.environ.get("CAIRN_FLOWS_CAIRN_URL", ""))
        self.catalog_uri: str = kw.get("catalog_uri", os.environ.get("CAIRN_FLOWS_CATALOG_URI", ""))
        # The Trino catalog dbt steps build in (their profile's database), and the uid Spark steps run as (the
        # cairn/spark image's own non-root user: Hadoop needs a user name for the uid).
        self.trino_catalog: str = kw.get("trino_catalog", os.environ.get("CAIRN_FLOWS_TRINO_CATALOG", "lakehouse"))
        self.spark_uid: int = int(kw.get("spark_uid", os.environ.get("CAIRN_FLOWS_SPARK_UID", "185")))
        self.public_url: str = kw.get("public_url", "")
        # Platform steps (cairn.flows.platforms): credentials from secret scopes, read as the owner; egress rules.
        self.secrets = kw.get("secrets")                    # SecretScopes
        self.airflow_deployments = kw.get("airflow_deployments")   # managed Airflow (cairn.airflow_deploy)
        self.egress = kw.get("egress")                      # cairn.network.egress.Egress (set once it is built)


def need(service: Any, what: str) -> Any:
    if service is None:
        raise StepFailed(f"{what} is not set up in this Cairn")
    return service


# ---- data ---------------------------------------------------------------------------------------------
def resolve_query(ws: Any, owner: str, ref: str) -> str:
    """A saved query's id, from its id or its name, among those the owner may open."""
    from cairn.sqlworkspace.workspace import WorkspaceError

    try:
        return ws.get(owner, ref)["id"]
    except WorkspaceError:
        pass
    named = [q for q in ws.saved(owner) if q.get("name") == ref]
    if len(named) != 1:
        raise StepFailed(f"no saved query {ref!r} the owner may open" if not named else
                         f"{len(named)} saved queries are named {ref!r}; use its id")
    return named[0]["id"]


def run_query(ws: Any, owner: str, s: dict, limit: int = 1000) -> dict:
    """A saved query (``query``) or ``sql`` on a ``connection``, run as ``owner`` through the SQL workspace (their
    policies, masks and row filters): rows, columns and the first 20 rows."""
    from cairn.sqlworkspace.workspace import WorkspaceError

    try:
        if s.get("query"):
            res = ws.run_saved(owner, resolve_query(ws, owner, s["query"]), limit)
        else:
            res = ws.run(owner, s["sql"], s["connection"], limit)
    except WorkspaceError as e:
        raise StepFailed(str(e))
    rows, cols = res.get("rows") or [], res.get("columns") or []
    names = [c["name"] if isinstance(c, dict) else c for c in cols]
    return {"rows": len(rows), "columns": names, "preview": [list(r) for r in rows[:20]], "_rows": rows}


def check(ws: Any, owner: str, s: dict) -> dict:
    """A measure (``rows``, or a column of the first row) compared with ``op`` and ``value``; StepFailed when it
    does not hold."""
    res = run_query(ws, owner, s, 1000)
    if s.get("measure", "rows") == "rows":
        got = float(res["rows"])
    elif s["measure"] in res["columns"] and res["_rows"]:
        try:
            got = float(res["_rows"][0][res["columns"].index(s["measure"])])
        except (TypeError, ValueError):
            raise StepFailed(f"{s['measure']} is not a number")
    else:
        raise StepFailed(f"no column {s['measure']} (or no rows) to check")
    if not OPS[s["op"]](got, float(s["value"])):
        raise StepFailed(f"check failed: {s.get('measure', 'rows')} is {got:g}, not {s['op']} {float(s['value']):g}")
    return {"rows": res["rows"], "value": got, "passed": True, "detail": f"{s.get('measure', 'rows')} is {got:g}"}


def refresh_derived(derived: Any, is_operator: Callable[[str], bool], owner: str, table: str, full: bool,
                    trigger: str = "flow", did: Optional[str] = None) -> dict:
    """Refreshes a derived table the owner owns (or any, for an operator): the refresh runs as the table's owner."""
    if derived is None:
        raise StepFailed("derived tables are not set up in this Cairn")
    dt = derived.get(did) if did else next((d for d in derived.list() if d["name"] == table), None)
    if dt is None:
        raise StepFailed(f"no derived table {table!r}")
    if dt["owner"] != owner and not is_operator(owner):
        raise StepFailed(f"{dt['name']} is not the flow owner's to refresh")
    out = derived.refresh(dt["id"], full, trigger=trigger)
    if out.get("status") not in ("ok", "running", None):
        raise StepFailed(out.get("error") or f"the refresh of {dt['name']} ended {out.get('status')}")
    return {"table": dt["name"], "target": dt.get("target"), "kind": out.get("kind"), "rows": out.get("rows"),
            "detail": out.get("status")}


def _sql(b: Backends, ctx: Ctx) -> Done:
    ws = need(b.workspace, "the SQL workspace")
    out = run_query(ws, ctx.owner, ctx.step, ctx.step.get("limit") or 1000)
    out.pop("_rows", None)
    ctx.log(f"{out['rows']} rows")
    return Done(out)


def _check(b: Backends, ctx: Ctx) -> Done:
    out = check(need(b.workspace, "the SQL workspace"), ctx.owner, ctx.step)
    ctx.log(out["detail"])
    return Done(out)


def _derived(b: Backends, ctx: Ctx) -> Done:
    out = refresh_derived(b.derived, b.is_operator, ctx.owner, ctx.step["table"], bool(ctx.step.get("full")))
    ctx.log(f"refreshed {out['table']} ({out.get('kind')})")
    return Done(out)


def _pipe(b: Backends, ctx: Ctx) -> Done:
    pipes = need(b.pipes, "pipes")
    if not b.is_operator(ctx.owner):
        raise StepFailed("loading a pipe needs MANAGE_SYSTEM_OPERATIONS, which the flow's owner does not have")
    pipe = next((p for p in pipes.list() if p["name"] == ctx.step["pipe"]), None)
    if pipe is None:
        raise StepFailed(f"no pipe {ctx.step['pipe']!r}")
    loaded = pipes.sweep(pipe["id"])
    ctx.log(f"{len(loaded)} files loaded")
    return Done({"pipe": pipe["name"], "files": len(loaded), "rows": sum(int(x.get("rows") or 0) for x in loaded)})


def _stream_end(streams: Any, stream: dict) -> dict[int, int]:
    from kafka import KafkaConsumer, TopicPartition

    try:
        consumer = KafkaConsumer(**{"request_timeout_ms": 30_000, **streams.kafka})
    except Exception as e:  # noqa: BLE001 - no broker: the step fails, it never counts as caught up
        raise StepFailed(f"Kafka could not be reached for {stream['name']}: {type(e).__name__}")
    try:
        parts = consumer.partitions_for_topic(stream["topic"])
        if not parts:
            raise StepFailed(f"Kafka has no topic {stream['topic']!r} for {stream['name']}")
        ends = consumer.end_offsets([TopicPartition(stream["topic"], p) for p in parts])
        return {tp.partition: int(off) for tp, off in ends.items()}
    except StepFailed:
        raise
    except Exception as e:  # noqa: BLE001 - the broker went away while Cairn asked
        raise StepFailed(f"Kafka could not say where {stream['name']} ends: {type(e).__name__}")
    finally:
        consumer.close()


def _stream(b: Backends, ctx: Ctx) -> Any:
    streams = need(b.streams, "streams")
    try:
        st = streams.by_name(ctx.step["stream"])
    except Exception:  # noqa: BLE001 - not found (or the store is down): the step says which
        raise StepFailed(f"no stream {ctx.step['stream']!r}")
    target = ctx.state.get("target")
    if target is None:
        target = {str(p): o for p, o in _stream_end(streams, st).items()}
        ctx.log(f"waiting for {st['name']} to reach {target}")
    have = {str(p): o for p, o in streams.offsets(st).items()}
    if all(have.get(p, 0) >= o for p, o in target.items()):
        ctx.log(f"{st['name']} reached {have}")
        return Done({"stream": st["name"], "offsets": have, "target": target})
    return Wait(int(time.time() * 1000) + ctx.step.get("poll_s", 15) * 1000, {"target": target})


# ---- compute ------------------------------------------------------------------------------------------
# "x.y.SomeException: why", "Caused by: …", and Py4J's ": x.y.SomeException: why" under its Py4JJavaError line.
_CAUSE = re.compile(r"^(?:Caused by: |: )?[\w.$]+(?:Exception|Error)\b.*")


def _failure_lines(text: str, limit: int = 12) -> list[str]:
    """The exception lines of a failure (``x.y.SomeException: why`` and each ``Caused by:``), wherever they are in
    the output: a JVM puts them above a stack trace long enough to push them out of the tail."""
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if _CAUSE.match(line) and line not in out:
            out.append(line[:500])
            if len(out) >= limit:
                break
    return out


def _job_result(ctx: Ctx, status: str, output: Optional[str], log: Optional[str], error: Optional[str]) -> Done:
    if status != "succeeded":
        tail = "\n".join(x for x in ((log or "").strip(), (output or "").strip()) if x)
        if tail:                                     # what dbt, Spark or the script said, in the step's log
            causes = _failure_lines(tail) if len(tail) > 8000 else []
            if causes:
                ctx.log("the job failed with:\n" + "\n".join(causes))
            ctx.log("the job's last output:\n" + tail[-8000:])
        raise StepFailed(error or "the job failed")
    text = output or ""
    out: dict[str, Any] = {"status": status, "output": text[-4000:]}
    # The last line of a script's output, when it is a JSON object, is its result for later steps.
    last = text.strip().splitlines()[-1] if text.strip() else ""
    if last.startswith("{"):
        try:
            out["result"] = json.loads(last)
        except ValueError:
            pass
    return Done(out, (log or "")[-20_000:])


def _code(b: Backends, ctx: Ctx) -> Done:
    from cairn.pipelines.jobs import JobError

    jobs = need(b.jobs, "jobs")
    s = ctx.step
    params = {**(s.get("params") or {}), "flow": ctx.flow, "flow_run": ctx.run_id}
    try:
        if s.get("job"):
            r = jobs.run_for_flow(ctx.owner, s["job"], params, s.get("timeout_s"))
            return _job_result(ctx, r["status"], r.get("output"), r.get("log"), r.get("error"))
        source = json.dumps(s["notebook"]) if s["type"] == "notebook" else s["code"]
        status, output, log, error = jobs.contained(ctx.owner, s["type"], source, params, s["timeout_s"],
                                                    f"{ctx.flow}.{s['name']}", f"{ctx.run_id[:16]}-{s['name'][:20].lower()}")
    except JobError as e:
        raise StepFailed(str(e))
    return _job_result(ctx, status, output, log, error)


SPARK_DRIVER = """
import json, os, subprocess, sys, tempfile
from cairn_spark import lakehouse_conf
conf = lakehouse_conf(os.environ["CAIRN_URL"], os.environ["CAIRN_CATALOG_URI"], os.environ["CAIRN_LAKEHOUSE_CREDENTIAL"],
                      os.environ["CAIRN_LAKEHOUSE_TOKEN"], lineage=False, app_name=os.environ["CAIRN_JOB"])
conf.update(json.loads(os.environ.get("CAIRN_SPARK_CONF") or "{}"))
fd, props = tempfile.mkstemp(suffix=".conf")
with os.fdopen(fd, "w") as f:
    for k, v in conf.items():
        f.write(f"{k} {v}\\n")
spec = json.loads(os.environ["CAIRN_SPARK_APP"])
cmd = [os.path.join(os.environ.get("SPARK_HOME", "/opt/spark"), "bin", "spark-submit"), "--properties-file", props]
if spec.get("class_name"):
    cmd += ["--class", spec["class_name"]]
rc = subprocess.call(cmd + [spec["application"], *[str(a) for a in spec.get("args") or []]])
os.unlink(props)
sys.exit(rc)
"""

DBT_DRIVER = """
import json, os, subprocess, sys
from urllib.parse import urlsplit
import yaml
spec = json.loads(os.environ["CAIRN_DBT_STEP"])
url = urlsplit(os.environ["CAIRN_TRINO_URL"])
project = spec["project_dir"]
with open(os.path.join(project, "dbt_project.yml")) as f:
    name = str((yaml.safe_load(f) or {}).get("profile") or "cairn")
os.makedirs("/tmp/profiles", exist_ok=True)
out = {"type": "trino", "method": "jwt", "jwt_token": os.environ["CAIRN_TRINO_TOKEN"], "host": url.hostname,
       "port": url.port or 443, "http_scheme": url.scheme, "user": os.environ["CAIRN_USER"],
       "database": os.environ.get("CAIRN_TRINO_CATALOG", "lakehouse"),
       "schema": os.environ.get("CAIRN_DBT_SCHEMA", "analytics"), "threads": 4,
       # Trino's certificate is always checked: Cairn's CA when it has one, else the system's.
       "cert": os.environ.get("CAIRN_TRINO_CA_FILE") or True}
fd = os.open("/tmp/profiles/profiles.yml", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(json.dumps({name: {"target": "cairn", "outputs": {"cairn": out}}}))
cmd = ["dbt", "--no-use-colors", spec["command"], "--project-dir", project, "--profiles-dir", "/tmp/profiles",
       "--target-path", "/tmp/target"]
if spec.get("select"):
    cmd += ["--select", spec["select"]]
# dbt writes under /tmp only (the image's /tmp/cairn belongs to the image's own user); the token is in its profile only.
env = {k: v for k, v in os.environ.items() if k != "CAIRN_TRINO_TOKEN"}
env.update(DBT_LOG_PATH="/tmp/dbt/logs", DBT_PACKAGES_INSTALL_PATH="/tmp/dbt/packages", HOME="/tmp/dbt",
           DBT_SEND_ANONYMOUS_USAGE_STATS="false")
os.makedirs("/tmp/dbt/logs", exist_ok=True)
sys.exit(subprocess.call(cmd, env=env))
"""


def _dbt(b: Backends, ctx: Ctx) -> Done:
    from cairn.pipelines.jobs import JobError, with_image

    jobs = need(b.jobs, "jobs")
    if not b.dbt_image:
        raise StepFailed("dbt steps are not configured: set CAIRN_FLOWS_DBT_IMAGE (an image with dbt-trino and the project)")
    if jobs.runner is None:
        raise StepFailed("jobs are not configured: set CAIRN_JOBS_RUNTIME (docker or kubernetes)")
    if jobs.mint_token is None:
        raise StepFailed("dbt steps run on Trino as the flow's owner, and Trino is not set up here")
    s = ctx.step
    spec = {"command": s["command"], "select": s.get("select") or "", "project_dir": s["project_dir"]}
    try:
        status, output, log, error = jobs.contained(ctx.owner, "python", DBT_DRIVER, {}, s["timeout_s"],
                                                    f"{ctx.flow}.{s['name']}", f"{ctx.run_id[:16]}-dbt",
                                                    runner=with_image(jobs.runner, b.dbt_image),
                                                    extra_env={"CAIRN_DBT_STEP": json.dumps(spec),
                                                               "CAIRN_TRINO_CATALOG": b.trino_catalog})
    except JobError as e:
        raise StepFailed(str(e))
    return _job_result(ctx, status, output, log, error)


def _spark(b: Backends, ctx: Ctx) -> Done:
    from cairn.pipelines.jobs import JobError, with_image

    jobs = need(b.jobs, "jobs")
    if not b.spark_image or not b.cairn_url or not b.catalog_uri:
        raise StepFailed("Spark steps are not configured: set CAIRN_FLOWS_SPARK_IMAGE, CAIRN_FLOWS_CAIRN_URL and "
                         "CAIRN_FLOWS_CATALOG_URI")
    if jobs.runner is None:
        raise StepFailed("jobs are not configured: set CAIRN_JOBS_RUNTIME (docker or kubernetes)")
    sessions = need(b.lakehouse_sessions, "lakehouse sessions (Trino's token key)")
    s = ctx.step
    try:                                              # a lakehouse session for the owner, or nothing runs
        sess = sessions.issue(ctx.owner, min(max(s["timeout_s"], 300), 43200))
    except Exception as e:  # noqa: BLE001 - fail closed
        raise StepFailed(f"no lakehouse session for the owner: {e}")
    spec = {"application": s["application"], "class_name": s.get("class_name"), "args": s.get("args") or []}
    try:
        status, output, log, error = jobs.contained(
            ctx.owner, "python", SPARK_DRIVER, {}, s["timeout_s"], f"{ctx.flow}.{s['name']}", f"{ctx.run_id[:16]}-spark",
            runner=with_image(jobs.runner, b.spark_image, uid=b.spark_uid),
            extra_env={"CAIRN_URL": b.cairn_url, "CAIRN_CATALOG_URI": b.catalog_uri,
                       "CAIRN_LAKEHOUSE_CREDENTIAL": sess["credential"], "CAIRN_LAKEHOUSE_TOKEN": sess["token"],
                       "CAIRN_SPARK_APP": json.dumps(spec), "CAIRN_SPARK_CONF": json.dumps(s.get("conf") or {})})
    except JobError as e:
        raise StepFailed(str(e))
    return _job_result(ctx, status, output, log, error)


# ---- Airflow --------------------------------------------------------------------------------------------
def airflow_from_env() -> dict:
    """Airflow's REST API (CAIRN_AIRFLOW_URL; CAIRN_AIRFLOW_API v2 (Airflow 3, default) or v1 (Airflow 2); a bearer
    token in CAIRN_AIRFLOW_TOKEN_FILE, or CAIRN_AIRFLOW_USERNAME with CAIRN_AIRFLOW_PASSWORD_FILE; CAIRN_AIRFLOW_CA_FILE)."""
    return {"url": os.environ.get("CAIRN_AIRFLOW_URL", "").rstrip("/"), "api": os.environ.get("CAIRN_AIRFLOW_API", "v2"),
            "token_file": os.environ.get("CAIRN_AIRFLOW_TOKEN_FILE", ""),
            "username": os.environ.get("CAIRN_AIRFLOW_USERNAME", ""),
            "password_file": os.environ.get("CAIRN_AIRFLOW_PASSWORD_FILE", ""),
            "ca_file": os.environ.get("CAIRN_AIRFLOW_CA_FILE", "")}


class Airflow:
    def __init__(self, cfg: dict):
        if not cfg.get("url"):
            raise StepFailed("Airflow is not configured: set CAIRN_AIRFLOW_URL (and its credentials) to run DAGs from flows")
        if cfg.get("api") not in ("v1", "v2"):
            raise StepFailed("CAIRN_AIRFLOW_API is v1 (Airflow 2) or v2 (Airflow 3)")
        self.cfg = cfg
        self.verify: Any = cfg.get("ca_file") or True

    def _headers(self) -> dict:
        import httpx

        c = self.cfg
        if c.get("token"):                                  # a managed deployment: a short-lived token from Cairn
            return {"Authorization": "Bearer " + c["token"]}
        if c.get("token_file"):
            return {"Authorization": "Bearer " + open(c["token_file"]).read().strip()}
        if c.get("username") and c.get("password_file"):
            password = open(c["password_file"]).read().strip()
            if c["api"] == "v1":
                import base64

                return {"Authorization": "Basic " + base64.b64encode(f"{c['username']}:{password}".encode()).decode()}
            r = httpx.post(f"{c['url']}/auth/token", json={"username": c["username"], "password": password},
                           verify=self.verify, timeout=30)
            if r.status_code not in (200, 201):
                raise StepFailed(f"Airflow refused Cairn's sign-in ({r.status_code})")
            return {"Authorization": "Bearer " + r.json()["access_token"]}
        raise StepFailed("Airflow has no credentials: set CAIRN_AIRFLOW_TOKEN_FILE, or CAIRN_AIRFLOW_USERNAME and "
                         "CAIRN_AIRFLOW_PASSWORD_FILE")

    def call(self, method: str, path: str, body: Any = None) -> dict:
        import httpx

        try:
            # JSON asked for explicitly: Airflow 2 answers some calls (a DAG's source) in plain text otherwise.
            r = httpx.request(method, f"{self.cfg['url']}/api/{self.cfg['api']}{path}", json=body,
                              headers={**self._headers(), "Accept": "application/json"}, verify=self.verify, timeout=60)
        except httpx.HTTPError as e:
            raise StepFailed(f"Airflow could not be reached: {type(e).__name__}")
        if r.status_code >= 300:
            try:
                why = r.json().get("detail") or r.json().get("title") or r.text
            except ValueError:
                why = r.text
            raise StepFailed(f"Airflow said {r.status_code}: {str(why)[:300]}")
        try:
            return r.json() if r.content else {}
        except ValueError:
            raise StepFailed(f"Airflow answered {method} {path.split('?')[0]} with something other than JSON")


def _airflow(b: Backends, ctx: Ctx) -> Any:
    from urllib.parse import quote

    s = ctx.step
    if s.get("deployment"):                         # a managed deployment, woken if it hibernates
        from cairn.airflow_deploy.deployments import DeploymentError

        try:
            af = Airflow(need(b.airflow_deployments, "managed Airflow").flow_airflow(ctx.owner, s["deployment"]))
        except DeploymentError as e:
            raise StepFailed(str(e))
    else:
        af = Airflow(b.airflow or {})
    dag = quote(s["dag_id"], safe="")
    run_id = ctx.state.get("dag_run_id")
    if run_id is None:
        run_id = f"cairn-{ctx.flow}-{ctx.run_id[:12]}-{s['name']}-{ctx.attempt}"[:250]
        body: dict[str, Any] = {"dag_run_id": run_id,
                                "conf": {**(s.get("conf") or {}), "cairn_flow": ctx.flow, "cairn_run": ctx.run_id,
                                         "cairn_user": ctx.owner}}
        if af.cfg["api"] == "v2":
            body["logical_date"] = None
        try:
            af.call("POST", f"/dags/{dag}/dagRuns", body)
        except StepFailed as e:
            if "said 409" not in str(e):             # 409: this attempt's run exists (Cairn restarted after asking)
                raise
        ctx.log(f"started DAG run {run_id} of {s['dag_id']}")
        if not s.get("wait", True):
            return Done({"dag_id": s["dag_id"], "dag_run_id": run_id, "state": "queued"})
    state = af.call("GET", f"/dags/{dag}/dagRuns/{quote(run_id, safe='')}").get("state")
    if state == "success":
        return Done({"dag_id": s["dag_id"], "dag_run_id": run_id, "state": state})
    if state == "failed":
        raise StepFailed(f"the DAG run {run_id} of {s['dag_id']} failed")
    return Wait(int(time.time() * 1000) + s.get("poll_s", 30) * 1000, {"dag_run_id": run_id, "state": state})


# ---- AI -------------------------------------------------------------------------------------------------
def _agent(b: Backends, ctx: Ctx) -> Done:
    from cairn.ai.agent_apps import AgentAppError

    apps = need(b.agent_apps, "agent apps")
    text = ctx.step["input"] if isinstance(ctx.step["input"], str) else json.dumps(ctx.step["input"], default=str)
    try:
        out = apps.invoke(ctx.owner, ctx.step["app"], {"input": text[:100_000],
                                                      "session_id": f"flow-{ctx.run_id[:16]}-{ctx.step['name'][:30]}"})
    except AgentAppError as e:
        raise StepFailed(str(e))
    answer = str(out.get("output") or "")
    ctx.log(f"{ctx.step['app']} answered ({len(answer)} characters)")
    result: dict[str, Any] = {"output": answer[:20_000], "session_id": out.get("session_id")}
    if answer.strip().startswith("{"):
        try:
            result["result"] = json.loads(answer)
        except ValueError:
            pass
    return Done(result)


# ---- control --------------------------------------------------------------------------------------------
def _approval(b: Backends, ctx: Ctx) -> Any:
    s = ctx.step
    now = int(time.time() * 1000)
    if ctx.decision:
        d = ctx.decision
        if d.get("decision") == "approve":
            return Done({"approved": True, "by": d.get("by"), "comment": d.get("comment") or ""})
        raise StepFailed(f"rejected by {d.get('by')}" + (f": {d['comment']}" if d.get("comment") else ""))
    if not ctx.state.get("asked"):
        who = [*s.get("approvers", []), *[f"urn:li:corpGroup:{g}" for g in s.get("groups", [])]]
        if b.notify is not None:
            try:
                b.notify("flow.approval_requested", f"Approval needed: {ctx.flow} / {s['name']}",
                         s.get("message") or f"The flow {ctx.flow} waits for your approval to go on.",
                         list(s.get("approvers", [])),
                         {"flow": ctx.flow, "run": ctx.run_id, "step": s["name"], "groups": s.get("groups", [])})
            except Exception:  # noqa: BLE001 - the approval still waits; people can find it in the run
                logger.exception("flow %s: could not ask for approval", ctx.flow)
        ctx.log(f"waiting for {', '.join(who)}")
    if now >= ctx.deadline_ms:
        raise StepFailed(f"no decision within {s['timeout_s'] // 60} minutes")
    return Wait(min(ctx.deadline_ms, now + 60_000), {"asked": True})


def _until_ms(until: str, tz: str, now_ms: int) -> int:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    now = datetime.fromtimestamp(now_ms / 1000, tz=zone)
    u = until.strip()
    if len(u) <= 5 and ":" in u:
        try:
            hh, mm = (int(x) for x in u.split(":"))
            at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        except ValueError:
            raise StepFailed(f"until: {until!r} is HH:MM or a date and time")
        if at <= now:
            at += timedelta(days=1)
        return int(at.timestamp() * 1000)
    try:
        at = datetime.fromisoformat(u)
    except ValueError:
        raise StepFailed(f"until: {until!r} is HH:MM or a date and time (ISO 8601)")
    if at.tzinfo is None:
        at = at.replace(tzinfo=zone)
    return int(at.timestamp() * 1000)


def _wait(b: Backends, ctx: Ctx) -> Any:
    now = int(time.time() * 1000)
    target = ctx.state.get("until_ms")
    if target is None:
        s = ctx.step
        target = now + int(s["seconds"]) * 1000 if s.get("seconds") is not None else _until_ms(str(s["until"]), ctx.timezone, now)
        ctx.log(f"waiting until {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(target / 1000))} UTC")
    if now >= target:
        return Done({"waited_until_ms": target})
    return Wait(target, {"until_ms": target})


def _branch(b: Backends, ctx: Ctx) -> Done:
    try:
        result = bool(expr.evaluate(ctx.step["condition"], ctx.values))
    except expr.ExprError as e:
        raise StepFailed(f"condition: {e}")
    ctx.log(f"{ctx.step['condition']} is {result}")
    return Done({"result": result, "chosen": "then" if result else "else"})


def _notify(b: Backends, ctx: Ctx) -> Done:
    s = ctx.step
    to = list(s.get("to") or []) or [ctx.owner]
    if b.notify is None:
        raise StepFailed("notifications are not set up in this Cairn")
    text = s["message"] if isinstance(s["message"], str) else json.dumps(s["message"], default=str)
    title = s.get("title") or f"{ctx.flow}: {s['name']}"
    b.notify("flow.notice", str(title)[:300], text[:10_000], to, {"flow": ctx.flow, "run": ctx.run_id, "step": s["name"]})
    return Done({"sent_to": to})


EXECUTORS: dict[str, Callable[[Backends, Ctx], Any]] = {
    "sql": _sql, "check": _check, "derived": _derived, "pipe": _pipe, "stream": _stream, "notebook": _code,
    "python": _code, "spark": _spark, "dbt": _dbt, "airflow": _airflow, "agent": _agent, "approval": _approval,
    "wait": _wait, "branch": _branch, "notify": _notify,
}
# Steps that look again later rather than hold a worker while they wait.
WAITING: tuple = ("approval", "wait", "airflow", "stream")

# Steps that drive other platforms (any REST API, Databricks, Snowflake, BigQuery): cairn.flows.platforms adds its
# executors to EXECUTORS and its types to WAITING when it loads.
from cairn.flows import platforms as _platforms  # noqa: E402,F401  (it builds on the classes above)
