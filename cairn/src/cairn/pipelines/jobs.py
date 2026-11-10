"""Scheduled notebook and Python jobs: run as their owner, in a container of their own, their output kept.

``POST /v1/jobs`` with ``{"name", "kind": "notebook" | "python", "notebook": {...ipynb...} | "code": "...",
"params": {...}, "cron": "0 2 * * *", "timeout_minutes": 60, "recipients": [people], "notify": "failure" |
"always"}``. Defining or running a job needs ``RUN_JOBS``; only its owner (or an operator) changes it.

* **As the owner.** Each run gets a Trino token for the job's owner, valid for the run's time limit, and the
  owner's name (``CAIRN_USER``, ``CAIRN_TRINO_TOKEN``, ``CAIRN_TRINO_URL``, and ``CAIRN_TRINO_CA_FILE`` when
  Trino's certificate is private): what the job reads, it reads as them,
  under their policies, masks and row filters. The token reaches the container through an env file, never a
  command line. Parameters arrive as ``PARAMS`` (a notebook gets a first cell that sets it; a script reads
  ``CAIRN_JOB_PARAMS``).
* **Isolated.** Never in Cairn's own process: ``CAIRN_JOBS_RUNTIME=docker`` runs each run in a new container
  (non-root, no capabilities, no new privileges, memory, CPU and process limits, the code mounted read-only)
  and ``kubernetes`` as a Job in ``CAIRN_JOBS_K8S_NAMESPACE``. Without a runtime, jobs are refused (503).
* **Kept.** Each run records its status, times, the executed notebook (or the script's output) and its log, up to
  ``CAIRN_JOBS_MAX_OUTPUT_BYTES`` (2 MB); runs are kept ``CAIRN_JOBS_KEEP_DAYS`` (30) days.
* **Told.** A failed run (or every run, with ``notify: always``) tells the owner and recipients
  (``job.failed``, ``job.finished``). Runs are claimed, so one scheduler starts each. Audited.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, and_, delete, insert, select, update

from cairn.common.db import JSONText, metadata

logger = logging.getLogger(__name__)

PRIVILEGE = "RUN_JOBS"
NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
MARKER = "=====CAIRN-JOB-OUTPUT====="
IMAGE = "cairn/jobs:latest"
MAX_MINUTES = 690          # the owner's Trino token (at most 12 hours) outlives the longest run

jobs = Table(
    "cairn_jobs", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(64), nullable=False, unique=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("kind", String(10), nullable=False),
    Column("source", Text, nullable=False),
    Column("params", JSONText, nullable=False),
    Column("cron", String(100), nullable=True),
    Column("timeout_minutes", Integer, nullable=False),
    Column("recipients", JSONText, nullable=False),
    Column("notify", String(10), nullable=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("next_run_ms", BigInteger, nullable=True, index=True),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)
job_runs = Table(
    "cairn_job_runs", metadata,
    Column("id", String(32), primary_key=True),
    Column("job_id", String(32), nullable=False, index=True),
    Column("trigger", String(16), nullable=False),           # schedule | manual
    Column("as_user", String(256), nullable=False),
    Column("status", String(10), nullable=False),            # running | succeeded | failed (lost, briefly)
    Column("started_ms", BigInteger, nullable=False, index=True),
    Column("finished_ms", BigInteger, nullable=True),
    Column("output", Text, nullable=True),
    Column("log", Text, nullable=True),
    Column("error", Text, nullable=True),
)


class JobError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def _next(cron: Optional[str], after_ms: int) -> Optional[int]:
    if not cron:
        return None
    from datetime import datetime, timezone

    from croniter import croniter

    return int(croniter(cron, datetime.fromtimestamp(after_ms / 1000, tz=timezone.utc)).get_next(float) * 1000)


def with_params(notebook: dict, params: dict) -> dict:
    """The notebook with a first cell that sets ``PARAMS`` (a JSON literal, parsed, never executed as code)."""
    cell = {"cell_type": "code", "execution_count": None, "metadata": {"tags": ["injected-parameters"]}, "outputs": [],
            "source": f"import json\nPARAMS = json.loads({json.dumps(json.dumps(params))})\n"}
    nb = dict(notebook)
    nb["cells"] = [cell, *(notebook.get("cells") or [])]
    return nb


def script(kind: str, timeout_s: int) -> str:
    """What the container runs: the job, its log, then the marker and the executed notebook (or the output)."""
    if kind == "notebook":
        run = (f"jupyter nbconvert --to notebook --execute /work/job.ipynb --output-dir /tmp --output out.ipynb "
               f"--ExecutePreprocessor.timeout={timeout_s} >/tmp/log 2>&1")
        out = "/tmp/out.ipynb"
    else:
        # python3 where an image has no plain python (the Spark image); a virtualenv's first on PATH either way.
        run = '"$(command -v python3 || command -v python)" -u /work/job.py >/tmp/out.txt 2>/tmp/log'
        out = "/tmp/out.txt"
    return f"{run}; rc=$?; cat /tmp/log; echo '{MARKER}'; cat {out} 2>/dev/null; exit $rc"


def split_output(text: str) -> tuple[str, str]:
    """(log, output) from what the container printed."""
    if MARKER not in text:
        return text, ""
    log, _, out = text.partition(MARKER)
    return log.strip(), out.lstrip("\n")


# --------------------------------------------------------------------------- runtimes
class DockerRunner:
    """A new container per run: non-root, no capabilities, no new privileges, limited memory, CPU and processes."""

    def __init__(self, image: str = IMAGE, network: str = "", memory: str = "2g", cpus: str = "2", docker: str = "docker"):
        self.image, self.network, self.memory, self.cpus, self.docker = image, network, memory, cpus, docker
        self.uid = 1000

    def run(self, run_id: str, kind: str, files: dict[str, str], env: dict[str, str], timeout_s: int) -> tuple[int, str]:
        work = tempfile.mkdtemp(prefix=f"cairn-job-{run_id}-")       # the code, mounted read-only at /work
        secret = tempfile.mkdtemp(prefix=f"cairn-job-env-{run_id}-")  # the env file, never mounted
        try:
            os.chmod(work, 0o755)
            for fname, content in files.items():
                with open(os.path.join(work, fname), "w") as fh:
                    fh.write(content)
                os.chmod(os.path.join(work, fname), 0o644)
            env_file = os.path.join(secret, "env")
            fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:          # secrets through a file docker reads, never the command line
                for k, v in env.items():
                    if "\n" in v:
                        raise JobError(f"{k}: a value on one line")
                    fh.write(f"{k}={v}\n")
            name = f"cairn-job-{run_id}"
            cmd = [self.docker, "run", "--rm", "--name", name, "--env-file", env_file,
                   "--user", f"{self.uid}:{self.uid}", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "--memory", self.memory, "--cpus", self.cpus, "--pids-limit", "512",
                   # /tmp may hold programs (exec): the JVM's native libraries (Spark's zstd, snappy) load from it,
                   # and Docker's default (noexec) fails them. No boundary is lost: the job runs code it chose already.
                   "--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m",
                   "--tmpfs", f"/home/job:rw,nosuid,nodev,size=256m,uid={self.uid},gid={self.uid},mode=0700",
                   "-v", f"{work}:/work:ro", *(["--network", self.network] if self.network else []),
                   "--entrypoint", "sh", self.image, "-c", script(kind, timeout_s)]
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 60)
            except subprocess.TimeoutExpired:
                subprocess.run([self.docker, "rm", "-f", name], capture_output=True, timeout=60)
                return 124, f"the run went over its time limit ({timeout_s // 60} minutes) and was stopped"
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        finally:
            shutil.rmtree(work, ignore_errors=True)
            shutil.rmtree(secret, ignore_errors=True)


class KubeRunner:
    """A Kubernetes Job per run: the code in a ConfigMap, the token in a Secret, deleted after the run."""

    SA = "/var/run/secrets/kubernetes.io/serviceaccount"

    def __init__(self, namespace: str, image: str = IMAGE, api: str = "", token_file: str = "", ca_file: str = "",
                 resources: Optional[dict] = None, poll: float = 2.0):
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.api = (api or (f"https://{host}:{port}" if host else "")).rstrip("/")
        if not self.api:
            raise JobError("jobs on Kubernetes: no API server (not in a cluster, and no CAIRN_JOBS_K8S_API)", 503)
        self.namespace, self.image, self.poll = namespace, image, poll
        self.token_file = token_file or f"{self.SA}/token"
        self.verify: Any = ca_file or (f"{self.SA}/ca.crt" if os.path.exists(f"{self.SA}/ca.crt") else True)
        self.resources = resources or {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"memory": "2Gi"}}
        self.uid = 1000
        self.pod_labels: dict[str, str] = {}      # egress: the workspace's NetworkPolicy selects pods by these

    def _call(self, method: str, path: str, body: Any = None, ok404: bool = False, raw: bool = False) -> Any:
        import httpx

        token = open(self.token_file).read().strip()
        with httpx.Client(base_url=self.api, verify=self.verify, timeout=30, headers={"Authorization": f"Bearer {token}"}) as c:
            r = c.request(method, path, json=body)
        if r.status_code == 404 and ok404:
            return None
        if r.status_code >= 300:
            raise JobError(f"kubernetes: {method} {path}: {r.status_code} {r.text[:300]}", 502)
        return r.text if raw else (r.json() if r.content else {})

    def run(self, run_id: str, kind: str, files: dict[str, str], env: dict[str, str], timeout_s: int) -> tuple[int, str]:
        ns, name = self.namespace, f"cairn-job-{run_id[:12]}"
        labels = {"app.kubernetes.io/part-of": "cairn", "app.kubernetes.io/component": "job", "cairn.io/run": run_id[:12]}
        base = f"/api/v1/namespaces/{ns}"
        try:
            self._call("POST", f"{base}/configmaps", {"apiVersion": "v1", "kind": "ConfigMap",
                                                       "metadata": {"name": name, "labels": labels}, "data": files})
            self._call("POST", f"{base}/secrets", {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                                                    "metadata": {"name": name, "labels": labels}, "stringData": env})
            pod = {"restartPolicy": "Never", "automountServiceAccountToken": False, "enableServiceLinks": False,
                   "securityContext": {"runAsNonRoot": True, "runAsUser": self.uid, "runAsGroup": self.uid,
                                       "fsGroup": self.uid,
                                       "seccompProfile": {"type": "RuntimeDefault"}},
                   "containers": [{"name": "job", "image": self.image, "command": ["sh", "-c", script(kind, timeout_s)],
                                   "envFrom": [{"secretRef": {"name": name}}], "resources": self.resources,
                                   "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                       "capabilities": {"drop": ["ALL"]}},
                                   "volumeMounts": [{"name": "code", "mountPath": "/work", "readOnly": True},
                                                    {"name": "tmp", "mountPath": "/tmp"},
                                                    {"name": "home", "mountPath": "/home/job"}]}],
                   "volumes": [{"name": "code", "configMap": {"name": name}},
                               {"name": "tmp", "emptyDir": {"sizeLimit": "512Mi"}},
                               {"name": "home", "emptyDir": {"sizeLimit": "256Mi"}}]}
            self._call("POST", f"/apis/batch/v1/namespaces/{ns}/jobs", {
                "apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "labels": labels},
                "spec": {"backoffLimit": 0, "activeDeadlineSeconds": timeout_s + 30, "ttlSecondsAfterFinished": 600,
                         "template": {"metadata": {"labels": {**labels, **self.pod_labels}}, "spec": pod}}})
            end = time.time() + timeout_s + 300
            while True:
                job = self._call("GET", f"/apis/batch/v1/namespaces/{ns}/jobs/{name}")
                st = job.get("status") or {}
                if st.get("succeeded") or st.get("failed"):
                    break
                if time.time() > end:
                    return 124, "the run went over its time limit and was stopped"
                time.sleep(self.poll)
            pods = self._call("GET", f"{base}/pods?labelSelector=cairn.io/run={run_id[:12]}").get("items") or []
            text = self._call("GET", f"{base}/pods/{pods[0]['metadata']['name']}/log", raw=True) if pods else ""
            if st.get("failed"):
                reason = ((st.get("conditions") or [{}])[-1] or {}).get("reason", "")
                return (124 if reason == "DeadlineExceeded" else 1), text or f"the job failed ({reason})"
            return 0, text
        finally:
            for path in (f"/apis/batch/v1/namespaces/{ns}/jobs/{name}?propagationPolicy=Background",
                         f"{base}/configmaps/{name}", f"{base}/secrets/{name}"):
                try:
                    self._call("DELETE", path, ok404=True)
                except Exception:  # noqa: BLE001 - left behind is cleaned by the Job's TTL; the secret's token expires
                    logger.warning("could not delete %s", path)


def with_image(runner: Any, image: str, uid: Optional[int] = None) -> Any:
    """The same runtime (Docker or Kubernetes, same limits) with another image: a flow's dbt or Spark step; ``uid``:
    the image's own non-root user, for an image whose tools need a named user (Spark's Hadoop)."""
    import copy

    other = copy.copy(runner)
    other.image = image
    if uid is not None:
        if int(uid) <= 0:
            raise JobError("jobs never run as root", 400)
        other.uid = int(uid)
    return other


def runner_from_env() -> Optional[Any]:
    rt = os.environ.get("CAIRN_JOBS_RUNTIME", "")
    image = os.environ.get("CAIRN_JOBS_IMAGE", IMAGE)
    if rt == "docker":
        return DockerRunner(image, os.environ.get("CAIRN_JOBS_DOCKER_NETWORK", ""),
                            os.environ.get("CAIRN_JOBS_MEMORY", "2g"), os.environ.get("CAIRN_JOBS_CPUS", "2"))
    if rt == "kubernetes":
        res = os.environ.get("CAIRN_JOBS_K8S_RESOURCES", "")
        return KubeRunner(os.environ.get("CAIRN_JOBS_K8S_NAMESPACE", "cairn-jobs"), image, os.environ.get("CAIRN_JOBS_K8S_API", ""),
                          os.environ.get("CAIRN_JOBS_K8S_TOKEN_FILE", ""), os.environ.get("CAIRN_JOBS_K8S_CA_FILE", ""),
                          json.loads(res) if res else None)
    if rt:
        raise ValueError("CAIRN_JOBS_RUNTIME: docker or kubernetes")
    return None


# --------------------------------------------------------------------------- the service
class Jobs:
    def __init__(self, engine, runner, allowed: Callable[[str], bool], is_operator: Callable[[str], bool],
                 mint_token: Optional[Callable[[str, int], Optional[str]]] = None, trino_url: str = "",
                 notify: Optional[Callable[[str, str, str, list[str], dict], Any]] = None, audit=None,
                 trino_ca_file: str = ""):
        """``allowed(actor)``: may define and run jobs (RUN_JOBS); ``mint_token(owner, seconds)``: a Trino token for
        the owner; ``runner``: DockerRunner, KubeRunner or None (jobs refused)."""
        self.engine, self.runner, self.allowed, self.is_operator = engine, runner, allowed, is_operator
        self.mint_token, self.trino_url, self.notify, self.audit = mint_token, trino_url, notify, audit
        self.trino_ca = open(trino_ca_file).read() if trino_ca_file else ""   # Trino's CA, for a private certificate
        self.egress = None            # cairn.network.egress.Egress: the owner's workspace's egress rules, when enforced
        # Called with (job, run id, status, started_ms, finished_ms, error) when a run ends (L145).
        self.observers: list = []
        self.max_output = int(os.environ.get("CAIRN_JOBS_MAX_OUTPUT_BYTES", str(2 << 20)))
        self.keep_days = int(os.environ.get("CAIRN_JOBS_KEEP_DAYS", "30"))
        # Runs happen beside the scheduler's tick and the API's request, a few at a time.
        self.pool = ThreadPoolExecutor(max_workers=int(os.environ.get("CAIRN_JOBS_CONCURRENCY", "4")),
                                       thread_name_prefix="cairn-job")

    def _need(self, actor: str) -> None:
        if not self.allowed(actor):
            raise JobError("defining and running jobs needs RUN_JOBS", 403)
        if self.runner is None:
            raise JobError("jobs are not configured: set CAIRN_JOBS_RUNTIME (docker or kubernetes)", 503)

    def _check(self, body: dict) -> dict:
        if not isinstance(body, dict):
            raise JobError("a job is an object")
        kind = body.get("kind")
        if kind not in ("notebook", "python"):
            raise JobError("kind: notebook or python")
        if kind == "notebook":
            nb = body.get("notebook")
            if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list) or "nbformat" not in nb:
                raise JobError("notebook: the notebook's JSON (nbformat 4)")
            source = json.dumps(nb)
        else:
            if not isinstance(body.get("code"), str) or not body["code"].strip():
                raise JobError("code: the Python script")
            source = body["code"]
        if len(source) > 5 << 20:
            raise JobError("the notebook or script is over 5 MB")
        cron = body.get("cron") or None
        if cron is not None:
            from croniter import croniter

            if not isinstance(cron, str) or not croniter.is_valid(cron):
                raise JobError("cron: a cron expression (UTC), such as 0 2 * * *")
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise JobError("params: an object")
        timeout = body.get("timeout_minutes", 60)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_MINUTES:
            raise JobError(f"timeout_minutes: 1 to {MAX_MINUTES}")
        recipients = body.get("recipients") or []
        if not isinstance(recipients, list) or not all(isinstance(r, str) and r.startswith("urn:li:corpuser:") for r in recipients):
            raise JobError("recipients: urn:li:corpuser:… people")
        if body.get("notify", "failure") not in ("failure", "always"):
            raise JobError("notify: failure or always")
        return {"kind": kind, "source": source, "params": params, "cron": cron, "timeout_minutes": timeout,
                "recipients": sorted(set(recipients)), "notify": body.get("notify", "failure"),
                "enabled": bool(body.get("enabled", True))}

    def save(self, actor: str, body: dict, name: Optional[str] = None) -> dict:
        self._need(actor)
        values = self._check(body)
        now = _now()
        with self.engine.begin() as conn:
            if name is None:
                name = str(body.get("name") or "")
                if not NAME.match(name):
                    raise JobError("name: lowercase letters, digits and -")
                if conn.execute(select(jobs.c.id).where(jobs.c.name == name)).first():
                    raise JobError(f"a job {name} exists", 409)
                conn.execute(insert(jobs).values(id=uuid.uuid4().hex, name=name, owner=actor, created_ms=now, updated_ms=now,
                                                 next_run_ms=_next(values["cron"], now), **values))
            else:
                job = self._get(name)
                if job["owner"] != actor and not self.is_operator(actor):
                    raise JobError("only its owner or an operator changes a job", 403)
                conn.execute(update(jobs).where(jobs.c.name == name).values(updated_ms=now,
                                                                            next_run_ms=_next(values["cron"], now), **values))
        self._audit(actor, "job.saved", name, {"kind": values["kind"], "cron": values["cron"]})
        return self.get(actor, name)

    def _get(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(jobs).where(jobs.c.name == name)).first()
        if r is None:
            raise JobError(f"no job {name}", 404)
        return dict(r._mapping)

    def get(self, actor: str, name: str, source: bool = False) -> dict:
        """A job; with ``source``, its notebook or script as it was saved (``code`` or ``notebook``), so it can
        be kept in git (``cairn export``, Terraform)."""
        j = self._get(name)
        if j["owner"] != actor and not self.is_operator(actor):
            raise JobError(f"no job {name}", 404)
        out = {k: v for k, v in j.items() if k != "source"} | {"source_bytes": len(j["source"])}
        if source:
            out |= {"code": j["source"]} if j["kind"] == "python" else {"notebook": json.loads(j["source"])}
        return out

    def list(self, actor: str, source: bool = False) -> list[dict]:
        with self.engine.connect() as conn:
            q = select(jobs.c.name)
            if not self.is_operator(actor):
                q = q.where(jobs.c.owner == actor)
            names = [r[0] for r in conn.execute(q.order_by(jobs.c.name))]
        return [self.get(actor, n, source) for n in names]

    def delete(self, actor: str, name: str) -> None:
        j = self._get(name)
        if j["owner"] != actor and not self.is_operator(actor):
            raise JobError("only its owner or an operator deletes a job", 403)
        with self.engine.begin() as conn:
            conn.execute(delete(job_runs).where(job_runs.c.job_id == j["id"]))
            conn.execute(delete(jobs).where(jobs.c.id == j["id"]))
        self._audit(actor, "job.deleted", name, {})

    def runs(self, actor: str, name: str, limit: int = 50) -> list[dict]:
        j = self.get(actor, name)
        with self.engine.connect() as conn:
            rows = conn.execute(select(job_runs.c.id, job_runs.c.trigger, job_runs.c.as_user, job_runs.c.status,
                                       job_runs.c.started_ms, job_runs.c.finished_ms, job_runs.c.error)
                                .where(job_runs.c.job_id == j["id"]).order_by(job_runs.c.started_ms.desc()).limit(limit))
            return [dict(r._mapping) for r in rows]

    def run_detail(self, actor: str, name: str, run_id: str) -> dict:
        j = self.get(actor, name)
        with self.engine.connect() as conn:
            r = conn.execute(select(job_runs).where(and_(job_runs.c.id == run_id, job_runs.c.job_id == j["id"]))).first()
        if r is None:
            raise JobError("no such run", 404)
        return dict(r._mapping)

    # ---- running -----------------------------------------------------------------------------------
    def run_now(self, actor: str, name: str, wait: bool = False) -> dict:
        """Starts a run; with ``wait``, returns when it has finished."""
        self._need(actor)
        j = self._get(name)
        if j["owner"] != actor and not self.is_operator(actor):
            raise JobError("only its owner or an operator runs a job", 403)
        rid = self._start(j, "manual")
        fut = self.pool.submit(self._execute, j, rid, "manual")
        return fut.result() if wait else {"run": rid, "status": "running", "error": None}

    def _start(self, j: dict, trigger: str) -> str:
        rid = uuid.uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(insert(job_runs).values(id=rid, job_id=j["id"], trigger=trigger, as_user=j["owner"],
                                                 status="running", started_ms=_now()))
        return rid

    def _execute(self, j: dict, rid: str, trigger: str) -> dict:
        status, output, log, error = "failed", None, None, None
        try:
            status, output, log, error = self.contained(j["owner"], j["kind"], j["source"], j["params"] or {},
                                                        j["timeout_minutes"] * 60, j["name"], rid)
        except Exception as e:  # noqa: BLE001 - recorded on the run and told, never raised into the scheduler
            error = str(e)[:1000]
        self._finish(j, rid, trigger, status, output, log, error)
        return {"run": rid, "status": status, "error": error}

    def contained(self, owner: str, kind: str, source: str, params: dict, timeout_s: int, label: str, rid: str,
                  runner=None, extra_env: Optional[dict[str, str]] = None) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
        """Runs code in a container of its own as ``owner`` (their Trino token): (status, output, log, error).
        ``runner``: another image's runner (a flow's dbt or Spark step); ``extra_env``: more settings, which go
        through the same env file as the token, never a command line. Raises JobError when it cannot run."""
        runner = runner or self.runner
        if runner is None:
            raise JobError("jobs are not configured: set CAIRN_JOBS_RUNTIME (docker or kubernetes)", 503)
        if not self.allowed(owner):                        # the owner may since have lost the right to run jobs
            raise JobError(f"{owner} may no longer run jobs (RUN_JOBS)", 403)
        env = {"CAIRN_USER": owner.removeprefix("urn:li:corpuser:"), "CAIRN_JOB": label, "CAIRN_JOB_RUN": rid,
               "CAIRN_JOB_PARAMS": json.dumps(params or {}), **(extra_env or {})}
        from cairn.observability.telemetry import current_traceparent

        if current_traceparent():                          # the run (and a Spark job it submits) joins this trace
            env["TRACEPARENT"] = current_traceparent()
        token = self.mint_token(owner, timeout_s + 300) if self.mint_token is not None else None
        if token:                                          # Trino configured: the run reads as its owner
            env["CAIRN_TRINO_TOKEN"] = token
            env["CAIRN_TRINO_URL"] = self.trino_url
        if self.egress is not None and self.egress.enforced:   # no rule, no way out but Cairn and the lakehouse
            from cairn.network.egress import EgressError

            workspace = self.egress.workspace_of(owner)
            try:
                env.update(self.egress.proxy_env(workspace, rid, timeout_s + 300))
                runner = self.egress.contain(runner, workspace)
            except EgressError as e:
                raise JobError(str(e), e.status)
        files = ({"job.ipynb": json.dumps(with_params(json.loads(source), params or {}))}
                 if kind == "notebook" else {"job.py": source})
        if token and self.trino_ca:
            files["trino-ca.pem"] = self.trino_ca
            env["CAIRN_TRINO_CA_FILE"] = "/work/trino-ca.pem"
        rc, text = runner.run(rid, kind, files, env, timeout_s)
        log, output = split_output(text)
        if rc == 0:
            return "succeeded", output, log, None
        error = ("it went over its time limit" if rc == 124 else f"it exited with status {rc}") + \
            (f": {log.strip().splitlines()[-1][:500]}" if log.strip() else "")
        return "failed", output, log, error

    def run_for_flow(self, owner: str, name: str, params: dict, timeout_s: Optional[int] = None) -> dict:
        """A defined job run as a step of ``owner``'s flow (``owner`` must own the job or be an operator), its
        parameters merged with the step's; recorded with the job's runs (trigger ``flow``)."""
        j = self._get(name)
        if j["owner"] != owner and not self.is_operator(owner):
            raise JobError(f"{name} is not the flow owner's job", 403)
        j = {**j, "params": {**(j["params"] or {}), **(params or {})}}
        if timeout_s:
            j["timeout_minutes"] = max(1, min(j["timeout_minutes"], -(-timeout_s // 60)))
        rid = self._start(j, "flow")
        out = self._execute(j, rid, "flow")
        with self.engine.connect() as conn:
            r = conn.execute(select(job_runs.c.output, job_runs.c.log).where(job_runs.c.id == rid)).first()
        return {**out, "output": r.output if r else None, "log": r.log if r else None}

    def _finish(self, j: dict, rid: str, trigger: str, status: str, output, log, error) -> None:
        owner, cap = j["owner"], self.max_output
        with self.engine.begin() as conn:
            conn.execute(update(job_runs).where(job_runs.c.id == rid).values(
                status=status, finished_ms=_now(), error=error,
                output=(output or "")[:cap] or None, log=(log or "")[-cap:] or None))
            conn.execute(delete(job_runs).where(and_(job_runs.c.job_id == j["id"],
                                                     job_runs.c.started_ms < _now() - self.keep_days * 86_400_000)))
        self._audit(owner, f"job.run_{status}", j["name"], {"run": rid, "trigger": trigger})
        from cairn.common import metrics

        metrics.RUNS.labels("job", status).inc()
        if self.observers:
            with self.engine.connect() as conn:
                t = conn.execute(select(job_runs.c.started_ms, job_runs.c.finished_ms).where(job_runs.c.id == rid)).first()
            for fn in list(self.observers):
                try:
                    fn(j, rid, status, t.started_ms if t else None, t.finished_ms if t else None, error)
                except Exception:  # noqa: BLE001 - observing a run never changes it
                    logger.exception("job run %s: an observer failed", rid)
        if status == "failed" or j["notify"] == "always":
            who = [owner, *[r for r in j["recipients"] if r != owner]]
            kind = "job.failed" if status == "failed" else "job.finished"
            title = f"Job {j['name']} {'failed' if status == 'failed' else 'finished'}"
            if self.notify is not None:
                try:
                    self.notify(kind, title, error or "The run succeeded.", who, {"job": j["name"], "run": rid})
                except Exception:  # noqa: BLE001 - the run is recorded either way
                    logger.exception("could not tell about job %s", j["name"])

    def _reap(self, now: int) -> None:
        """Runs still 'running' well past their time limit were lost (a scheduler or API restart): failed, told."""
        with self.engine.connect() as conn:
            rows = conn.execute(select(job_runs.c.id.label("run_id"), job_runs.c.trigger, jobs).join(jobs, jobs.c.id == job_runs.c.job_id)
                                .where(and_(job_runs.c.status == "running",
                                            job_runs.c.started_ms < now - (jobs.c.timeout_minutes * 60_000 + 900_000))))
            lost = [dict(r._mapping) for r in rows]
        for r in lost:
            with self.engine.begin() as conn:            # claim it: one process reports each
                n = conn.execute(update(job_runs).where(and_(job_runs.c.id == r["run_id"], job_runs.c.status == "running"))
                                 .values(status="lost")).rowcount
            if n:
                self._finish(r, r["run_id"], r["trigger"], "failed", None, None,
                             "the run was lost (Cairn restarted while it ran); it may not have finished")

    def run_due(self, now: Optional[int] = None, wait: bool = False) -> int:
        """Starts every enabled job whose time has come (the scheduler's leader calls this each tick). The runs go
        to the pool, so a long job never holds up the tick; ``wait`` waits for them (tests)."""
        now = now or _now()
        self._reap(now)
        with self.engine.connect() as conn:
            due = [dict(r._mapping) for r in conn.execute(select(jobs).where(and_(
                jobs.c.enabled, jobs.c.next_run_ms.is_not(None), jobs.c.next_run_ms <= now)))]
        started: list[Future] = []
        for j in due:
            with self.engine.begin() as conn:            # claim it: one scheduler starts each run
                n = conn.execute(update(jobs).where(and_(jobs.c.id == j["id"], jobs.c.next_run_ms == j["next_run_ms"]))
                                 .values(next_run_ms=_next(j["cron"], now))).rowcount
            if n and self.runner is not None:
                started.append(self.pool.submit(self._execute, j, self._start(j, "schedule"), "schedule"))
        if wait:
            for f in started:
                f.result()
        return len(started)

    def _audit(self, actor: str, action: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("pipelines", action, actor, f"job:{name}", details)


def build_jobs_router(j: Jobs, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/jobs", tags=["jobs"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except JobError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(source: bool = False, p=Depends(current_principal)):
        """The jobs (an operator: everyone's); ``?source=true`` adds each one's notebook or script."""
        return await call(lambda: j.list(p.urn, source))

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: j.save(p.urn, body))

    @router.get("/{name}")
    async def get(name: str, source: bool = False, p=Depends(current_principal)):
        return await call(lambda: j.get(p.urn, name, source))

    @router.put("/{name}")
    async def change(name: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: j.save(p.urn, body, name))

    @router.delete("/{name}", status_code=204)
    async def drop(name: str, p=Depends(current_principal)):
        await call(lambda: j.delete(p.urn, name))

    @router.post("/{name}/run", status_code=202)
    async def run(name: str, wait: bool = False, p=Depends(current_principal)):
        # Started and answered at once (poll /runs/{id}); ?wait=true answers when the run has finished.
        return await call(lambda: j.run_now(p.urn, name, wait))

    @router.get("/{name}/runs")
    async def runs(name: str, p=Depends(current_principal)):
        return await call(lambda: j.runs(p.urn, name))

    @router.get("/{name}/runs/{run_id}")
    async def run_detail(name: str, run_id: str, p=Depends(current_principal)):
        return await call(lambda: j.run_detail(p.urn, name, run_id))

    return router
