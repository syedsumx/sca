"""Sharing BI assets with people and groups: saved queries, workbooks and dashboards.

Each asset has an owner. The owner shares it (``PUT /sql/shares/<kind>/<id>``) with people or groups as a
**viewer** (opens and runs it) or an **editor** (also changes it). Only the owner shares, makes it visible to
everyone (``shared``), or deletes it; an editor's save keeps the owner and the sharing as they are. Someone it is
not shared with gets 404, as if it did not exist.

Sharing never shares data: every query, workbook element and tile still runs as the person looking, under their
own policies, masks and row filters. A dashboard shared with someone lets them open the saved queries its tiles
are built on (to render them), and nothing more.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Index, PrimaryKeyConstraint, String, Table, and_, delete, insert, select

from cairn.common.db import metadata

KINDS = ("query", "workbook", "dashboard")
ROLES = {"VIEWER": 1, "EDITOR": 2}
OWNER = 3
PRINCIPAL = re.compile(r"^urn:li:(corpuser|corpGroup):[^\s]{1,250}$")
MAX_SHARES = 500

bi_shares = Table(
    "cairn_bi_shares", metadata,
    Column("kind", String(16), nullable=False),
    Column("asset_id", String(64), nullable=False),
    Column("principal", String(256), nullable=False),
    Column("role", String(8), nullable=False),
    Column("granted_by", String(256), nullable=False),
    Column("granted_ms", BigInteger, nullable=False),
    PrimaryKeyConstraint("kind", "asset_id", "principal"),
    Index("ix_cairn_bi_shares_principal", "principal", "kind"),
)


class ShareError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def role_name(level: int) -> Optional[str]:
    return {1: "VIEWER", 2: "EDITOR", 3: "OWNER"}.get(level)


def _table(kind: str):
    if kind == "query":
        from cairn.sqlworkspace.workspace import saved_queries as t
    elif kind == "workbook":
        from cairn.sqlworkspace.workbooks import workbooks as t
    elif kind == "dashboard":
        from cairn.sqlworkspace.boards import dashboards as t
    else:
        raise ShareError(f"kind: one of {', '.join(KINDS)}", 404)
    return t


class Sharing:
    def __init__(self, engine, groups_of: Callable[[str], list[str]], audit=None,
                 notify: Optional[Callable[[str, str, str, list[str], dict], Any]] = None):
        self.engine, self.groups_of, self.audit, self.notify = engine, groups_of, audit, notify

    def _me(self, actor: str) -> set[str]:
        try:
            return {actor, *self.groups_of(actor)}
        except Exception:                                 # no groups known: only what is shared with the person
            return {actor}

    def level(self, actor: str, kind: str, asset_id: str, conn=None) -> int:
        """The actor's role on the asset by sharing (0: none), directly or through a group. Not ownership."""
        q = select(bi_shares.c.role).where(and_(bi_shares.c.kind == kind, bi_shares.c.asset_id == asset_id,
                                                bi_shares.c.principal.in_(self._me(actor))))
        if conn is not None:
            rows = conn.execute(q).all()
        else:
            with self.engine.connect() as c:
                rows = c.execute(q).all()
        return max([ROLES.get(r.role, 0) for r in rows] or [0])

    def ids(self, actor: str, kind: str):
        """A subquery of the ids of ``kind`` shared with the actor (for list queries)."""
        return select(bi_shares.c.asset_id).where(and_(bi_shares.c.kind == kind,
                                                       bi_shares.c.principal.in_(self._me(actor))))

    def shares(self, actor: str, kind: str, asset_id: str) -> dict:
        owner = self._owner(kind, asset_id)
        lvl = OWNER if owner == actor else self.level(actor, kind, asset_id)
        if lvl < ROLES["EDITOR"]:
            raise ShareError(f"no such {kind}", 404) if lvl == 0 else \
                ShareError("only its owner and editors see who it is shared with", 403)
        with self.engine.connect() as conn:
            rows = conn.execute(select(bi_shares).where(and_(bi_shares.c.kind == kind, bi_shares.c.asset_id == asset_id))
                                .order_by(bi_shares.c.principal)).all()
        return {"kind": kind, "id": asset_id, "owner": owner,
                "shares": [{"principal": r.principal, "role": r.role, "granted_by": r.granted_by,
                            "granted_ms": r.granted_ms} for r in rows]}

    def mine(self, actor: str) -> list[dict]:
        """Every share of the actor's own queries, workbooks and dashboards, one record each (``GET /sql/shares``:
        what Terraform's cairn_sql_share and ``cairn export`` read)."""
        out = []
        with self.engine.connect() as conn:
            for kind in KINDS:
                t = _table(kind)
                rows = conn.execute(select(bi_shares).join(t, and_(bi_shares.c.kind == kind, bi_shares.c.asset_id == t.c.id))
                                    .where(t.c.owner == actor).order_by(bi_shares.c.asset_id, bi_shares.c.principal)).all()
                out += [{"id": f"{kind}:{r.asset_id}:{r.principal}", "kind": kind, "item": r.asset_id,
                         "principal": r.principal, "role": r.role, "owner": actor, "granted_ms": r.granted_ms}
                        for r in rows]
        return out

    def set(self, actor: str, kind: str, asset_id: str, principal: str, role: Optional[str]) -> dict:
        owner = self._owner(kind, asset_id)
        if owner != actor:
            lvl = self.level(actor, kind, asset_id)
            raise ShareError(f"no such {kind}", 404) if lvl == 0 else ShareError(f"only its owner shares a {kind}", 403)
        if not PRINCIPAL.match(principal or ""):
            raise ShareError("principal: urn:li:corpuser:… or urn:li:corpGroup:…")
        if principal == owner:
            raise ShareError("the owner already has it")
        if role is not None and role not in ROLES:
            raise ShareError("role: VIEWER or EDITOR (or none to stop sharing)")
        with self.engine.begin() as conn:
            conn.execute(delete(bi_shares).where(and_(bi_shares.c.kind == kind, bi_shares.c.asset_id == asset_id,
                                                      bi_shares.c.principal == principal)))
            if role is not None:
                n = len(conn.execute(select(bi_shares.c.principal).where(and_(
                    bi_shares.c.kind == kind, bi_shares.c.asset_id == asset_id))).all())
                if n >= MAX_SHARES:
                    raise ShareError(f"shared with at most {MAX_SHARES} people and groups; share with a group")
                conn.execute(insert(bi_shares).values(kind=kind, asset_id=asset_id, principal=principal, role=role,
                                                      granted_by=actor, granted_ms=int(time.time() * 1000)))
        if self.audit is not None:
            self.audit.record("sql", "bi.shared" if role else "bi.unshared", actor, f"{kind}:{asset_id}",
                              {"principal": principal, "role": role})
        if role and self.notify is not None and principal.startswith("urn:li:corpuser:"):
            try:
                name = self._name(kind, asset_id)
                self.notify(f"{actor.rsplit(':', 1)[-1]} shared a {kind} with you", name,
                            f"You may {'open and change' if role == 'EDITOR' else 'open'} {name!r}.", [principal],
                            {"kind": kind, "id": asset_id, "role": role})
            except Exception:                             # a notification that fails does not undo the share
                pass
        return self.shares(actor, kind, asset_id)

    def drop(self, kind: str, asset_id: str, conn=None) -> None:
        """Forgets an asset's shares (when it is deleted)."""
        stmt = delete(bi_shares).where(and_(bi_shares.c.kind == kind, bi_shares.c.asset_id == asset_id))
        if conn is not None:
            conn.execute(stmt)
        else:
            with self.engine.begin() as c:
                c.execute(stmt)

    def _owner(self, kind: str, asset_id: str) -> str:
        t = _table(kind)
        with self.engine.connect() as conn:
            r = conn.execute(select(t.c.owner).where(t.c.id == asset_id)).first()
        if r is None:
            raise ShareError(f"no such {kind}", 404)
        return r.owner

    def _name(self, kind: str, asset_id: str) -> str:
        t = _table(kind)
        with self.engine.connect() as conn:
            return str(conn.execute(select(t.c.name).where(t.c.id == asset_id)).scalar() or asset_id)


def access(sharing: Optional[Sharing], actor: str, kind: str, row) -> int:
    """The actor's level on an asset row: OWNER, EDITOR or VIEWER (shared with them or with everyone), or 0."""
    if row is None:
        return 0
    if row.owner == actor:
        return OWNER
    lvl = sharing.level(actor, kind, row.id) if sharing is not None else 0
    return max(lvl, ROLES["VIEWER"] if row.shared else 0)


def build_sharing_router(sharing: Sharing, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/sql/shares", tags=["sql"])

    def call(fn):
        try:
            return fn()
        except ShareError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    def mine(p=Depends(current_principal)):
        return {"shares": sharing.mine(p.urn)}

    @router.get("/{kind}/{asset_id}")
    def get(kind: str, asset_id: str, p=Depends(current_principal)):
        return call(lambda: sharing.shares(p.urn, kind, asset_id))

    @router.put("/{kind}/{asset_id}")
    def put(kind: str, asset_id: str, body: dict = Body(...), p=Depends(current_principal)):
        return call(lambda: sharing.set(p.urn, kind, asset_id, str(body.get("principal") or ""), body.get("role")))

    return router
