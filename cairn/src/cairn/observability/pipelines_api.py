"""``/v1/observability/pipelines``, ``/pipeline-slas``, ``/data-products`` and ``/exporters`` (L145).

Reading (pipelines, runs, logs, impact, quality, data products) needs ``VIEW_OPERATIONS`` or
``MANAGE_SYSTEM_OPERATIONS``; changing anything (SLAs, data products, exporters, a rerun, an Airflow read now)
needs ``MANAGE_SYSTEM_OPERATIONS``. An exporter's header names a secret: whoever creates or changes it must be able
to read that secret scope, and applying reads every value as the person applying (each read audited)."""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request  # noqa: F401  module level: FastAPI resolves the handlers' annotations here

from cairn.observability import exporters as ex
from cairn.observability.pipelines import PipelineObservability
from cairn.observability.service import ObservabilityError

MANAGE = "MANAGE_SYSTEM_OPERATIONS"
VIEW = "VIEW_OPERATIONS"


def build_router(pipes: PipelineObservability, current_principal, authorize, scopes: Any = None) -> APIRouter:
    router = APIRouter(prefix="/v1/observability", tags=["observability"])
    obs = pipes.obs

    def manager(p) -> Any:
        authorize(p, MANAGE, None)
        return p

    def viewer(p) -> Any:
        if not authorize(p, MANAGE, None, raise_on_deny=False):
            authorize(p, VIEW, None)
        return p

    async def call(fn, *a, **kw):
        try:
            return await run_in_threadpool(fn, *a, **kw)
        except ObservabilityError as e:
            raise HTTPException(e.status, str(e))
        except ex.ExporterError as e:
            raise HTTPException(e.status, str(e))

    # ---- pipelines and runs -----------------------------------------------------------------------------------
    @router.get("/pipelines")
    async def list_pipelines(q: str = Query("", max_length=200), source: str = Query("", max_length=32),
                             days: int = Query(30, ge=1, le=90), p=Depends(current_principal)):
        viewer(p)
        return {"items": await call(pipes.pipelines, q, source, days)}

    @router.post("/pipelines/sync")
    async def sync(p=Depends(current_principal)):
        """Read every configured Airflow's recent DAG runs now (the notifier does it every minute)."""
        manager(p)
        return await call(pipes.sync_airflow)

    @router.get("/pipelines/{key}")
    async def get_pipeline(key: str, limit: int = Query(50, ge=1, le=500), p=Depends(current_principal)):
        viewer(p)
        return await call(pipes.pipeline, key, limit)

    @router.get("/pipelines/{key}/runs/{run_id}")
    async def get_run(key: str, run_id: str, p=Depends(current_principal)):
        viewer(p)
        return await call(pipes.run, key, run_id, p.urn)

    @router.get("/pipelines/{key}/runs/{run_id}/diagnose")
    async def diagnose(key: str, run_id: str, p=Depends(current_principal)):
        viewer(p)
        return await call(pipes.diagnose, key, run_id)

    @router.get("/pipelines/{key}/runs/{run_id}/tasks/{task}/log")
    async def task_log(key: str, run_id: str, task: str, try_number: Optional[int] = Query(None, alias="try", ge=1),
                       p=Depends(current_principal)):
        viewer(p)
        return await call(pipes.task_log, key, run_id, task, try_number)

    @router.post("/pipelines/{key}/runs/{run_id}/rerun")
    async def rerun(key: str, run_id: str, p=Depends(current_principal)):
        manager(p)
        if getattr(p, "via", "") in ("agent", "agent_key") or getattr(p, "agent", None):
            raise HTTPException(403, "an agent proposes a rerun; a person approves it")
        return await call(pipes.rerun, p.urn, key, run_id)

    # ---- pipeline SLAs and data products (resource types) -----------------------------------------------------
    @router.get("/pipeline-slas")
    async def list_slas(p=Depends(current_principal)):
        viewer(p)
        return {"items": await call(obs.list, "pipeline_sla")}

    @router.post("/pipeline-slas", status_code=201)
    async def create_sla(body: dict = Body(...), p=Depends(current_principal)):
        manager(p)
        return await call(obs.create, "pipeline_sla", p.urn, body)

    @router.get("/pipeline-slas/{oid}")
    async def get_sla(oid: str, p=Depends(current_principal)):
        viewer(p)
        return await call(obs.get, "pipeline_sla", oid)

    @router.put("/pipeline-slas/{oid}")
    async def put_sla(oid: str, body: dict = Body(...), p=Depends(current_principal)):
        manager(p)
        return await call(obs.replace, "pipeline_sla", p.urn, oid, body)

    @router.delete("/pipeline-slas/{oid}", status_code=204)
    async def delete_sla(oid: str, p=Depends(current_principal)):
        manager(p)
        await call(obs.remove, "pipeline_sla", p.urn, oid)

    @router.get("/data-products")
    async def list_products(p=Depends(current_principal)):
        viewer(p)
        return {"items": await call(obs.list, "data_product")}

    @router.post("/data-products", status_code=201)
    async def create_product(body: dict = Body(...), p=Depends(current_principal)):
        manager(p)
        return await call(obs.create, "data_product", p.urn, body)

    @router.get("/data-products/{oid}")
    async def get_product(oid: str, p=Depends(current_principal)):
        viewer(p)
        return await call(obs.get, "data_product", oid)

    @router.put("/data-products/{oid}")
    async def put_product(oid: str, body: dict = Body(...), p=Depends(current_principal)):
        manager(p)
        return await call(obs.replace, "data_product", p.urn, oid, body)

    @router.delete("/data-products/{oid}", status_code=204)
    async def delete_product(oid: str, p=Depends(current_principal)):
        manager(p)
        await call(obs.remove, "data_product", p.urn, oid)

    @router.get("/data-products/{oid}/status")
    async def product_status(oid: str, p=Depends(current_principal)):
        viewer(p)
        return await call(pipes.data_product, oid)

    # ---- OTLP destinations ---------------------------------------------------------------------------------------
    def check_scopes(actor: str, body: dict) -> None:
        heads = body.get("headers") or []
        if not heads:
            return
        if scopes is None or not scopes.enabled:
            raise HTTPException(503, "secret scopes are off (CAIRN_SECRETS_KEY): an exporter's headers come from them")
        for h in heads:
            scope = str((h or {}).get("scope") or "")
            if scopes.level(actor, scope) < 1:
                raise HTTPException(403, f"you cannot read the secret scope {scope!r}: an exporter's header must come "
                                         "from a scope you may read")

    def exporter_docs() -> list[dict]:
        return obs._docs("exporter")

    @router.get("/exporters")
    async def list_exporters(p=Depends(current_principal)):
        viewer(p)
        return {"items": await call(obs.list, "exporter")}

    @router.post("/exporters", status_code=201)
    async def create_exporter(body: dict = Body(...), p=Depends(current_principal)):
        manager(p)
        doc = await call(ex.validate, body)
        await run_in_threadpool(check_scopes, p.urn, doc)
        return await call(obs.create, "exporter", p.urn, doc)

    @router.get("/exporters/collector-config")
    async def collector_config(p=Depends(current_principal)):
        """The Collector configuration fragment for the enabled exporters, and the Collector environment variables
        its header values go in (never the values)."""
        viewer(p)
        docs = await run_in_threadpool(exporter_docs)
        cfg = await call(ex.render, docs)
        names = sorted(ex.env_name(d["id"], i) for d in docs if d.get("enabled", True)
                       for i, _ in enumerate(d.get("headers") or []))
        return {"yaml": ex.to_yaml(cfg), "config": cfg, "env": names,
                "pipelines": sorted(cfg["service"]["pipelines"]),
                "secrets": [{"env": ex.env_name(d["id"], i), "scope": h["scope"], "key": h["key"]}
                            for d in docs if d.get("enabled", True) for i, h in enumerate(d.get("headers") or [])]}

    @router.post("/exporters/apply")
    async def apply(p=Depends(current_principal)):
        """Writes the fragment and the header values (read from their scopes as you) where the Compose overlay's
        Collector reads them (CAIRN_OBS_EXPORTERS_DIR)."""
        manager(p)
        docs = await run_in_threadpool(exporter_docs)
        cfg = await call(ex.render, docs)
        if any(d.get("headers") for d in docs) and (scopes is None or not scopes.enabled):
            raise HTTPException(503, "secret scopes are off (CAIRN_SECRETS_KEY)")

        def read(scope: str, key: str) -> str:
            try:
                return scopes.read(p.urn, scope, key)
            except Exception as e:  # noqa: BLE001 - a SecretScopeError: refused or missing
                raise ex.ExporterError(f"the secret {scope}/{key} could not be read as you: {e}",
                                       getattr(e, "status", 403))
        env = await call(ex.secret_env, docs, read)
        written = await call(ex.write, os.environ.get("CAIRN_OBS_EXPORTERS_DIR", ""), cfg, env)
        if obs.audit is not None:
            obs.audit.record("observability", "observability.exporters_applied", p.urn, None,
                             {"exporters": [d["id"] for d in docs if d.get("enabled", True)],
                              "pipelines": sorted(cfg["service"]["pipelines"])})
        return {"written": [os.path.basename(w) for w in written], "pipelines": sorted(cfg["service"]["pipelines"]),
                "next": "recreate the Collector to load them: docker compose -f docker-compose.cairn.yml -f "
                        "docker-compose.observability.yml up -d cairn-otel-collector"}

    @router.get("/exporters/{oid}")
    async def get_exporter(oid: str, p=Depends(current_principal)):
        viewer(p)
        return await call(obs.get, "exporter", oid)

    @router.put("/exporters/{oid}")
    async def put_exporter(oid: str, body: dict = Body(...), p=Depends(current_principal)):
        manager(p)
        doc = await call(ex.validate, {**body, "id": oid})
        await run_in_threadpool(check_scopes, p.urn, doc)
        return await call(obs.replace, "exporter", p.urn, oid, doc)

    @router.delete("/exporters/{oid}", status_code=204)
    async def delete_exporter(oid: str, p=Depends(current_principal)):
        manager(p)
        await call(obs.remove, "exporter", p.urn, oid)

    return router
