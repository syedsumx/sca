"""BI version history: every save of a saved query, workbook or dashboard is kept; compare two and restore one.

``GET /sql/versions/<kind>/<id>`` lists an asset's versions (newest first: number, who saved it, when, its name);
``GET …/<n>`` gives one as it was; ``GET …/diff?a=<n>&b=<m>`` the fields that changed and a line-by-line diff;
``POST …/<n>/restore`` saves that version again, as a new version, through the asset's own save, so the same
rules hold (owners and editors restore; a dashboard's tiles are checked again; who may see the asset is kept).
Anyone who may open the asset may read its history. Versions go with the asset when it is deleted; the newest
``CAIRN_BI_VERSIONS`` (200) of each are kept.
"""

from __future__ import annotations

import difflib
import json
import os
import time
from typing import Any, Optional

from sqlalchemy import BigInteger, Column, Integer, PrimaryKeyConstraint, String, Table, and_, delete, func, insert, select
from sqlalchemy.exc import IntegrityError

from cairn.common.db import JSONText, metadata

KINDS = ("query", "workbook", "dashboard")
# What a version holds of each kind: the fields its save takes.
FIELDS = {"query": ("name", "description", "sql", "connection", "shared", "chart"),
          "workbook": ("name", "description", "spec", "connection", "shared"),
          "dashboard": ("name", "description", "shared", "tiles", "controls")}

bi_versions = Table(
    "cairn_bi_versions", metadata,
    Column("kind", String(16), nullable=False),
    Column("asset_id", String(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("doc", JSONText, nullable=False),
    Column("saved_by", String(512), nullable=False),
    Column("saved_ms", BigInteger, nullable=False),
    PrimaryKeyConstraint("kind", "asset_id", "version"),
)


class VersionError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Versions:
    def __init__(self, engine, workspace, workbooks, boards, sharing=None):
        self.engine, self.ws, self.wb, self.boards, self.sharing = engine, workspace, workbooks, boards, sharing
        self.keep = max(2, int(os.environ.get("CAIRN_BI_VERSIONS", "200")))

    # ---- recording --------------------------------------------------------------------------------
    def _row(self, kind: str, aid: str) -> Optional[Any]:
        from cairn.sqlworkspace.sharing import _table

        t = _table(kind)
        with self.engine.connect() as conn:
            return conn.execute(select(t).where(t.c.id == aid)).first()

    @staticmethod
    def _doc(kind: str, r) -> dict:
        d = dict(r._mapping)
        for k in ("chart", "spec", "tiles", "controls"):
            if k in d and isinstance(d[k], str):
                d[k] = json.loads(d[k] or ("[]" if k in ("tiles", "controls") else "{}"))
        d["shared"] = bool(d.get("shared"))
        return {k: d.get(k) for k in FIELDS[kind]}

    def record(self, kind: str, aid: str, actor: Optional[str] = None) -> Optional[int]:
        """After a save: keeps the asset as it now is (unless it is the same as its latest version). After a
        delete: forgets its versions."""
        r = self._row(kind, aid)
        if r is None:
            with self.engine.begin() as conn:
                conn.execute(delete(bi_versions).where(and_(bi_versions.c.kind == kind, bi_versions.c.asset_id == aid)))
            return None
        doc = self._doc(kind, r)
        who = actor or r.owner
        for _ in range(5):                                  # two replicas saving at once: take the next number
            with self.engine.begin() as conn:
                last = conn.execute(select(bi_versions.c.version, bi_versions.c.doc).where(and_(
                    bi_versions.c.kind == kind, bi_versions.c.asset_id == aid)).order_by(bi_versions.c.version.desc())
                    .limit(1)).first()
                if last is not None and last.doc == doc:
                    return last.version
                n = (last.version if last else 0) + 1
                try:
                    with conn.begin_nested():
                        conn.execute(insert(bi_versions).values(kind=kind, asset_id=aid, version=n, doc=doc, saved_by=who,
                                                                saved_ms=int(time.time() * 1000)))
                except IntegrityError:
                    continue
                conn.execute(delete(bi_versions).where(and_(bi_versions.c.kind == kind, bi_versions.c.asset_id == aid,
                                                            bi_versions.c.version <= n - self.keep)))
                return n
        raise VersionError("could not number the version (too many saves at once)", 409)

    # ---- reading ----------------------------------------------------------------------------------
    def _may(self, actor: str, kind: str, aid: str, need: int = 1):
        from cairn.sqlworkspace.sharing import access

        if kind not in KINDS:
            raise VersionError(f"kind: one of {', '.join(KINDS)}", 404)
        r = self._row(kind, aid)
        level = access(self.sharing, actor, kind, r)
        if not level:
            raise VersionError(f"no such {kind}", 404)
        if level < need:
            raise VersionError(f"only its owner and editors restore a {kind}", 403)
        return r

    def list(self, actor: str, kind: str, aid: str) -> list[dict]:
        self._may(actor, kind, aid)
        with self.engine.connect() as conn:
            rows = conn.execute(select(bi_versions).where(and_(bi_versions.c.kind == kind, bi_versions.c.asset_id == aid))
                                .order_by(bi_versions.c.version.desc())).all()
        return [{"version": r.version, "name": r.doc.get("name"), "saved_by": r.saved_by, "saved_ms": r.saved_ms}
                for r in rows]

    def get(self, actor: str, kind: str, aid: str, version: int) -> dict:
        self._may(actor, kind, aid)
        return self._get(kind, aid, version)

    def _get(self, kind: str, aid: str, version: int) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(bi_versions).where(and_(bi_versions.c.kind == kind, bi_versions.c.asset_id == aid,
                                                            bi_versions.c.version == version))).first()
        if r is None:
            raise VersionError("no such version", 404)
        return {"version": r.version, "saved_by": r.saved_by, "saved_ms": r.saved_ms, "doc": r.doc}

    def diff(self, actor: str, kind: str, aid: str, a: int, b: int) -> dict:
        self._may(actor, kind, aid)
        va, vb = self._get(kind, aid, a)["doc"], self._get(kind, aid, b)["doc"]
        changed = [k for k in FIELDS[kind] if va.get(k) != vb.get(k)]
        lines = difflib.unified_diff(json.dumps(va, indent=2, sort_keys=True).splitlines(),
                                     json.dumps(vb, indent=2, sort_keys=True).splitlines(),
                                     f"version {a}", f"version {b}", lineterm="")
        return {"a": a, "b": b, "changed": changed, "diff": "\n".join(lines)}

    # ---- restoring --------------------------------------------------------------------------------
    def restore(self, actor: str, kind: str, aid: str, version: int) -> dict:
        from cairn.sqlworkspace.sharing import ROLES

        self._may(actor, kind, aid, ROLES["EDITOR"])
        doc = dict(self._get(kind, aid, version)["doc"])
        if kind == "query":
            out = self.ws.save(actor, doc, aid)
        elif kind == "workbook":
            out = self.wb.save(actor, doc, aid)
        else:
            out = self.boards.save_dashboard(actor, doc, aid)
        return {"restored": version, "asset": out}

    def count(self, kind: str, aid: str) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(bi_versions).where(and_(
                bi_versions.c.kind == kind, bi_versions.c.asset_id == aid))).scalar_one())


def build_versions_router(v: Versions, current_principal) -> Any:
    from fastapi import APIRouter, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    from cairn.sqlworkspace.workbooks import WorkbookError
    from cairn.sqlworkspace.workspace import WorkspaceError

    router = APIRouter(prefix="/sql/versions", tags=["sql"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except (VersionError, WorkspaceError, WorkbookError) as e:
            raise HTTPException(e.status, str(e))

    @router.get("/{kind}/{aid}")
    async def list_(kind: str, aid: str, p=Depends(current_principal)):
        return await call(lambda: v.list(p.urn, kind, aid))

    @router.get("/{kind}/{aid}/diff")
    async def diff(kind: str, aid: str, a: int, b: int, p=Depends(current_principal)):
        return await call(lambda: v.diff(p.urn, kind, aid, a, b))

    @router.get("/{kind}/{aid}/{version}")
    async def get(kind: str, aid: str, version: int, p=Depends(current_principal)):
        return await call(lambda: v.get(p.urn, kind, aid, version))

    @router.post("/{kind}/{aid}/{version}/restore")
    async def restore(kind: str, aid: str, version: int, p=Depends(current_principal)):
        return await call(lambda: v.restore(p.urn, kind, aid, version))

    return router
