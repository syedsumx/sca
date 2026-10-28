"""``/quality/contracts``: publish, read, check and retire data contracts."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, ValidationError

from cairn.common.security import Principal
from cairn.quality.contracts import ContractError, ContractService, ContractSpec, check_proposed


class CheckRequest(BaseModel):
    contract: Optional[dict[str, Any]] = None      # a proposed contract, or the published one for `dataset`
    dataset: Optional[str] = None
    columns: dict[str, dict]                       # proposed schema: name → {type, nullable}


def build_router(contracts: ContractService, current_principal, authorize, can_view) -> APIRouter:
    router = APIRouter(prefix="/quality/contracts", tags=["contracts"])

    def _wrap(fn):
        try:
            return fn()
        except ContractError as e:
            raise HTTPException(e.status, str(e))

    def _visible(p: Principal, urn: str) -> None:
        if not can_view(p)(urn):
            raise HTTPException(404, "no contract for this dataset")

    @router.get("")
    def list_contracts(p: Principal = Depends(current_principal)):
        check = can_view(p)
        return [c for c in contracts.list() if check(c["dataset"])]

    @router.put("")
    def publish(body: dict[str, Any] = Body(...), p: Principal = Depends(current_principal)):
        """Publish a contract (or a new version of one). Same version and terms: no change."""
        try:
            spec = ContractSpec.model_validate(body)
        except (ValidationError, ValueError) as e:
            raise HTTPException(422, str(e))
        authorize(p, "EDIT_ENTITY_ASSERTIONS", spec.dataset)
        return _wrap(lambda: contracts.upsert(spec, p.urn))

    @router.get("/one")
    def get_one(dataset: str = Query(...), p: Principal = Depends(current_principal)):
        _visible(p, dataset)
        out = _wrap(lambda: contracts.status(dataset))
        return {**out, "versions": [{k: v for k, v in r.items() if k != "spec"} for r in contracts.versions(dataset)]}

    @router.delete("/one")
    def retire(dataset: str = Query(...), p: Principal = Depends(current_principal)):
        _visible(p, dataset)
        authorize(p, "EDIT_ENTITY_ASSERTIONS", dataset)
        if not contracts.delete(dataset, p.urn):
            raise HTTPException(404, "no contract for this dataset")
        return {"retired": dataset}

    @router.post("/check")
    def check(body: CheckRequest, p: Principal = Depends(current_principal)):
        """Would this schema keep the promise? For CI on a proposed dbt or SQL change."""
        if body.contract is not None:
            try:
                spec = ContractSpec.model_validate(body.contract)
            except (ValidationError, ValueError) as e:
                raise HTTPException(422, str(e))
        elif body.dataset:
            _visible(p, body.dataset)
            row = contracts.get(body.dataset)
            if row is None:
                raise HTTPException(404, "no contract for this dataset")
            spec = ContractSpec.model_validate(row["spec"])
        else:
            raise HTTPException(422, "give a contract or a dataset")
        violations = check_proposed(spec, body.columns)
        return {"dataset": spec.dataset, "version": spec.version, "ok": not violations, "violations": violations}

    return router
