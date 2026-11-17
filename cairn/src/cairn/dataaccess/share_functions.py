"""Functions in a share: a stored function's definition, read by the share's recipient (L169).

A stored function (``CREATE FUNCTION`` in a routines catalog; ``cairn.dataaccess.trino_functions``) holds logic that
belongs with shared data: how a margin is computed, how an identifier is normalised. This adds functions to a share,
so the recipient defines the same function on their own engine.

* **Attaching.** ``POST /v1/shares/<share>/functions`` with ``{"function": "routines.sales.margin"}``, by the
  share's maker or an operator, who needs ``MANAGE_DATASET`` on the function (what creating or replacing it needs).
  Cairn asks Trino for the definition **as that person** (``SHOW CREATE FUNCTION``) and keeps it with the share:
  every overload, as Trino prints it. Attaching again takes the definition as it is now.
  ``DELETE /v1/shares/<share>/functions/<function>`` takes it out; ``GET`` lists them.
* **The recipient** uses the share's own credential: ``GET /v1/shares/recipient/functions`` returns each function
  with its statements, and each statement's parts (arguments, return type, language, body) for engines whose
  ``CREATE FUNCTION`` is spelled differently.
* **What is shared is text.** The recipient never calls the function on the provider's Trino, and gets no access to
  tables its body names: a function that reads a table works for the recipient only over tables it has (the shared
  ones, under the names it gives them). The definition is the one kept when it was attached: later changes reach
  the recipient when the function is attached again.

Fetches are audited as the recipient (``share:<id>``) and counted in the share's usage (``function:<name>``).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable

from sqlalchemy import BigInteger, Column, PrimaryKeyConstraint, String, Table, Text, and_, delete, insert, select
from starlette.requests import Request  # module level: FastAPI resolves the endpoints' annotations here

from cairn.common.db import metadata
from cairn.dataaccess.shares import PREFIX, ShareError

logger = logging.getLogger(__name__)

MAX_PER_SHARE = 100
MAX_TEXT = 200_000
FUNCTION = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")

share_functions = Table(
    "cairn_share_functions", metadata,
    Column("share_id", String(32), nullable=False, index=True),
    Column("function", String(512), nullable=False),         # catalog.schema.name
    Column("statements", Text, nullable=False),              # JSON list: CREATE FUNCTION, one per overload
    Column("attached_by", String(256), nullable=False),
    Column("attached_ms", BigInteger, nullable=False),
    PrimaryKeyConstraint("share_id", "function"),
)


class ShareFunctions:
    def __init__(self, engine, shares, access, run: Callable[[str, str], list], recipient: Callable[[str], dict],
                 audit=None):
        """``access``: Cairn's Trino policy decisions (``TrinoAccess``); ``run(actor, sql)``: runs a statement on
        Trino as that person and returns its rows; ``recipient(authorization)``: the active share behind a
        credential."""
        self.engine, self.shares, self.access, self.run, self.recipient, self.audit = engine, shares, access, run, recipient, audit

    def _attached(self, share_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(
                select(share_functions).where(share_functions.c.share_id == share_id).order_by(share_functions.c.function))]

    # ---- the provider's side -----------------------------------------------------------------------------
    def list(self, actor: str, everyone: bool, ref: str) -> dict:
        row = self.shares._find(ref, actor, everyone)
        return {"share": row["name"], "functions": [
            {"function": r["function"], "overloads": len(json.loads(r["statements"])), "attached_by": r["attached_by"],
             "attached_ms": r["attached_ms"]} for r in self._attached(row["id"])]}

    def attach(self, actor: str, groups: list[str], everyone: bool, ref: str, function: str) -> dict:
        from cairn.dataaccess.trino import MANAGE

        row = self.shares._find(ref, actor, everyone)
        if not self.shares._active(row):
            raise ShareError("the share has ended", 409)
        function = str(function or "").strip().lower()
        if not FUNCTION.match(function):
            raise ShareError("function: catalog.schema.name, of a stored function")
        catalog, schema, name = function.split(".")
        if catalog not in self.access.routine_catalogs or \
                not self.access.allowed(actor, groups, MANAGE, f"urn:li:function:{function}"):
            raise ShareError(f"no function {function}", 404)   # one the person may not manage: not told it exists
        try:
            rows = self.run(actor, f'SHOW CREATE FUNCTION "{catalog}"."{schema}"."{name}"')
        except Exception as e:  # noqa: BLE001 - Trino refused, or there is no such function
            logger.info("share %s: definition of %s not read: %s", row["name"], function, e)
            raise ShareError(f"no function {function}", 404)
        statements = [str(r[0]) for r in rows if r and r[0]]
        if not statements or sum(len(s) for s in statements) > MAX_TEXT:
            raise ShareError(f"no function {function}" if not statements else f"{function} is over {MAX_TEXT} characters",
                             404 if not statements else 409)
        have = self._attached(row["id"])
        again = any(r["function"] == function for r in have)
        if not again and len(have) >= MAX_PER_SHARE:
            raise ShareError(f"a share carries at most {MAX_PER_SHARE} functions")
        with self.engine.begin() as conn:
            conn.execute(delete(share_functions).where(and_(share_functions.c.share_id == row["id"],
                                                            share_functions.c.function == function)))
            conn.execute(insert(share_functions).values(share_id=row["id"], function=function,
                                                        statements=json.dumps(statements), attached_by=actor,
                                                        attached_ms=int(time.time() * 1000)))
            if self.audit is not None:
                self.audit.record("shares", "share.function_attached", actor, None,
                                  {"share": row["id"], "name": row["name"], "recipient": row["recipient"],
                                   "function": function, "overloads": len(statements), "again": again}, conn=conn)
        return self.list(actor, everyone, ref)

    def detach(self, actor: str, everyone: bool, ref: str, function: str) -> dict:
        row = self.shares._find(ref, actor, everyone)
        with self.engine.begin() as conn:
            if not conn.execute(delete(share_functions).where(and_(
                    share_functions.c.share_id == row["id"], share_functions.c.function == str(function).lower()))).rowcount:
                raise ShareError(f"{function} is not in the share", 404)
            if self.audit is not None:
                self.audit.record("shares", "share.function_detached", actor, None,
                                  {"share": row["id"], "name": row["name"], "function": function}, conn=conn)
        return self.list(actor, everyone, ref)

    # ---- the recipient's side ----------------------------------------------------------------------------
    def recipient_functions(self, row: dict) -> list[dict]:
        from cairn.dataaccess.trino_functions import parse_create

        self.shares._count(row["id"], "", requests=1)
        out = []
        for r in self._attached(row["id"]):
            overloads = []
            for s in json.loads(r["statements"]):
                p = parse_create(s) or {}
                overloads.append({"statement": s, **{k: p.get(k, "") for k in ("arguments", "returns", "language", "comment", "body")}})
            out.append({"function": r["function"], "name": r["function"].rsplit(".", 1)[-1], "overloads": overloads,
                        "attached_ms": r["attached_ms"]})
            self.shares._count(row["id"], f"function:{r['function']}"[:512], loads=1)
        if out and self.audit is not None:
            self.audit.record("shares", "share.functions_fetched", f"urn:li:corpuser:{PREFIX}{row['id']}", None,
                              {"share": row["id"], "name": row["name"], "recipient": row["recipient"],
                               "functions": [f["function"] for f in out]})
        return out


def build_share_functions_router(sf: ShareFunctions, current_principal, authorize, groups_of) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/v1/shares", tags=["shares"])

    def operator(p) -> bool:
        return bool(authorize(p, "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False))

    def person(p):
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "shares are managed by people")
        return p

    def call(fn, *a):
        try:
            return fn(*a)
        except ShareError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/recipient/functions")
    def recipient_functions(request: Request):
        """The share's functions, with their definitions: for the recipient, with the share's credential."""
        try:
            row = sf.recipient(request.headers.get("authorization", ""))
        except ShareError as e:
            headers = {"WWW-Authenticate": 'Bearer realm="shares"'} if e.status == 401 else None
            raise HTTPException(e.status, str(e), headers=headers)
        return {"functions": call(sf.recipient_functions, row)}

    @router.get("/{ref}/functions")
    def list_(ref: str, p=Depends(current_principal)):
        return call(sf.list, p.urn, operator(p), ref)

    @router.post("/{ref}/functions")
    def attach(ref: str, function: str = Body(..., embed=True), p=Depends(current_principal)):
        """Adds a stored function's definition to the share (again: as it is now). Needs the share (its maker, or
        an operator) and MANAGE_DATASET on the function."""
        p = person(p)
        return call(sf.attach, p.urn, list(groups_of(p.urn) or []), operator(p), ref, function)

    @router.delete("/{ref}/functions/{function}")
    def detach(ref: str, function: str, p=Depends(current_principal)):
        p = person(p)
        return call(sf.detach, p.urn, operator(p), ref, function)

    return router
