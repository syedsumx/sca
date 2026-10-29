"""``/sources``: set up, test, schedule and watch ingestion sources (MANAGE_INGESTION)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from cairn.common.security import Principal
from cairn.sources.catalog import SourceError
from cairn.sources.service import SourceIn, SourceService, TestIn


def build_router(svc: SourceService, current_principal, authorize) -> APIRouter:
    router = APIRouter(prefix="/sources", tags=["sources"])

    def _admin(p: Principal) -> None:
        authorize(p, "MANAGE_INGESTION", None)

    def _wrap(fn):
        from cairn.agent.gateway import GatewayError

        try:
            return fn()
        except SourceError as e:
            raise HTTPException(e.status, str(e))
        except GatewayError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/connectors")
    def connectors(p: Principal = Depends(current_principal)):
        """The connector gallery: each connector's setup form."""
        _admin(p)
        return svc.connectors()

    @router.get("/executors")
    def executors(p: Principal = Depends(current_principal)):
        """Where a source can run: Cairn's executor or an in-VPC agent pool."""
        _admin(p)
        return svc.executors()

    @router.get("")
    def list_(p: Principal = Depends(current_principal)):
        """Every source with its last run and health."""
        _admin(p)
        return _wrap(lambda: svc.list())

    @router.post("", status_code=201)
    def create(body: SourceIn, p: Principal = Depends(current_principal)):
        _admin(p)
        return _wrap(lambda: svc.create(body, p.urn))

    @router.post("/test")
    def test(body: TestIn, p: Principal = Depends(current_principal)):
        """Check the connection on the executor that will run the source; poll ``/sources/test/{id}``."""
        _admin(p)
        return _wrap(lambda: svc.test(body, p.urn))

    @router.get("/test/{test_id:path}")
    def test_result(test_id: str, p: Principal = Depends(current_principal)):
        _admin(p)
        return _wrap(lambda: svc.test_result(test_id))

    @router.get("/runs/{run_urn:path}")
    def run_log(run_urn: str, p: Principal = Depends(current_principal)):
        """One run: its numbers and its log."""
        _admin(p)
        return _wrap(lambda: svc.run_log(run_urn))

    @router.get("/{urn:path}/detail")
    def get(urn: str, p: Principal = Depends(current_principal)):
        """A source with its last 20 runs."""
        _admin(p)
        return _wrap(lambda: svc.get(urn))

    @router.put("/{urn:path}/detail")
    def update(urn: str, body: SourceIn, p: Principal = Depends(current_principal)):
        _admin(p)
        return _wrap(lambda: svc.update(urn, body, p.urn))

    @router.delete("/{urn:path}/detail", status_code=204)
    def delete(urn: str, p: Principal = Depends(current_principal)):
        _admin(p)
        _wrap(lambda: svc.delete(urn, p.urn))

    @router.post("/{urn:path}/run")
    def run(urn: str, p: Principal = Depends(current_principal)):
        _admin(p)
        return _wrap(lambda: svc.run(urn, p.urn))

    @router.post("/{urn:path}/cancel")
    def cancel(urn: str, body: dict, p: Principal = Depends(current_principal)):
        _admin(p)
        return _wrap(lambda: svc.cancel(urn, str(body.get("run") or ""), p.urn))

    return router


def build_openlineage_router(receiver, current_principal, authorize) -> APIRouter:
    """``POST /openlineage/api/v1/lineage``: point an OpenLineage HTTP transport at
    ``https://<cairn>/cairn/api/openlineage`` with a Cairn API key (``EDIT_LINEAGE``)."""
    from cairn.sources.openlineage import OpenLineageError

    router = APIRouter(prefix="/openlineage", tags=["lineage"])

    @router.post("/api/v1/lineage")
    async def lineage(request: Request, p: Principal = Depends(current_principal)):
        authorize(p, "EDIT_LINEAGE", None)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "the body is not JSON")
        try:
            return receiver.handle_many(body, p.urn)
        except OpenLineageError as e:
            raise HTTPException(422, str(e))

    return router
