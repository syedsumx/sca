"""Machine learning on the lakehouse: MLflow tracking and model registry behind Cairn sign-in, models as
catalog assets with lineage from their training tables, and model serving through Cairn.

MLflow (``CAIRN_MLFLOW_URL``) is reached only through Cairn, at ``<cairn>/v1/ml``: clients set
``MLFLOW_TRACKING_URI=<cairn>/v1/ml`` and ``MLFLOW_TRACKING_TOKEN=<their Cairn token>`` (a person's or a
pipeline user's), and every call is made as them:

* **Runs are theirs.** ``mlflow.user`` on a new run is set to the caller, whatever the client says, and
  cannot be changed afterwards. Experiments and runs are open to signed-in people, like a shared
  notebook workspace; artifacts go through the same door (``--serve-artifacts``).
* **The registry is governed.** A registered model belongs to whoever created it. Reading one needs
  ``VIEW_ENTITY_PAGE`` on it (``urn:li:mlModelGroup:(urn:li:dataPlatform:mlflow,<name>,PROD)``) or being its
  owner; changing it (versions, aliases, stages, tags, rename, delete) needs ``MANAGE_ML_MODEL`` on it or
  being its owner. Searches list only what the caller may see. A name another person owns is refused.
  Every change is audited.
* **Models are catalog assets.** Each new version is published to the catalog as an ML model in its
  model group, owned by its creator, with the run that trained it as a job whose inputs are the run's
  datasets (``mlflow.log_input``): a dataset whose source or name is a lakehouse table
  (``lakehouse.<ns>.<table>`` or ``iceberg://<ns>.<table>``) becomes lineage from that table.
* **Serving.** ``POST /v1/ml/serve/<model>/invocations`` forwards to the model's serving endpoint
  (``CAIRN_ML_SERVING``: ``{"<model>": "http://<mlflow models serve>"}``) for callers with ``USE_ML_MODEL``
  on the model, or its owner; each call is counted in the audit log.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlencode

from sqlalchemy import BigInteger, Column, String, Table, select
from starlette.requests import Request  # module level: FastAPI resolves the endpoints' annotations here

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

PLATFORM = "urn:li:dataPlatform:mlflow"
VIEW, MANAGE, USE = "VIEW_ENTITY_PAGE", "MANAGE_ML_MODEL", "USE_ML_MODEL"
ALLOWED = re.compile(r"^(api/2\.0/mlflow|api/2\.0/mlflow-artifacts|api/3\.0/mlflow|ajax-api/2\.0/mlflow)/")
REGISTRY = re.compile(r"^(?:api|ajax-api)/2\.0/mlflow/(registered-models|model-versions)/([a-z-]+(?:/[a-z-]+)?)$")
# Registry calls that only read; everything else there changes something.
READS = {("registered-models", "get"), ("registered-models", "search"), ("registered-models", "get-latest-versions"),
         ("registered-models", "alias"), ("model-versions", "get"), ("model-versions", "search"),
         ("model-versions", "get-download-uri")}
TABLE_REF = re.compile(r"^(?:iceberg://|lakehouse\.)([A-Za-z0-9_][A-Za-z0-9_.$-]*)\.([A-Za-z0-9_$-]+)$")

ml_models = Table(
    "cairn_ml_models", metadata,
    Column("name", String(256), primary_key=True),
    Column("owner", String(256), nullable=False),
    Column("created_s", BigInteger, nullable=False),
)


def group_urn(name: str) -> str:
    return f"urn:li:mlModelGroup:({PLATFORM},{name},PROD)"


def model_urn(name: str, version: str) -> str:
    return f"urn:li:mlModel:({PLATFORM},{name}.v{version},PROD)"


class MlError(Exception):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


class MlflowGateway:
    def __init__(self, upstream: str, engine, audit=None, graph=None,
                 table_urn: Optional[Callable[[list[str], str], str]] = None,
                 allowed: Optional[Callable[[Any, str, Optional[str]], bool]] = None,
                 serving: Optional[dict] = None, http=None):
        """``allowed(principal, privilege, urn)``: Cairn's policy decision. ``table_urn(ns, name)``: the
        dataset a lakehouse table is in the catalog."""
        self.upstream = upstream.rstrip("/")
        self.engine, self.audit, self.graph, self.table_urn = engine, audit, graph, table_urn
        self.allowed = allowed or (lambda p, priv, urn: False)
        self.serving = {str(k): str(v).rstrip("/") for k, v in (serving or {}).items()}
        self.http = http
        self.monitoring = None          # cairn.ml.monitoring.ModelMonitoring, when wired

    @classmethod
    def from_env(cls, engine, audit, graph, table_urn, allowed) -> Optional["MlflowGateway"]:
        url = os.environ.get("CAIRN_MLFLOW_URL", "")
        if not url:
            return None
        raw = os.environ.get("CAIRN_ML_SERVING", "").strip()
        serving = json.loads(open(raw).read() if raw and not raw.startswith("{") else raw or "{}")
        return cls(url, engine, audit, graph, table_urn, allowed, serving)

    # ---- who may do what with a registered model ------------------------------------------------
    def owner(self, name: str) -> Optional[str]:
        with self.engine.connect() as conn:
            return conn.execute(select(ml_models.c.owner).where(ml_models.c.name == name)).scalar()

    def _operator(self, p) -> bool:
        return self.allowed(p, "MANAGE_SYSTEM_OPERATIONS", None)

    def may_view(self, p, name: str) -> bool:
        return self.owner(name) == p.urn or self._operator(p) or self.allowed(p, VIEW, group_urn(name)) \
            or self.allowed(p, MANAGE, group_urn(name))

    def may_manage(self, p, name: str) -> bool:
        return self.owner(name) == p.urn or self._operator(p) or self.allowed(p, MANAGE, group_urn(name))

    def may_use(self, p, name: str) -> bool:
        return self.may_manage(p, name) or self.allowed(p, USE, group_urn(name))

    # ---- forwarding ------------------------------------------------------------------------------
    async def _send(self, method: str, path: str, query: str, body: bytes, headers: dict) -> tuple[int, dict, bytes]:
        import httpx

        client = self.http or httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=600, write=600, pool=30))
        try:
            url = f"{self.upstream}/{path}" + (f"?{query}" if query else "")
            r = await client.request(method, url, content=body, headers=headers)
            return r.status_code, {k: v for k, v in r.headers.items() if k.lower() == "content-type"}, r.content
        except httpx.HTTPError as e:
            raise MlError(f"MLflow is not reachable: {e}", 502)
        finally:
            if self.http is None:
                await client.aclose()

    async def _json(self, method: str, path: str, params: dict) -> dict:
        status, _, content = await self._send(method, path, urlencode(params, doseq=True), b"",
                                              {"accept": "application/json"})
        if status >= 300:
            raise MlError(f"MLflow answered {status}: {content[:200]!r}", 502)
        return json.loads(content or b"{}")

    async def handle(self, p, method: str, path: str, query: str, body: bytes, content_type: str) -> tuple[int, dict, bytes]:
        path = path.lstrip("/")
        if not ALLOWED.match(path):
            raise MlError("not an MLflow API path", 404)
        payload: dict = {}
        if body and "json" in (content_type or "") and method in ("POST", "PATCH", "PUT", "DELETE"):
            try:
                payload = json.loads(body)
            except ValueError:
                raise MlError("expected JSON", 400)
        params = {k: v[-1] for k, v in parse_qs(query).items()}
        headers = {"content-type": content_type} if content_type else {}

        # Runs are the caller's.
        if path.endswith("/runs/create"):
            tags = [t for t in payload.get("tags") or [] if t.get("key") != "mlflow.user"]
            payload["tags"] = tags + [{"key": "mlflow.user", "value": p.urn.removeprefix("urn:li:corpuser:")}]
            body = json.dumps(payload).encode()
        if path.endswith("/runs/set-tag") and payload.get("key") == "mlflow.user":
            raise MlError("mlflow.user is set by Cairn: the person who started the run")
        if path.endswith("/runs/log-batch") and any(t.get("key") == "mlflow.user" for t in payload.get("tags") or []):
            raise MlError("mlflow.user is set by Cairn: the person who started the run")

        m = REGISTRY.match(path)
        if not m:
            return await self._send(method, path, query, body, headers)
        kind, op = m.group(1), m.group(2)
        name = str(payload.get("name") or params.get("name") or "")
        reading = (kind, op) in READS and not (op == "alias" and method != "GET")
        if op == "search":
            status, h, content = await self._send(method, path, query, body, headers)
            if status == 200:
                key = "registered_models" if kind == "registered-models" else "model_versions"
                data = json.loads(content or b"{}")
                data[key] = [x for x in data.get(key) or [] if self.may_view(p, x.get("name", ""))]
                content = json.dumps(data).encode()
            return status, h, content
        if not name:
            raise MlError("the model's name is needed", 400)
        if kind == "registered-models" and op == "create":
            return await self._create(p, name, method, path, query, body, headers)
        if reading:
            if not self.may_view(p, name):
                raise MlError(f"seeing model {name} needs {VIEW} on it", 403)
            return await self._send(method, path, query, body, headers)
        if not self.may_manage(p, name):
            raise MlError(f"changing model {name} needs {MANAGE} on it (or being its owner)", 403)
        new_name = str(payload.get("new_name") or "")
        if op == "rename" and new_name and self.owner(new_name) not in (None, p.urn):
            raise MlError(f"model {new_name} belongs to someone else", 403)
        status, h, content = await self._send(method, path, query, body, headers)
        if status == 200:
            self._changed(p, kind, op, name, payload)
            if kind == "model-versions" and op == "create":
                try:
                    await self.publish(json.loads(content).get("model_version") or {})
                except Exception as e:  # noqa: BLE001 - the version exists; the catalog catches up on the next one
                    logger.warning("ml: publishing %s to the catalog failed: %s", name, e)
        return status, h, content

    async def _create(self, p, name, method, path, query, body, headers):
        from sqlalchemy.exc import IntegrityError

        owner = self.owner(name)
        if owner is not None and owner != p.urn and not self._operator(p):
            raise MlError(f"model {name} belongs to someone else", 403)
        status, h, content = await self._send(method, path, query, body, headers)
        if status == 200 and owner is None:
            try:
                with self.engine.begin() as conn:
                    conn.execute(ml_models.insert().values(name=name, owner=p.urn, created_s=int(time.time())))
                    if self.audit is not None:
                        self.audit.record("ml", "ml.model_created", p.urn, group_urn(name), {"name": name}, conn=conn)
            except IntegrityError:
                pass
        return status, h, content

    def _changed(self, p, kind: str, op: str, name: str, payload: dict) -> None:
        with self.engine.begin() as conn:
            if kind == "registered-models" and op == "rename" and payload.get("new_name"):
                conn.execute(ml_models.update().where(ml_models.c.name == name).values(name=payload["new_name"]))
            if kind == "registered-models" and op == "delete":
                conn.execute(ml_models.delete().where(ml_models.c.name == name))
            if self.audit is not None:
                detail = {k: payload[k] for k in ("version", "alias", "stage", "key", "new_name") if k in payload}
                self.audit.record("ml", f"ml.{kind.replace('-', '_')}.{op.replace('/', '_').replace('-', '_')}", p.urn,
                                  group_urn(name), {"name": name, **detail}, conn=conn)

    # ---- the catalog -----------------------------------------------------------------------------
    def _dataset_urn(self, ds: dict) -> Optional[str]:
        source = ds.get("source") or ""
        candidates = [ds.get("name") or ""]
        try:
            parsed = json.loads(source) if isinstance(source, str) and source.startswith("{") else {}
        except ValueError:
            parsed = {}
        candidates += [str(parsed.get(k) or "") for k in ("uri", "url", "path", "table_name")] + [str(source)]
        for c in candidates:
            hit = TABLE_REF.match(c.strip())
            if hit and self.table_urn is not None:
                return self.table_urn(hit.group(1).split("."), hit.group(2))
        return None

    async def publish(self, version: dict) -> Optional[str]:
        """The model version as a catalog asset: its group, itself, and the run that trained it."""
        from cairn.common.proposals import make_mcp

        if self.graph is None or not version.get("name"):
            return None
        name, ver = version["name"], str(version.get("version"))
        owner = self.owner(name)
        mcps = [make_mcp(group_urn(name), "mlModelGroupProperties",
                         {"name": name, "description": version.get("description") or None,
                          "customProperties": {"registry": "mlflow"}}),
                make_mcp(group_urn(name), "status", {"removed": False})]
        if owner:
            mcps.append(make_mcp(group_urn(name), "ownership", {
                "owners": [{"owner": owner, "type": "TECHNICAL_OWNER"}],
                "lastModified": {"time": int(time.time() * 1000), "actor": owner}}))
        training: list[str] = []
        run_id = version.get("run_id") or ""
        if run_id:
            run = (await self._json("GET", "api/2.0/mlflow/runs/get", {"run_id": run_id})).get("run") or {}
            info = run.get("info") or {}
            exp = str(info.get("experiment_id") or "0")
            flow = f"urn:li:dataFlow:(mlflow,experiment-{exp},PROD)"
            job = f"urn:li:dataJob:({flow},run-{run_id})"
            inputs = sorted({u for u in (self._dataset_urn((d or {}).get("dataset") or {})
                                         for d in (run.get("inputs") or {}).get("dataset_inputs") or []) if u})
            mcps += [make_mcp(flow, "dataFlowInfo", {"name": f"MLflow experiment {exp}", "customProperties": {}}),
                     make_mcp(job, "dataJobInfo", {
                         "name": info.get("run_name") or run_id, "flowUrn": flow,
                         "type": {"com.linkedin.datajob.azkaban.AzkabanJobType": "COMMAND"},
                         "customProperties": {"run_id": run_id, "user": str(info.get("user_id") or "")}}),
                     make_mcp(job, "dataJobInputOutput", {"inputDatasets": inputs, "outputDatasets": []})]
            training = [job]
        mcps.append(make_mcp(model_urn(name, ver), "mlModelProperties", {
            "name": f"{name} v{ver}", "version": {"versionTag": ver}, "groups": [group_urn(name)],
            "trainingJobs": training, "customProperties": {
                k: str(v) for k, v in {"source": version.get("source"), "run_id": run_id,
                                        "created_by": version.get("user_id") or owner}.items() if v}}))
        mcps.append(make_mcp(model_urn(name, ver), "status", {"removed": False}))
        self.graph.emit(mcps)
        return model_urn(name, ver)

    # ---- serving ---------------------------------------------------------------------------------
    async def invoke(self, p, name: str, body: bytes, content_type: str) -> tuple[int, dict, bytes]:
        url = self.serving.get(name)
        if url is None:
            raise MlError(f"model {name} is not served", 404)
        if not self.may_use(p, name):
            raise MlError(f"using model {name} needs {USE_ML_MODEL} on it", 403)
        return await self.forward_invocation(p, name, url, body, content_type)

    async def forward_invocation(self, p, name: str, url: str, body: bytes, content_type: str) -> tuple[int, dict, bytes]:
        """One prediction from the model server at ``url``, for a caller already allowed: monitored and audited."""
        import httpx
        from starlette.concurrency import run_in_threadpool

        client = self.http or httpx.AsyncClient(timeout=120)
        started = time.monotonic()
        try:
            r = await client.post(f"{url}/invocations", content=body, headers={"content-type": content_type or "application/json"})
        except httpx.HTTPError as e:
            if self.monitoring is not None:
                await run_in_threadpool(self.monitoring.record, name, p.urn, 502,
                                        int((time.monotonic() - started) * 1000), body, b"")
            raise MlError(f"the model server is not reachable: {e}", 502)
        finally:
            if self.http is None:
                await client.aclose()
        latency = int((time.monotonic() - started) * 1000)
        headers = {"content-type": r.headers.get("content-type", "application/json")}
        if self.monitoring is not None:      # every call is logged; the payloads only for a monitored model
            headers["x-cairn-inference-id"] = await run_in_threadpool(
                self.monitoring.record, name, p.urn, r.status_code, latency, body, r.content)
        if self.audit is not None:
            self.audit.record("ml", "ml.model_invoked", p.urn, group_urn(name), {"name": name, "status": r.status_code})
        return r.status_code, headers, r.content

    def registry_get(self, p, name: str, version: str) -> tuple[int, dict, bytes]:
        """A registered model version, if ``p`` may see the model (MLflow's model-versions/get)."""
        import httpx

        if not (self.may_view(p, name) or self.may_use(p, name)):
            return 404, {}, b""
        r = httpx.get(f"{self.upstream}/api/2.0/mlflow/model-versions/get", params={"name": name, "version": version},
                      timeout=30)
        return r.status_code, {}, r.content


USE_ML_MODEL = USE


def build_ml_router(gw: MlflowGateway, current_principal):
    from fastapi import APIRouter, Depends
    from fastapi.responses import JSONResponse, Response

    router = APIRouter(prefix="/v1/ml", tags=["ml"])

    def refuse(e: MlError):
        # MLflow clients read error_code and message.
        code = {403: "PERMISSION_DENIED", 404: "RESOURCE_DOES_NOT_EXIST", 400: "INVALID_PARAMETER_VALUE"}.get(
            e.status, "TEMPORARILY_UNAVAILABLE")
        return JSONResponse({"error_code": code, "message": str(e)}, status_code=e.status)

    @router.post("/serve/{name}/invocations")
    async def invoke(name: str, request: Request, p=Depends(current_principal)):
        try:
            status, headers, content = await gw.invoke(p, name, await request.body(), request.headers.get("content-type", ""))
        except MlError as e:
            return refuse(e)
        return Response(content, status_code=status, headers=headers)

    @router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def mlflow(path: str, request: Request, p=Depends(current_principal)):
        try:
            status, headers, content = await gw.handle(p, request.method, path, request.url.query, await request.body(),
                                                       request.headers.get("content-type", ""))
        except MlError as e:
            return refuse(e)
        return Response(content, status_code=status, headers=headers)

    return router
