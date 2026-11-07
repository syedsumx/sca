"""Agent apps: code-first agents deployed to Cairn (``cairn agents deploy``), the counterpart of Agent Bricks' deploy.

``POST /v1/agent-apps`` with ``{"manifest": agent.toml as JSON, "bundle": base64 of the project as tar.gz}`` deploys
(or redeploys) an app; ``POST /v1/agent-apps/<name>/invocations`` with ``{"input", "session_id"}`` asks it.

* **An identity of its own.** Deploying registers the app in the agent hub (kind ``app``) with the manifest's scopes
  (``model``, ``query_data``, ``read``, ``propose``): the administrators' pause switch and quotas apply. It holds no
  key.
* **Run as the person asking.** Each invocation gets a short-lived token (15 minutes, revoked when the run ends) for
  the person who asked, narrowed to the app's scopes: the agent's model calls go through Cairn's AI gateway and its
  SQL through Cairn's query path as that person (their policies, masks, row filters, budget), metered as
  ``gateway:<agent id>``.
* **Who.** Deploying needs ``BUILD_AGENTS``; only an app's owner (or an operator) redeploys or deletes it. Invoking
  needs to be its owner or ``USE_AGENT`` on ``urn:li:cairnAgent:<name>``.
* **Sessions.** Cairn keeps each conversation (per app, person and session id, the last ``CAIRN_AGENT_APPS_HISTORY``
  messages, 40) and sends it with each run; a person reads only their own (``GET …/sessions/<id>``).
* **Runtime.** ``CAIRN_AGENT_APPS_RUNTIME=process``: the project runs in ``cairn.agentkit.runtime`` as a local process
  per app (on the API's host, its Python environment, which must have the project's requirements: a deploy that
  needs a missing package is refused and names it), reachable on 127.0.0.1 only with a secret Cairn gives it in a
  file. ``CAIRN_AGENT_APPS_CAIRN_URL`` is the address the runtime calls Cairn back on.
* **MLflow.** With ``CAIRN_MLFLOW_URL`` (Cairn's MLflow), each app has an experiment ``agents/<name>`` and each run is
  an MLflow run in it: ``mlflow.user`` the person who asked, the app version and session as tags, latency and the
  sizes of the question and answer as metrics, FINISHED or FAILED. Never the content.
* **Audited:** ``agent_app.deployed``, ``agent_app.invoked`` (who, session, time; no content), ``agent_app.deleted``.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, String, Table, Text, delete, insert, select, update

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

agent_apps = Table(
    "cairn_agent_apps", metadata,
    Column("name", String(48), primary_key=True),
    Column("agent_id", String(32), nullable=False),
    Column("owner", String(512), nullable=False),
    Column("manifest", Text, nullable=False),
    Column("bundle", Text, nullable=False),               # base64 tar.gz
    Column("version", BigInteger, nullable=False),
    Column("status", String(16), nullable=False),         # running | failed
    Column("error", Text, nullable=True),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)
agent_app_sessions = Table(
    "cairn_agent_app_sessions", metadata,
    Column("app", String(48), primary_key=True),
    Column("actor", String(512), primary_key=True),
    Column("session_id", String(128), primary_key=True),
    Column("messages", Text, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

NAME = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
SESSION = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
APP_SCOPES = ("read", "query_data", "propose", "model")
MAX_BUNDLE = int(os.environ.get("CAIRN_AGENT_APPS_MAX_BUNDLE", str(5 * 1024 * 1024)))


class AgentAppError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def urn(name: str) -> str:
    return f"urn:li:cairnAgent:{name}"


def unpack(data: bytes, into: Path) -> None:
    """A project bundle, unpacked safely: regular files and folders inside ``into`` only."""
    into.mkdir(parents=True, exist_ok=True)
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except (tarfile.TarError, OSError) as e:
        raise AgentAppError(f"the bundle is not a gzipped tar: {e}")
    with tar:
        for m in tar.getmembers():
            target = (into / m.name).resolve()
            if not str(target).startswith(str(into.resolve()) + os.sep) or not (m.isfile() or m.isdir()):
                raise AgentAppError(f"the bundle has an entry outside the project or not a file: {m.name}")
        tar.extractall(into, filter="data")
    if not (into / "agent.py").is_file():
        raise AgentAppError("the bundle has no agent.py")


def missing_requirements(project: Path) -> list[str]:
    """Requirements (``requirements.txt``) not installed in this Python environment."""
    from importlib.metadata import PackageNotFoundError, distribution

    req = project / "requirements.txt"
    out = []
    for line in (req.read_text().splitlines() if req.is_file() else []):
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~;\[ ]", line, maxsplit=1)[0].strip()
        try:
            distribution(name)
        except PackageNotFoundError:
            out.append(name)
    return out


class ProcessRuntime:
    """Each app as a local ``cairn.agentkit.runtime`` process on 127.0.0.1, with a secret only Cairn knows."""

    def __init__(self, root: Path, cairn_url: str):
        self.root, self.cairn_url = root, cairn_url
        self.procs: dict[str, tuple[subprocess.Popen, int, str, int]] = {}    # name -> (proc, port, secret, version)
        self.lock = threading.Lock()

    def start(self, name: str, version: int, bundle: bytes) -> None:
        self.stop(name)
        d = self.root / name / str(version)
        if d.exists():
            shutil.rmtree(d)
        unpack(bundle, d / "project")
        missing = missing_requirements(d / "project")
        if missing:
            raise AgentAppError(f"the runtime lacks the project's requirements: {', '.join(missing)} (install them "
                                "in Cairn's environment, or remove them from requirements.txt)", 409)
        secret = secrets.token_urlsafe(32)
        sf = d / "secret"
        sf.write_text(secret)
        os.chmod(sf, 0o600)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CAIRN_", "AWS_", "ANTHROPIC", "OPENAI"))}
        env.update({"CAIRN_URL": self.cairn_url, "PYTHONDONTWRITEBYTECODE": "1"})
        log = open(d / "runtime.log", "ab")
        proc = subprocess.Popen([sys.executable, "-m", "cairn.agentkit.runtime", "--dir", str(d / "project"),
                                 "--mode", "app", "--port", str(port), "--secret-file", str(sf)],
                                env=env, stdout=log, stderr=log, cwd=str(d / "project"))
        import httpx

        end = time.time() + 60
        while time.time() < end:
            if proc.poll() is not None:
                tail = (d / "runtime.log").read_text(errors="replace")[-1500:]
                raise AgentAppError(f"the agent did not start: {tail}", 502)
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        else:
            proc.kill()
            raise AgentAppError("the agent did not start within 60 seconds", 504)
        with self.lock:
            self.procs[name] = (proc, port, secret, version)

    def endpoint(self, name: str, version: int) -> Optional[tuple[str, str]]:
        with self.lock:
            got = self.procs.get(name)
        if got is None or got[0].poll() is not None or got[3] != version:
            return None
        return f"http://127.0.0.1:{got[1]}", got[2]

    def stop(self, name: str) -> None:
        with self.lock:
            got = self.procs.pop(name, None)
        if got is not None and got[0].poll() is None:
            got[0].terminate()
            try:
                got[0].wait(10)
            except subprocess.TimeoutExpired:
                got[0].kill()


class MlflowRuns:
    """Each run of an agent app as an MLflow run (REST, Cairn's MLflow): best effort, never failing a run."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def _post(self, path: str, body: dict) -> dict:
        import httpx

        r = httpx.post(f"{self.url}/api/2.0/mlflow/{path}", json=body, timeout=15)
        r.raise_for_status()
        return r.json()

    def experiment(self, app: str) -> Optional[str]:
        import httpx

        name = f"agents/{app}"
        try:
            r = httpx.get(f"{self.url}/api/2.0/mlflow/experiments/get-by-name", params={"experiment_name": name},
                          timeout=15)
            if r.status_code == 200:
                return r.json()["experiment"]["experiment_id"]
            return self._post("experiments/create", {"name": name, "tags": [
                {"key": "cairn.agent_app", "value": app}]})["experiment_id"]
        except Exception as e:  # noqa: BLE001 - tracking is best effort
            logger.warning("MLflow experiment for agent app %s: %s", app, e)
            return None

    def record(self, experiment: str, app: str, version: int, actor: str, session: str, ok: bool, started_ms: int,
               metrics: dict) -> None:
        try:
            run = self._post("runs/create", {"experiment_id": experiment, "start_time": started_ms,
                                             "run_name": f"{app} v{version}", "tags": [
                                                 {"key": "mlflow.user", "value": actor},
                                                 {"key": "cairn.agent_app", "value": app},
                                                 {"key": "cairn.app_version", "value": str(version)},
                                                 {"key": "cairn.session", "value": session}]})["run"]["info"]["run_id"]
            now = _now()
            self._post("runs/log-batch", {"run_id": run, "metrics": [
                {"key": k, "value": float(v), "timestamp": now, "step": 0} for k, v in metrics.items()]})
            self._post("runs/update", {"run_id": run, "status": "FINISHED" if ok else "FAILED", "end_time": now})
        except Exception as e:  # noqa: BLE001 - tracking is best effort
            logger.warning("MLflow run for agent app %s: %s", app, e)


class AgentApps:
    def __init__(self, engine, agents, runtime: ProcessRuntime, authorize: Callable[[str, str, Optional[str]], bool],
                 is_operator: Callable[[str], bool], audit=None, mlflow: Optional[MlflowRuns] = None):
        self.engine, self.agents, self.runtime, self.authorize = engine, agents, runtime, authorize
        self.is_operator, self.audit, self.mlflow = is_operator, audit, mlflow
        self.history = int(os.environ.get("CAIRN_AGENT_APPS_HISTORY", "40"))

    def _row(self, name: str) -> dict:
        with self.engine.connect() as c:
            r = c.execute(select(agent_apps).where(agent_apps.c.name == name)).first()
        if r is None:
            raise AgentAppError("no such agent app", 404)
        d = dict(r._mapping)
        d["manifest"] = json.loads(d["manifest"])
        return d

    @staticmethod
    def _public(d: dict) -> dict:
        m = d["manifest"]
        return {"name": d["name"], "agent_id": d["agent_id"], "urn": urn(d["name"]), "owner": d["owner"],
                "description": m.get("description", ""), "template": m.get("template"), "model": m.get("model"),
                "scopes": m.get("scopes", []), "version": d["version"], "status": d["status"], "error": d["error"],
                "mlflow_experiment": m.get("_mlflow_experiment"), "updated_ms": d["updated_ms"]}

    def deploy(self, actor: str, body: dict) -> dict:
        if not self.authorize(actor, "BUILD_AGENTS", None):
            raise AgentAppError("deploying an agent needs BUILD_AGENTS", 403)
        m, raw = body.get("manifest"), body.get("bundle")
        if not isinstance(m, dict) or not isinstance(raw, str):
            raise AgentAppError("manifest (agent.toml) and bundle (base64 tar.gz)")
        name = str(m.get("name") or "")
        if not NAME.match(name):
            raise AgentAppError("name: lowercase letters, digits and -")
        scopes = m.get("scopes") or []
        if not isinstance(scopes, list) or not set(scopes) <= set(APP_SCOPES) or not scopes:
            raise AgentAppError(f"scopes: some of {', '.join(APP_SCOPES)}")
        try:
            data = base64.b64decode(raw, validate=True)
        except ValueError:
            raise AgentAppError("bundle: base64")
        if len(data) > MAX_BUNDLE:
            raise AgentAppError(f"the bundle is over {MAX_BUNDLE} bytes", 413)
        now = _now()
        with self.engine.connect() as c:
            cur = c.execute(select(agent_apps).where(agent_apps.c.name == name)).first()
        if cur is not None and cur.owner != actor and not self.is_operator(actor):
            raise AgentAppError(f"{name} is someone else's agent app", 403)
        if cur is None:
            agent = self.agents.create_app_agent(name, actor, [s for s in APP_SCOPES if s in scopes],
                                                 str(m.get("description") or ""), actor)
            agent_id, version = agent["id"], 1
        else:
            agent_id, version = cur.agent_id, cur.version + 1
            self.agents.set_scopes(agent_id, [s for s in APP_SCOPES if s in scopes], None, actor)
        status, error = "running", None
        try:
            self.runtime.start(name, version, data)
        except AgentAppError as e:
            if cur is None:                              # a first deploy that cannot run leaves nothing behind
                self.agents.delete(agent_id, actor)
                raise
            status, error = "failed", str(e)[:2000]
        if self.mlflow is not None:
            m = {**m, "_mlflow_experiment": self.mlflow.experiment(name)}
        values = {"agent_id": agent_id, "manifest": json.dumps(m), "bundle": raw, "version": version, "status": status,
                  "error": error, "updated_ms": now}
        with self.engine.begin() as c:
            if cur is None:
                c.execute(insert(agent_apps).values(name=name, owner=actor, created_ms=now, **values))
            else:
                c.execute(update(agent_apps).where(agent_apps.c.name == name).values(**values))
        self._audit("agent_app.deployed", actor, name, {"version": version, "status": status, "scopes": scopes,
                                                        "bytes": len(data)})
        if error:
            raise AgentAppError(f"version {version} did not start; the app stays down until a deploy works: {error}",
                                502)
        return self.get(actor, name)

    def _may_use(self, actor: str, d: dict) -> bool:
        return d["owner"] == actor or self.authorize(actor, "USE_AGENT", urn(d["name"]))

    def get(self, actor: str, name: str) -> dict:
        d = self._row(name)
        if not self._may_use(actor, d) and not self.is_operator(actor):
            raise AgentAppError("no such agent app", 404)
        return self._public(d)

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as c:
            names = [r.name for r in c.execute(select(agent_apps.c.name).order_by(agent_apps.c.name))]
        out = []
        for n in names:
            try:
                out.append(self.get(actor, n))
            except AgentAppError:
                continue
        return out

    def delete(self, actor: str, name: str) -> None:
        d = self._row(name)
        if d["owner"] != actor and not self.is_operator(actor):
            raise AgentAppError("only its owner or an operator deletes an agent app", 403)
        self.runtime.stop(name)
        self.agents.delete(d["agent_id"], actor)
        with self.engine.begin() as c:
            c.execute(delete(agent_app_sessions).where(agent_app_sessions.c.app == name))
            c.execute(delete(agent_apps).where(agent_apps.c.name == name))
        self._audit("agent_app.deleted", actor, name, {})

    def session(self, actor: str, name: str, session_id: str) -> list[dict]:
        self.get(actor, name)
        with self.engine.connect() as c:
            r = c.execute(select(agent_app_sessions.c.messages).where(
                agent_app_sessions.c.app == name, agent_app_sessions.c.actor == actor,
                agent_app_sessions.c.session_id == session_id)).first()
        return json.loads(r.messages) if r else []

    def invoke(self, actor: str, name: str, body: dict) -> dict:
        import httpx

        d = self._row(name)
        if not self._may_use(actor, d):
            raise AgentAppError("no such agent app", 404)
        text = body.get("input")
        session_id = str(body.get("session_id") or uuid.uuid4().hex)
        if not isinstance(text, str) or not text.strip() or len(text) > 100_000:
            raise AgentAppError("input: the message, up to 100,000 characters")
        if not SESSION.match(session_id):
            raise AgentAppError("session_id: letters, digits and _ . : -")
        why = self.agents.runnable(d["agent_id"])
        if why:
            raise AgentAppError(why, 403)
        from cairn.identity.agents import AgentError

        try:
            self.agents.admit(d["agent_id"])              # the agent's quotas
        except AgentError as e:
            raise AgentAppError(str(e), getattr(e, "status", 429))
        if d["status"] != "running":
            raise AgentAppError(f"{name} is not running: {d['error'] or d['status']}", 503)
        ep = self.runtime.endpoint(name, d["version"])
        if ep is None:                                   # Cairn restarted, or the process died: start it again
            self.runtime.start(name, d["version"], base64.b64decode(d["bundle"]))
            ep = self.runtime.endpoint(name, d["version"])
        history = self.session(actor, name, session_id) + [{"role": "user", "content": text}]
        token = self.agents.issue_run_token(d["agent_id"], actor)
        started = _now()
        try:
            r = httpx.post(f"{ep[0]}/invocations", json={"messages": history[-self.history:], "session_id": session_id,
                                                          "user": actor},
                           headers={"X-Cairn-Runtime-Secret": ep[1], "X-Cairn-Run-Token": token}, timeout=600)
        except httpx.HTTPError as e:
            raise AgentAppError(f"the agent could not be reached: {type(e).__name__}", 502)
        finally:
            self.agents.revoke_token(token)
        experiment = d["manifest"].get("_mlflow_experiment")
        if r.status_code != 200:
            try:
                why = r.json().get("detail") or r.text
            except ValueError:
                why = r.text
            self._audit("agent_app.invoked", actor, name, {"session": session_id, "ok": False,
                                                           "ms": _now() - started})
            if self.mlflow is not None and experiment:
                self.mlflow.record(experiment, name, d["version"], actor, session_id, False, started,
                                   {"latency_ms": _now() - started, "input_chars": len(text)})
            raise AgentAppError(str(why)[:1000], 502)
        out = r.json()
        if self.mlflow is not None and experiment:
            self.mlflow.record(experiment, name, d["version"], actor, session_id, True, started,
                               {"latency_ms": _now() - started, "input_chars": len(text),
                                "output_chars": len(out.get("output") or ""), "turns": len(history) // 2 + 1})
        history.append({"role": "assistant", "content": out["output"]})
        with self.engine.begin() as c:
            c.execute(delete(agent_app_sessions).where(agent_app_sessions.c.app == name,
                                                       agent_app_sessions.c.actor == actor,
                                                       agent_app_sessions.c.session_id == session_id))
            c.execute(insert(agent_app_sessions).values(app=name, actor=actor, session_id=session_id,
                                                        messages=json.dumps(history[-self.history:]),
                                                        updated_ms=_now()))
        self._audit("agent_app.invoked", actor, name, {"session": session_id, "ok": True, "ms": _now() - started,
                                                       "version": d["version"]})
        return {"output": out["output"], "session_id": session_id, "version": d["version"]}

    def _audit(self, action: str, actor: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("ai", action, actor, urn(name), details)


def build_agent_apps_router(a: AgentApps, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/agent-apps", tags=["ai"])

    def person(p) -> str:
        if p.is_service or getattr(p, "via", "") == "agent":
            raise HTTPException(403, "agent apps are deployed and asked by people")
        return p.urn

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except AgentAppError as e:
            raise HTTPException(e.status, str(e))

    @router.post("", status_code=201)
    async def deploy(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: a.deploy(person(p), body))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        return await call(lambda: a.list(person(p)))

    @router.get("/{name}")
    async def get(name: str, p=Depends(current_principal)):
        return await call(lambda: a.get(person(p), name))

    @router.delete("/{name}", status_code=204)
    async def delete_(name: str, p=Depends(current_principal)):
        await call(lambda: a.delete(person(p), name))

    @router.post("/{name}/invocations")
    async def invoke(name: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: a.invoke(person(p), name, body))

    @router.get("/{name}/sessions/{session_id}")
    async def session(name: str, session_id: str, p=Depends(current_principal)):
        return await call(lambda: a.session(person(p), name, session_id))

    return router
