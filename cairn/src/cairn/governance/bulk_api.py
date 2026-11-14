"""``/bulk``: spreadsheet export and import of catalog metadata."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from cairn.common.security import Principal
from cairn.governance.bulk import MAX_ROWS, BulkError, BulkService, export_rows, to_csv

MAX_UPLOAD_BYTES = 10 << 20


def build_router(bulk: BulkService, current_principal, authorize, can_view, search, classification) -> APIRouter:
    router = APIRouter(prefix="/bulk", tags=["bulk"])

    def _wrap(fn):
        try:
            return fn()
        except BulkError as e:
            raise HTTPException(e.status, str(e))

    def _allowed(p: Principal):
        return lambda privilege, urn: authorize(p, privilege, urn, raise_on_deny=False)

    @router.get("/export")
    def export(query: str = "*", urns: Optional[list[str]] = Query(None), limit: int = Query(500, ge=1, le=5000),
               p: Principal = Depends(current_principal)):
        check = can_view(p)
        picked = urns or [r["urn"] for r in search(query, limit)]
        picked = [u for u in dict.fromkeys(picked) if u.startswith("urn:li:dataset:") and check(u)][:limit]
        body = to_csv(export_rows(picked, bulk.graph.get_aspect, classification))
        return Response(body, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="cairn-metadata.csv"'})

    @router.post("/import")
    async def import_csv(request: Request, p: Principal = Depends(current_principal)):
        raw = await request.body()
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"files up to {MAX_UPLOAD_BYTES >> 20} MB, {MAX_ROWS} rows")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(400, "save the file as CSV UTF-8")
        check = can_view(p)
        out = _wrap(lambda: bulk.preview(text, p.urn, _allowed(p)))
        hidden = [c for c in out["changes"] + out["denied"] if not check(c["urn"])]
        if hidden:   # never reveal or edit assets the importer cannot see
            out["errors"] += [f"{len({c['urn'] for c in hidden})} asset(s) not found"]
            out["changes"] = [c for c in out["changes"] if check(c["urn"])]
            out["denied"] = [c for c in out["denied"] if check(c["urn"])]
            bulk.restrict(out["batch_id"], out["changes"])
        return out

    @router.post("/{batch_id}/apply")
    def apply(batch_id: str, p: Principal = Depends(current_principal)):
        return _wrap(lambda: bulk.apply(batch_id, p.urn, _allowed(p)))

    @router.post("/{batch_id}/undo")
    def undo(batch_id: str, p: Principal = Depends(current_principal)):
        return _wrap(lambda: bulk.undo(batch_id, p.urn))

    @router.get("")
    def history(p: Principal = Depends(current_principal)):
        return bulk.history(p.urn)

    return router
