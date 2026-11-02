"""What each bundle manages (``cairn bundle``): the names of the resources it created, so applying it again
updates them and ``--prune`` deletes only its own, never resources made by hand or by other bundles.

Versioned: an apply sends the version it read, so two applies of one bundle at once cannot both win.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, select, update

from cairn.common.db import metadata

bundles = Table(
    "cairn_bundles", metadata,
    Column("name", String(128), primary_key=True),
    Column("resources", Text, nullable=False),        # {"pipes": ["orders", ...], "streams": [...], ...}
    Column("version", Integer, nullable=False),
    Column("digest", String(64), nullable=False, default=""),
    Column("applied_by", String(256), nullable=False),
    Column("applied_ms", BigInteger, nullable=False),
)

NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
KINDS = ("pipes", "streams", "derived", "maintenance")


class BundleStateError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class BundleState:
    def __init__(self, engine, audit=None):
        self.engine, self.audit = engine, audit

    def get(self, name: str) -> dict:
        if not NAME.match(name):
            raise BundleStateError("a bundle name is lowercase letters, digits, '.', '_' and '-'")
        with self.engine.connect() as conn:
            r = conn.execute(select(bundles).where(bundles.c.name == name)).first()
        if r is None:
            return {"name": name, "resources": {k: [] for k in KINDS}, "version": 0, "digest": ""}
        return {"name": r.name, "resources": json.loads(r.resources), "version": r.version, "digest": r.digest,
                "applied_by": r.applied_by, "applied_ms": r.applied_ms}

    def put(self, name: str, body: dict, actor: str) -> dict:
        cur = self.get(name)
        if body.get("version") != cur["version"]:
            raise BundleStateError("the bundle was applied meanwhile: plan again", 409)
        res = body.get("resources") or {}
        if not isinstance(res, dict) or set(res) - set(KINDS) or \
                not all(isinstance(v, list) and all(isinstance(x, str) and len(x) <= 512 for x in v) for v in res.values()):
            raise BundleStateError(f"resources is {{kind: [names]}} for kinds {', '.join(KINDS)}")
        out = {k: sorted(set(res.get(k) or [])) for k in KINDS}
        digest = str(body.get("digest") or "")[:64]
        now = int(time.time() * 1000)
        with self.engine.begin() as conn:
            if cur["version"] == 0:
                conn.execute(bundles.insert().values(name=name, resources=json.dumps(out), version=1, digest=digest,
                                                     applied_by=actor, applied_ms=now))
            elif not conn.execute(update(bundles).where(bundles.c.name == name, bundles.c.version == cur["version"])
                                  .values(resources=json.dumps(out), version=cur["version"] + 1, digest=digest,
                                          applied_by=actor, applied_ms=now)).rowcount:
                raise BundleStateError("the bundle was applied meanwhile: plan again", 409)
            if self.audit is not None:
                self.audit.record("iac", "bundle.applied", actor, None, {"bundle": name, "digest": digest, "resources": out},
                                  conn=conn)
        return self.get(name)


def build_bundles_router(state: BundleState, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/v1/bundles", tags=["iac"])

    def wrap(fn):
        try:
            return fn()
        except BundleStateError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/{name}")
    def get(name: str, p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return wrap(lambda: state.get(name))

    @router.put("/{name}")
    def put(name: str, body: dict = Body(...), p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return wrap(lambda: state.put(name, body, p.urn))

    return router
