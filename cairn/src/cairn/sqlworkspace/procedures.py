"""Stored procedures: named, parameterized SQL scripts, run on Trino as whoever calls them.

``POST /v1/procedures`` (a person): ``name``, ``connection`` (a Trino connection), ``params``
(``[{"name", "type"}]``, types ``integer``, ``double``, ``varchar``, ``boolean``, ``date``), ``statements``
(SQL statements run in order; ``:name`` is a parameter) and ``description``. Each statement is parsed when the
procedure is saved: one statement each, and only declared parameters.

``POST /v1/procedures/<name>/call`` with ``{"args": {...}}`` checks and types each argument, binds it as a SQL
literal (never as text spliced into SQL), and runs the statements one after another **as the caller**, so
Cairn's policies (access, masks, row filters, write rights) apply to every statement as they would to the
caller's own query. A statement that fails stops the call; what ran before it stays. The last statement's
rows are returned (up to 1,000).

Who may call: the procedure's owner, and those Cairn's policies allow ``EXECUTE_PROCEDURE`` on
``urn:li:procedure:<name>``. A procedure never runs with its owner's rights: what it does, the caller could
do anyway. Each call is audited with its arguments.
"""

from __future__ import annotations

import datetime
import json
import math
import re
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, String, Table, Text, delete, select, update

from cairn.common.db import metadata
from cairn.sqlworkspace.workspace import WorkspaceError

procedures = Table(
    "cairn_procedures", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(128), nullable=False, unique=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("connection", String(128), nullable=False),
    Column("description", String(2000), nullable=False, default=""),
    Column("params", Text, nullable=False),
    Column("statements", Text, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

NAME = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
TYPES = ("integer", "double", "varchar", "boolean", "date")
PRIVILEGE = "EXECUTE_PROCEDURE"


def _placeholders(tree) -> set[str]:
    from sqlglot import exp

    return {str(p.name) for p in tree.find_all(exp.Placeholder) if p.name}


def _parse(sql: str):
    import sqlglot

    try:
        trees = [t for t in sqlglot.parse(sql, read="trino") if t is not None]
    except Exception as e:  # noqa: BLE001 - sqlglot raises several kinds
        raise WorkspaceError(f"a statement does not parse: {str(e)[:300]}")
    if len(trees) != 1:
        raise WorkspaceError("each statement is one SQL statement")
    return trees[0]


def literal(kind: str, value: Any):
    """An argument as a SQL literal of its declared type."""
    from sqlglot import exp

    if value is None:
        return exp.Cast(this=exp.Null(), to=exp.DataType.build(kind))
    if kind == "integer":
        if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, str) and re.fullmatch(r"-?\d{1,18}", value))):
            raise WorkspaceError("an integer")
        return exp.Literal.number(int(value))
    if kind == "double":
        if isinstance(value, bool):
            raise WorkspaceError("a number")
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise WorkspaceError("a number")
        if not math.isfinite(v):
            raise WorkspaceError("a finite number")
        return exp.Literal.number(repr(v))
    if kind == "boolean":
        if not isinstance(value, bool):
            raise WorkspaceError("true or false")
        return exp.Boolean(this=value)
    if kind == "date":
        try:
            d = datetime.date.fromisoformat(str(value))
        except ValueError:
            raise WorkspaceError("a date (YYYY-MM-DD)")
        return exp.Cast(this=exp.Literal.string(d.isoformat()), to=exp.DataType.build("date"))
    v = str(value)
    if "\x00" in v or len(v) > 65535:
        raise WorkspaceError("text without NUL characters, at most 65,535 characters")
    return exp.Literal.string(v)


class Procedures:
    def __init__(self, engine, run: Callable[[str, str, str], tuple[list[str], list[list[Any]]]],
                 may_call: Callable[[str, str], bool], audit=None):
        """``run(connection, actor, sql) -> (columns, rows)`` runs one statement on Trino as ``actor``;
        ``may_call(actor, urn)``: Cairn's EXECUTE_PROCEDURE decision."""
        self.engine, self.run, self.may_call, self.audit = engine, run, may_call, audit

    @staticmethod
    def urn(name: str) -> str:
        return f"urn:li:procedure:{name}"

    def _check(self, body: dict) -> dict:
        name = str(body.get("name") or "")
        if not NAME.match(name):
            raise WorkspaceError("name: lowercase letters, digits and _ (starting with a letter)")
        connection = str(body.get("connection") or "")
        if not connection:
            raise WorkspaceError("connection: the Trino connection it runs on")
        params = body.get("params") or []
        if not isinstance(params, list) or len(params) > 50:
            raise WorkspaceError("params: at most 50, each {name, type}")
        declared: dict[str, str] = {}
        for p in params:
            if not isinstance(p, dict) or not NAME.match(str(p.get("name") or "")) or p.get("type") not in TYPES:
                raise WorkspaceError(f"each param is {{name, type}}, type one of {', '.join(TYPES)}")
            if p["name"] in declared:
                raise WorkspaceError(f"param {p['name']} is declared twice")
            declared[p["name"]] = p["type"]
        stmts = body.get("statements")
        if isinstance(stmts, str):
            stmts = [stmts]
        if not isinstance(stmts, list) or not 1 <= len(stmts) <= 50 or not all(isinstance(x, str) and x.strip() for x in stmts):
            raise WorkspaceError("statements: 1 to 50 SQL statements")
        used: set[str] = set()
        for x in stmts:
            names = _placeholders(_parse(x))
            unknown = names - set(declared)
            if unknown:
                raise WorkspaceError(f"undeclared parameter :{sorted(unknown)[0]}")
            used |= names
        return {"name": name, "connection": connection, "description": str(body.get("description") or "")[:2000],
                "params": json.dumps([{"name": n, "type": t} for n, t in declared.items()]),
                "statements": json.dumps([x.strip().rstrip(";") for x in stmts])}

    @staticmethod
    def _public(r) -> dict:
        return {"id": r.id, "name": r.name, "owner": r.owner, "connection": r.connection, "description": r.description,
                "params": json.loads(r.params), "statements": json.loads(r.statements), "updated_ms": r.updated_ms}

    def create(self, actor: str, body: dict) -> dict:
        from sqlalchemy.exc import IntegrityError

        values = self._check(body)
        try:
            with self.engine.begin() as conn:
                conn.execute(procedures.insert().values(id=uuid.uuid4().hex, owner=actor, updated_ms=int(time.time() * 1000), **values))
                if self.audit is not None:
                    self.audit.record("sql", "procedure.created", actor, self.urn(values["name"]), values, conn=conn)
        except IntegrityError:
            raise WorkspaceError("a procedure of that name exists", 409)
        return self.get(values["name"])

    def get(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(procedures).where(procedures.c.name == name)).first()
        if r is None:
            raise WorkspaceError("no such procedure", 404)
        return self._public(r)

    def list(self, actor: str) -> list[dict]:
        """The procedures the caller owns or may call."""
        with self.engine.connect() as conn:
            rows = [self._public(r) for r in conn.execute(select(procedures).order_by(procedures.c.name))]
        return [p for p in rows if p["owner"] == actor or self.may_call(actor, self.urn(p["name"]))]

    def update(self, actor: str, name: str, body: dict) -> dict:
        p = self.get(name)
        if p["owner"] != actor:
            raise WorkspaceError("only its owner changes a procedure", 403)
        values = self._check({**body, "name": name})
        with self.engine.begin() as conn:
            conn.execute(update(procedures).where(procedures.c.id == p["id"]).values(updated_ms=int(time.time() * 1000), **values))
            if self.audit is not None:
                self.audit.record("sql", "procedure.updated", actor, self.urn(name), values, conn=conn)
        return self.get(name)

    def delete(self, actor: str, name: str) -> None:
        p = self.get(name)
        if p["owner"] != actor:
            raise WorkspaceError("only its owner deletes a procedure", 403)
        with self.engine.begin() as conn:
            conn.execute(delete(procedures).where(procedures.c.id == p["id"]))
            if self.audit is not None:
                self.audit.record("sql", "procedure.deleted", actor, self.urn(name), {}, conn=conn)

    def bind(self, p: dict, args: dict) -> list[str]:
        """The statements with the arguments bound as typed literals."""
        from sqlglot import exp

        if not isinstance(args, dict):
            raise WorkspaceError("args: {name: value}")
        types = {x["name"]: x["type"] for x in p["params"]}
        extra = set(args) - set(types)
        if extra:
            raise WorkspaceError(f"{p['name']} has no parameter {sorted(extra)[0]}")
        missing = set(types) - set(args)
        if missing:
            raise WorkspaceError(f"{p['name']} needs {', '.join(sorted(missing))}")
        values = {}
        for n, t in types.items():
            try:
                values[n] = literal(t, args[n])
            except WorkspaceError as e:
                raise WorkspaceError(f"{n}: {e}")
        out = []
        for sql in p["statements"]:
            tree = _parse(sql).transform(
                lambda node: values[node.name].copy() if isinstance(node, exp.Placeholder) and node.name in values else node)
            out.append(tree.sql(dialect="trino"))
        return out

    def call(self, actor: str, name: str, args: Optional[dict] = None) -> dict:
        p = self.get(name)
        if p["owner"] != actor and not self.may_call(actor, self.urn(name)):
            raise WorkspaceError(f"calling {name} needs {PRIVILEGE} on it", 403)
        stmts = self.bind(p, args or {})
        started = time.monotonic()
        done, columns, rows, error = 0, [], [], None
        for sql in stmts:
            try:
                columns, rows = self.run(p["connection"], actor, sql)
            except Exception as e:  # noqa: BLE001 - the statement's error is the call's
                error = f"statement {done + 1}: {str(e)[:1000]}"
                break
            done += 1
        out = {"procedure": name, "statements": len(stmts), "ran": done, "ms": int((time.monotonic() - started) * 1000),
               "columns": columns, "rows": rows[:1000], "error": error}
        if self.audit is not None:
            self.audit.record("sql", "procedure.called", actor, self.urn(name),
                              {"args": args or {}, "ran": done, "error": error})
        if error:
            raise WorkspaceError(error, 400)
        return out


def build_procedures_router(procs: Procedures, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/procedures", tags=["sql"])

    def person(p):
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "procedures are made and called by people and pipeline users")
        return p.urn

    async def call(fn, *a):
        try:
            return await run_in_threadpool(fn, *a)
        except WorkspaceError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        return await call(procs.list, person(p))

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        return await call(procs.create, person(p), body)

    @router.get("/{name}")
    async def get(name: str, p=Depends(current_principal)):
        actor = person(p)
        got = await call(procs.get, name)
        if got["owner"] != actor and not procs.may_call(actor, procs.urn(name)):
            raise HTTPException(404, "no such procedure")
        return got

    @router.put("/{name}")
    async def change(name: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(procs.update, person(p), name, body)

    @router.delete("/{name}", status_code=204)
    async def remove(name: str, p=Depends(current_principal)):
        await call(procs.delete, person(p), name)

    @router.post("/{name}/call")
    async def call_(name: str, body: dict = Body(default={}), p=Depends(current_principal)):
        return await call(procs.call, person(p), name, (body or {}).get("args") or {})

    return router
