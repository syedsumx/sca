"""Managed model serving: a registered model version deployed from Cairn, scaled with its load, invoked as the
caller (L63).

``POST /v1/ml/deployments`` with ``{"name", "model", "version", "min_replicas": 0, "max_replicas": 4,
"target_concurrency": 2, "idle_minutes": 10}``; ``POST /v1/ml/deployments/<name>/invocations`` predicts.

* **Who.** Deploying, changing and deleting need ``MANAGE_ML_MODEL`` on the model (or owning it); invoking needs
  ``USE_ML_MODEL`` on it, checked on every call as the caller, who is the one audited and monitored. A model's
  servers are reached only through Cairn.
* **Scaling.** Cairn counts the requests it has in flight to each deployment. Every ``CAIRN_ML_SCALE_SECONDS``
  (10) each Cairn process records its average concurrency, and the deployment is scaled to
  ``ceil(total concurrency / target_concurrency)`` replicas within ``[min_replicas, max_replicas]``: up at once,
  down one replica per tick. With no request for ``idle_minutes`` it goes to ``min_replicas``, which may be zero;
  the next call then starts one replica and waits for it (``CAIRN_ML_COLD_START_SECONDS``, 180).
* **Runtime** (``CAIRN_ML_SERVING_RUNTIME=process``): MLflow's own model server (``mlflow models serve``,
  ``CAIRN_ML_SERVING_MLFLOW``) as local processes on the API's host, so one API node. A Kubernetes runtime
  (a Deployment per model deployment) is not available yet.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from typing import Any, Optional

from sqlalchemy import BigInteger, Column, Float, Integer, String, Table, and_, delete, insert, select, update
from starlette.requests import Request  # module level: FastAPI resolves the endpoints' annotations here

from cairn.common.db import metadata

logger = logging.getLogger(__name__)
NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
NODE = f"{socket.gethostname()}-{os.getpid()}"

model_deployments = Table(
    "cairn_model_deployments", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(48), nullable=False, unique=True),
    Column("owner", String(256), nullable=False),
    Column("model", String(256), nullable=False),
    Column("version", String(32), nullable=False),
    Column("min_replicas", Integer, nullable=False),
    Column("max_replicas", Integer, nullable=False),
    Column("target_concurrency", Float, nullable=False),
    Column("idle_minutes", Integer, nullable=False),
    Column("replicas", Integer, nullable=False),
    Column("last_request_ms", BigInteger, nullable=True),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)
model_load = Table(
    "cairn_model_load", metadata,
    Column("deployment_id", String(32), primary_key=True),
    Column("node", String(128), primary_key=True),
    Column("sampled_ms", BigInteger, nullable=False),
    Column("concurrency", Float, nullable=False),
)


class ServingError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def desired_replicas(concurrency: float, target: float, current: int, lo: int, hi: int, idle: bool) -> int:
    """How many replicas a deployment should have: enough for its concurrency at ``target`` each (up at once,
    down one at a time), within ``[lo, hi]``; ``lo`` once idle. While it is in use, never below one."""
    if idle:
        return lo
    want = math.ceil(concurrency / target) if concurrency > 0 else 0
    if concurrency > 0 or current > 0:
        want = max(want, 1)
    if want < current:
        want = current - 1
    return max(lo, min(hi, want))


# --------------------------------------------------------------------------- runtimes
class ProcessRuntime:
    """MLflow's model server as local processes, one per replica (a single host)."""

    def __init__(self, mlflow_bin: str, tracking_uri: str, workers: int = 1):
        self.mlflow_bin, self.tracking_uri, self.workers = mlflow_bin, tracking_uri, workers
        self.procs: dict[str, list[tuple[subprocess.Popen, int]]] = {}
        self.lock = threading.Lock()

    def _start(self, d: dict) -> tuple[subprocess.Popen, int]:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = {**os.environ, "MLFLOW_TRACKING_URI": self.tracking_uri,
               "PATH": f"{os.path.dirname(self.mlflow_bin)}:{os.environ.get('PATH', '')}"}
        log = open(os.path.join(os.environ.get("CAIRN_ML_SERVING_LOG_DIR", "/tmp"), f"serve-{d['name']}-{port}.log"), "w")
        p = subprocess.Popen([self.mlflow_bin, "models", "serve", "-m", f"models:/{d['model']}/{d['version']}",
                              "-h", "127.0.0.1", "-p", str(port), "--env-manager", "local", "-w", str(self.workers)],
                             env=env, stdout=log, stderr=subprocess.STDOUT)
        return p, port

    def scale(self, d: dict, n: int) -> None:
        with self.lock:
            cur = self.procs.setdefault(d["id"], [])
            cur[:] = [(p, port) for p, port in cur if p.poll() is None]
            while len(cur) < n:
                cur.append(self._start(d))
            while len(cur) > n:
                p, _ = cur.pop()
                p.terminate()

    def endpoints(self, d: dict) -> list[str]:
        import httpx

        out = []
        with self.lock:
            cur = [(p, port) for p, port in self.procs.get(d["id"], []) if p.poll() is None]
        for _, port in cur:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/ping", timeout=2).status_code == 200:
                    out.append(f"http://127.0.0.1:{port}")
            except httpx.HTTPError:
                pass
        return out

    def running(self, d: dict) -> int:
        with self.lock:
            return sum(1 for p, _ in self.procs.get(d["id"], []) if p.poll() is None)

    def delete(self, d: dict) -> None:
        self.scale(d, 0)
        self.procs.pop(d["id"], None)


def runtime_from_env() -> Optional[Any]:
    rt = os.environ.get("CAIRN_ML_SERVING_RUNTIME", "")
    if rt == "process":
        return ProcessRuntime(os.environ.get("CAIRN_ML_SERVING_MLFLOW", "mlflow"),
                              os.environ.get("CAIRN_MLFLOW_URL", ""), int(os.environ.get("CAIRN_ML_SERVING_WORKERS", "1")))
    if rt:
        raise ValueError("CAIRN_ML_SERVING_RUNTIME: process (a Kubernetes runtime is not available yet)")
    return None


# --------------------------------------------------------------------------- the service
class ModelServing:
    def __init__(self, engine, runtime, gateway, audit=None):
        """``gateway``: the MLflow gateway (its rights: may_manage / may_use, its monitoring, its registry)."""
        self.engine, self.runtime, self.gateway, self.audit = engine, runtime, gateway, audit
        self.inflight: dict[str, int] = {}
        self.area: dict[str, float] = {}           # in-flight x seconds since the last sample, per deployment
        self.mark: dict[str, float] = {}
        self.last_request: dict[str, int] = {}
        self.lock = threading.Lock()
        self.rr = itertools.count()
        self._sampled_at: dict[str, float] = {}
        self.tick_s = float(os.environ.get("CAIRN_ML_SCALE_SECONDS", "10"))
        self.cold_s = float(os.environ.get("CAIRN_ML_COLD_START_SECONDS", "180"))
        self._task: Optional[asyncio.Task] = None

    def _need_runtime(self):
        if self.runtime is None:
            raise ServingError("model serving is not configured: set CAIRN_ML_SERVING_RUNTIME", 503)
        return self.runtime

    def _get(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(model_deployments).where(model_deployments.c.name == name)).first()
        if r is None:
            raise ServingError(f"no deployment {name}", 404)
        return dict(r._mapping)

    def _check(self, body: dict) -> dict:
        model, version = str(body.get("model") or ""), str(body.get("version") or "")
        if not model or not re.fullmatch(r"\d{1,9}", version):
            raise ServingError("model and version (a registered model's version number)")
        lo, hi = body.get("min_replicas", 0), body.get("max_replicas", 2)
        if not (isinstance(lo, int) and isinstance(hi, int) and 0 <= lo <= hi <= 50 and hi >= 1):
            raise ServingError("min_replicas and max_replicas: 0 <= min <= max <= 50, max >= 1")
        target = body.get("target_concurrency", 2)
        idle = body.get("idle_minutes", 10)
        if not (isinstance(target, (int, float)) and 0.5 <= float(target) <= 1000):
            raise ServingError("target_concurrency: requests in flight per replica (0.5 to 1000)")
        if not (isinstance(idle, int) and 1 <= idle <= 1440):
            raise ServingError("idle_minutes: 1 to 1440")
        return {"model": model, "version": version, "min_replicas": lo, "max_replicas": hi,
                "target_concurrency": float(target), "idle_minutes": idle}

    def _version_exists(self, p, model: str, version: str) -> None:
        status, _, raw = self.gateway.registry_get(p, model, version)
        if status != 200:
            raise ServingError(f"{model} has no version {version} you may see", 404)

    def deploy(self, p, body: dict, name: Optional[str] = None) -> dict:
        rt = self._need_runtime()
        v = self._check(body)
        if not self.gateway.may_manage(p, v["model"]):
            raise ServingError(f"deploying {v['model']} needs MANAGE_ML_MODEL on it", 403)
        self._version_exists(p, v["model"], v["version"])
        now = _now()
        with self.engine.begin() as conn:
            if name is None:
                name = str(body.get("name") or "")
                if not NAME.match(name):
                    raise ServingError("name: lowercase letters, digits and -")
                if conn.execute(select(model_deployments.c.id).where(model_deployments.c.name == name)).first():
                    raise ServingError(f"a deployment {name} exists", 409)
                did = uuid.uuid4().hex
                conn.execute(insert(model_deployments).values(id=did, name=name, owner=p.urn, replicas=v["min_replicas"],
                                                              created_ms=now, updated_ms=now, **v))
            else:
                cur = self._get(name)
                if not self.gateway.may_manage(p, cur["model"]):
                    raise ServingError(f"changing {name} needs MANAGE_ML_MODEL on {cur['model']}", 403)
                conn.execute(update(model_deployments).where(model_deployments.c.id == cur["id"])
                             .values(updated_ms=now, replicas=max(v["min_replicas"], min(cur["replicas"], v["max_replicas"])),
                                     **v))
                if (cur["model"], cur["version"]) != (v["model"], v["version"]):
                    rt.delete(cur)                         # a new version: the old servers go
        d = self._get(name)
        rt.scale(d, d["replicas"])
        self._audit(p.urn, "ml.model_deployed", d, {"model": d["model"], "version": d["version"],
                                                   "min": d["min_replicas"], "max": d["max_replicas"]})
        return self.describe(p, name)

    def describe(self, p, name: str) -> dict:
        d = self._get(name)
        if not (self.gateway.may_use(p, d["model"])):
            raise ServingError(f"no deployment {name}", 404)
        rt = self._need_runtime()
        return {k: d[k] for k in ("name", "owner", "model", "version", "min_replicas", "max_replicas",
                                  "target_concurrency", "idle_minutes", "replicas", "last_request_ms")} | {
            "running": rt.running(d), "in_flight": self.inflight.get(d["id"], 0)}

    def list(self, p) -> list[dict]:
        with self.engine.connect() as conn:
            names = [r[0] for r in conn.execute(select(model_deployments.c.name).order_by(model_deployments.c.name))]
        out = []
        for n in names:
            try:
                out.append(self.describe(p, n))
            except ServingError:
                continue
        return out

    def undeploy(self, p, name: str) -> None:
        d = self._get(name)
        if not self.gateway.may_manage(p, d["model"]):
            raise ServingError(f"deleting {name} needs MANAGE_ML_MODEL on {d['model']}", 403)
        self._need_runtime().delete(d)
        with self.engine.begin() as conn:
            conn.execute(delete(model_load).where(model_load.c.deployment_id == d["id"]))
            conn.execute(delete(model_deployments).where(model_deployments.c.id == d["id"]))
        self._audit(p.urn, "ml.model_undeployed", d, {})

    # ---- load and scaling -----------------------------------------------------------------------------
    def _account(self, did: str, delta: int) -> None:
        now = time.monotonic()
        with self.lock:
            last = self.mark.get(did, now)
            self.area[did] = self.area.get(did, 0.0) + self.inflight.get(did, 0) * (now - last)
            self.mark[did] = now
            self.inflight[did] = self.inflight.get(did, 0) + delta

    def sample(self) -> None:
        """Records this process's average concurrency per deployment since the last sample."""
        now = time.monotonic()
        with self.lock:
            rows = []
            for did in set(self.inflight) | set(self.area):
                last = self.mark.get(did, now)
                area = self.area.get(did, 0.0) + self.inflight.get(did, 0) * (now - last)
                window = max(now - self._sampled_at.get(did, now - self.tick_s), 1e-3)
                rows.append((did, area / window))
                self.area[did], self.mark[did] = 0.0, now
                self._sampled_at[did] = now
            seen = dict(self.last_request)
        ms = _now()
        with self.engine.begin() as conn:
            for did, conc in rows:
                conn.execute(delete(model_load).where(and_(model_load.c.deployment_id == did, model_load.c.node == NODE)))
                conn.execute(insert(model_load).values(deployment_id=did, node=NODE, sampled_ms=ms, concurrency=conc))
            for did, at in seen.items():
                conn.execute(update(model_deployments).where(and_(
                    model_deployments.c.id == did,
                    (model_deployments.c.last_request_ms.is_(None)) | (model_deployments.c.last_request_ms < at)))
                    .values(last_request_ms=at))

    def autoscale(self) -> dict[str, int]:
        """Scales every deployment to what its load needs; returns the new replica counts."""
        rt = self._need_runtime()
        now = _now()
        with self.engine.connect() as conn:
            deps = [dict(r._mapping) for r in conn.execute(select(model_deployments))]
            loads: dict[str, float] = {}
            for r in conn.execute(select(model_load).where(model_load.c.sampled_ms >= now - 3 * self.tick_s * 1000)):
                loads[r.deployment_id] = loads.get(r.deployment_id, 0.0) + r.concurrency
        out = {}
        for d in deps:
            idle = not d["last_request_ms"] or now - d["last_request_ms"] > d["idle_minutes"] * 60_000
            want = desired_replicas(loads.get(d["id"], 0.0), d["target_concurrency"], d["replicas"],
                                    d["min_replicas"], d["max_replicas"], idle and not self.inflight.get(d["id"]))
            if want != d["replicas"]:
                with self.engine.begin() as conn:
                    conn.execute(update(model_deployments).where(model_deployments.c.id == d["id"]).values(replicas=want))
                logger.info("model deployment %s: %d -> %d replicas", d["name"], d["replicas"], want)
                self._audit("urn:li:corpuser:cairn", "ml.model_scaled", d, {"from": d["replicas"], "to": want})
            if rt.running(d) != want:
                rt.scale(d, want)
            out[d["name"]] = want
        return out

    async def run(self) -> None:
        """The background loop: sample, then scale (every process samples; the scaling is idempotent)."""
        from starlette.concurrency import run_in_threadpool

        while True:
            await asyncio.sleep(self.tick_s)
            try:
                await run_in_threadpool(self.sample)
                await run_in_threadpool(self.autoscale)
            except Exception:  # noqa: BLE001 - one failed tick must not stop scaling
                logger.exception("model autoscaling tick failed")

    # ---- invoking ---------------------------------------------------------------------------------------
    async def invoke(self, p, name: str, body: bytes, content_type: str) -> tuple[int, dict, bytes]:
        from starlette.concurrency import run_in_threadpool

        d = await run_in_threadpool(self._get, name)
        if not self.gateway.may_use(p, d["model"]):
            raise ServingError(f"using model {d['model']} needs USE_ML_MODEL on it", 403)
        rt = self._need_runtime()
        self.last_request[d["id"]] = _now()
        self._account(d["id"], +1)
        try:
            urls = await run_in_threadpool(rt.endpoints, d)
            if not urls:                                # scaled to zero (or starting): one replica, and wait for it
                if d["replicas"] < 1:
                    with self.engine.begin() as conn:
                        conn.execute(update(model_deployments).where(model_deployments.c.id == d["id"]).values(replicas=1))
                    d["replicas"] = 1
                await run_in_threadpool(rt.scale, d, max(1, d["replicas"]))
                end = time.monotonic() + self.cold_s
                while not urls and time.monotonic() < end:
                    await asyncio.sleep(1)
                    urls = await run_in_threadpool(rt.endpoints, d)
                if not urls:
                    raise ServingError(f"{name} did not start within {int(self.cold_s)}s", 503)
            url = urls[next(self.rr) % len(urls)]
            return await self.gateway.forward_invocation(p, d["model"], url, body, content_type)
        finally:
            self._account(d["id"], -1)

    def _audit(self, actor: str, action: str, d: dict, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("ml", action, actor, f"model-deployment:{d['name']}", details)


def build_serving_router(ms: ModelServing, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from fastapi.responses import Response
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/ml/deployments", tags=["ml"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except ServingError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        return await call(lambda: ms.list(p))

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: ms.deploy(p, body))

    @router.get("/{name}")
    async def get(name: str, p=Depends(current_principal)):
        return await call(lambda: ms.describe(p, name))

    @router.put("/{name}")
    async def change(name: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: ms.deploy(p, body, name))

    @router.delete("/{name}", status_code=204)
    async def drop(name: str, p=Depends(current_principal)):
        await call(lambda: ms.undeploy(p, name))

    @router.post("/{name}/invocations")
    async def invoke(name: str, request: Request, p=Depends(current_principal)):
        try:
            status, headers, content = await ms.invoke(p, name, await request.body(),
                                                       request.headers.get("content-type", ""))
        except ServingError as e:
            raise HTTPException(e.status, str(e))
        return Response(content, status_code=status, headers=headers)

    return router
