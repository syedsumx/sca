"""``/reports``: coverage, adoption, quality and governance figures. Admin-only unless
``CAIRN_REPORTS_OPEN=true`` opens them to every signed-in user."""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from cairn.common.security import Principal
from pydantic import BaseModel, Field

from cairn.reports.costs import CostRow, CostService
from cairn.reports.service import ReportService, to_csv

MANAGE = "MANAGE_GLOBAL_SETTINGS"


class CostIngest(BaseModel):
    rows: list[CostRow] = Field(max_length=50_000)


class DeprecationIn(BaseModel):
    urn: str
    reason: str = Field(default="", max_length=1000)


def build_router(reports: ReportService, current_principal, authorize, can_view, summaries,
                 costs: Optional[CostService] = None, groups_of=None) -> APIRouter:
    router = APIRouter(prefix="/reports", tags=["reports"])

    def _reader(p: Principal) -> None:
        if os.environ.get("CAIRN_REPORTS_OPEN", "").lower() not in ("1", "true", "yes"):
            authorize(p, MANAGE, None)

    def _latest() -> dict:
        return reports.latest() or reports.compute()

    @router.get("")
    def latest(p: Principal = Depends(current_principal)):
        _reader(p)
        return _latest()

    @router.post("/refresh")
    def refresh(p: Principal = Depends(current_principal)):
        authorize(p, MANAGE, None)
        return reports.compute()

    @router.get("/history")
    def history(days: int = Query(90, ge=1, le=730), p: Principal = Depends(current_principal)):
        _reader(p)
        return reports.history(days)

    @router.get("/coverage.csv")
    def coverage_csv(p: Principal = Depends(current_principal)):
        _reader(p)
        snap = _latest()
        return Response(to_csv(snap), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="cairn-coverage-{snap["day"]}.csv"'})

    @router.get("/assets")
    def assets(missing: Optional[str] = None, has: Optional[str] = None, domain: Optional[str] = None,
               platform: Optional[str] = None, limit: int = Query(200, ge=1, le=2000),
               p: Principal = Depends(current_principal)):
        """The datasets behind a figure. ``domain=none`` selects datasets without a domain."""
        _reader(p)
        try:
            rows = reports.assets(missing, "" if domain == "none" else domain, platform, has, limit * 2)
        except ValueError as e:
            raise HTTPException(400, str(e))
        check = can_view(p)
        rows = [r for r in rows if check(r["urn"])][:limit]
        names = summaries([r["urn"] for r in rows])
        return [{**r, "name": (names.get(r["urn"]) or {}).get("name") or r["urn"]} for r in rows]

    if costs is None:
        return router

    def _named(rows: list[dict], p: Principal) -> list[dict]:
        check = can_view(p)
        rows = [r for r in rows if check(r["urn"])]
        names = summaries([r["urn"] for r in rows])
        return [{**r, "name": (names.get(r["urn"]) or {}).get("name") or r["urn"]} for r in rows]

    @router.post("/costs")
    def ingest_costs(body: CostIngest, p: Principal = Depends(current_principal)):
        """Daily figures per dataset from any source the built-in collectors do not cover."""
        authorize(p, MANAGE, None)
        return {"ingested": costs.ingest(body.rows)}

    @router.get("/costs/idle")
    def idle(days: int = Query(30, ge=1, le=365), p: Principal = Depends(current_principal)):
        _reader(p)
        return _named(costs.idle(days), p)

    @router.get("/costs/expensive")
    def expensive(days: int = Query(30, ge=1, le=365), min_cost_usd: float = Query(1.0, ge=0),
                  p: Principal = Depends(current_principal)):
        _reader(p)
        return _named(costs.expensive(days, min_cost_usd=min_cost_usd), p)

    @router.get("/costs/jobs")
    def job_costs(days: int = Query(30, ge=1, le=365), p: Principal = Depends(current_principal)):
        """What each Spark application cost, priced from its runs (cores x wall-clock time)."""
        _reader(p)
        return costs.jobs(days)

    @router.get("/costs/trino")
    def trino_costs(by: str = Query("user", pattern="^(user|team|table)$"), days: int = Query(30, ge=1, le=365),
                    p: Principal = Depends(current_principal)):
        """Trino's CPU, scanned bytes and cost per person, team or table. Operators (or everyone, with
        CAIRN_REPORTS_OPEN) see all queries; anyone else sees what their own queries cost."""
        everyone = (os.environ.get("CAIRN_REPORTS_OPEN", "").lower() in ("1", "true", "yes")
                    or authorize(p, MANAGE, None, raise_on_deny=False))
        mine = None if everyone else p.urn.removeprefix("urn:li:corpuser:")
        if mine == p.urn:                       # not a person (and not an operator): nothing of theirs to show
            raise HTTPException(403, "Trino costs are shown to people, for their own queries, and to operators")
        try:
            out = costs.trino(by, days, user=mine, groups_of=groups_of)
        except ValueError as e:
            raise HTTPException(400, str(e))
        out["scope"] = "mine" if mine else "all"
        if by == "table":
            check = can_view(p)
            rows = [r for r in out["rows"] if check(r["key"])]
            names = summaries([r["key"] for r in rows])
            out["rows"] = [{**r, "name": (names.get(r["key"]) or {}).get("name") or r["key"]} for r in rows]
        return out

    @router.get("/costs/asset")
    def asset_cost(urn: str, p: Principal = Depends(current_principal)):
        if not can_view(p)(urn):
            raise HTTPException(404, "not found")
        return costs.asset(urn)

    @router.post("/costs/propose-deprecation")
    def propose(body: DeprecationIn, p: Principal = Depends(current_principal)):
        _reader(p)
        if not can_view(p)(body.urn):
            raise HTTPException(404, "not found")
        return costs.propose_deprecation(body.urn, p.urn, body.reason)

    return router
