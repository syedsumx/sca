"""Data clean rooms: parties join their tables in aggregate, without seeing each other's rows.

A room (``POST /v1/clean-rooms``) names its ``parties`` (Cairn users or groups) and ``min_group`` (k: the
smallest group a result may describe, default 10). Each party contributes tables it may read itself
(``POST .../tables``). Queries are **templates**: any party proposes one (SQL over the room's tables, with
``:params``), and it runs only once every party has approved its current text; changing the text clears the
approvals. Cairn checks a template when it is proposed:

* it reads only the room's tables;
* its result is an aggregate (``GROUP BY``, or aggregates only), with no ``SELECT *``;
* Cairn adds ``HAVING count(*) >= k`` itself, so no group smaller than k is released, whatever the template.

A run (``POST .../templates/<name>/run``) binds the arguments as typed literals and runs on Trino as the
room's own identity (``cleanroom-<room>``), which Cairn's access control lets read the contributed tables and
nothing else: not the parties' other tables, and the parties never read each other's contributed tables
directly. Every proposal, approval and run is audited with who asked.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, select, update

from cairn.common.db import metadata

rooms = Table(
    "cairn_clean_rooms", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(64), nullable=False, unique=True),
    Column("parties", Text, nullable=False),        # JSON list of urns
    Column("min_group", Integer, nullable=False),
    Column("tables", Text, nullable=False),         # JSON {"catalog.schema.table": party urn}
    Column("templates", Text, nullable=False),      # JSON {name: {sql, params, proposed_by, approved_by}}
    Column("created_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)

NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
TABLE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+$")
IDENTITY_PREFIX = "cleanroom-"


class RoomError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class CleanRooms:
    def __init__(self, engine, run: Callable[[str, str], tuple[list[str], list[list[Any]]]],
                 member_of: Callable[[str, str], bool], may_read: Callable[[str, str], bool], audit=None,
                 default_catalog: str = "lakehouse"):
        """``run(actor, sql)``: a statement on Trino as ``actor``; ``member_of(actor, party)``: is the actor that
        party (itself, or in that group); ``may_read(actor, table)``: may the actor read the table."""
        self.engine, self.run, self.member_of, self.may_read, self.audit = engine, run, member_of, may_read, audit
        self.default_catalog = default_catalog

    # ---- rooms ---------------------------------------------------------------------------
    def _row(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(rooms).where(rooms.c.name == name)).first()
        if r is None:
            raise RoomError("no such clean room", 404)
        d = dict(r._mapping)
        for k in ("parties", "tables", "templates"):
            d[k] = json.loads(d[k])
        return d

    def _party(self, room: dict, actor: str) -> str:
        for p in room["parties"]:
            if p == actor or self.member_of(actor, p):
                return p
        raise RoomError(f"you are not a party to {room['name']}", 403)

    def _save(self, room: dict, action: str, actor: str, details: dict) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(rooms).where(rooms.c.id == room["id"]).values(
                tables=json.dumps(room["tables"]), templates=json.dumps(room["templates"])))
            if self.audit is not None:
                self.audit.record("sharing", action, actor, f"urn:li:cleanRoom:{room['name']}", details, conn=conn)

    def create(self, actor: str, body: dict) -> dict:
        from sqlalchemy.exc import IntegrityError

        name, parties = str(body.get("name") or ""), body.get("parties")
        k = body.get("min_group", 10)
        if not NAME.match(name):
            raise RoomError("name: lowercase letters, digits and _")
        if not isinstance(parties, list) or len(set(parties)) < 2 or not all(str(p).startswith("urn:li:corp") for p in parties):
            raise RoomError("parties: at least two Cairn users or groups")
        if isinstance(k, bool) or not isinstance(k, int) or not 2 <= k <= 1_000_000:
            raise RoomError("min_group is 2 or more")
        if actor not in parties and not any(self.member_of(actor, p) for p in parties):
            raise RoomError("a room is made by one of its parties", 403)
        row = {"id": uuid.uuid4().hex, "name": name, "parties": json.dumps(sorted(set(parties))), "min_group": k,
               "tables": "{}", "templates": "{}", "created_by": actor, "created_ms": int(time.time() * 1000)}
        try:
            with self.engine.begin() as conn:
                conn.execute(rooms.insert().values(**row))
                if self.audit is not None:
                    self.audit.record("sharing", "clean_room.created", actor, f"urn:li:cleanRoom:{name}",
                                      {"parties": sorted(set(parties)), "min_group": k}, conn=conn)
        except IntegrityError:
            raise RoomError("a clean room of that name exists", 409)
        return self.get(actor, name)

    def get(self, actor: str, name: str) -> dict:
        room = self._row(name)
        self._party(room, actor)
        return {k: v for k, v in room.items() if k != "id"}

    def tables_of(self, name: str) -> set[str]:
        """The tables the room's identity may read (catalog.schema.table)."""
        try:
            return set(self._row(name)["tables"])
        except RoomError:
            return set()

    def contribute(self, actor: str, name: str, table: str) -> dict:
        room = self._row(name)
        party = self._party(room, actor)
        table = table.lower()
        if table.count(".") == 1:
            table = f"{self.default_catalog}.{table}"
        if not TABLE.match(table):
            raise RoomError("table: catalog.schema.table")
        if not self.may_read(actor, table):
            raise RoomError(f"you may not read {table}, so you may not bring it", 403)
        room["tables"][table] = party
        self._save(room, "clean_room.table_added", actor, {"table": table, "party": party})
        return self.get(actor, name)

    # ---- templates --------------------------------------------------------------------------
    def _check_sql(self, room: dict, sql: str, params: list[dict]) -> None:
        import sqlglot
        from sqlglot import exp

        try:
            trees = [t for t in sqlglot.parse(sql, read="trino") if t is not None]
        except Exception as e:  # noqa: BLE001
            raise RoomError(f"the template does not parse: {str(e)[:200]}")
        if len(trees) != 1 or not isinstance(trees[0], exp.Select):
            raise RoomError("a template is one SELECT")
        tree = trees[0]
        if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)) for e in tree.expressions):
            raise RoomError("no SELECT *: name what the result shows")
        has_agg = any(e.find(exp.AggFunc) is not None for e in tree.expressions)
        if not has_agg:
            raise RoomError("a template's result is an aggregate (count, sum, avg...), never rows")
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        for t in tree.find_all(exp.Table):
            if t.name.lower() in ctes:
                continue
            full = ".".join(p for p in (t.catalog or self.default_catalog, t.db, t.name) if p).lower()
            if full not in room["tables"]:
                raise RoomError(f"{full} is not in the room")
        declared = {p["name"] for p in params}
        used = {str(p.name) for p in tree.find_all(exp.Placeholder) if p.name}
        if used - declared:
            raise RoomError(f"undeclared parameter :{sorted(used - declared)[0]}")

    def propose(self, actor: str, name: str, body: dict) -> dict:
        from cairn.sqlworkspace.procedures import TYPES

        room = self._row(name)
        party = self._party(room, actor)
        tname, sql = str(body.get("name") or ""), str(body.get("sql") or "").strip().rstrip(";")
        params = body.get("params") or []
        if not NAME.match(tname):
            raise RoomError("template name: lowercase letters, digits and _")
        if not isinstance(params, list) or not all(isinstance(p, dict) and p.get("type") in TYPES for p in params):
            raise RoomError(f"params: [{{name, type}}], type one of {', '.join(TYPES)}")
        self._check_sql(room, sql, params)
        old = room["templates"].get(tname)
        same = old is not None and old["sql"] == sql and old["params"] == params
        room["templates"][tname] = {"sql": sql, "params": params, "proposed_by": party,
                                    "approved_by": old["approved_by"] if same else [party]}
        self._save(room, "clean_room.template_proposed", actor, {"template": tname, "sql": sql})
        return room["templates"][tname]

    def approve(self, actor: str, name: str, tname: str) -> dict:
        room = self._row(name)
        party = self._party(room, actor)
        t = room["templates"].get(tname)
        if t is None:
            raise RoomError("no such template", 404)
        if party not in t["approved_by"]:
            t["approved_by"].append(party)
        self._save(room, "clean_room.template_approved", actor, {"template": tname, "party": party})
        return t

    def run_template(self, actor: str, name: str, tname: str, args: Optional[dict] = None) -> dict:
        import sqlglot
        from sqlglot import exp

        from cairn.sqlworkspace.procedures import literal
        from cairn.sqlworkspace.workspace import WorkspaceError

        room = self._row(name)
        party = self._party(room, actor)
        t = room["templates"].get(tname)
        if t is None:
            raise RoomError("no such template", 404)
        waiting = sorted(set(room["parties"]) - set(t["approved_by"]))
        if waiting:
            raise RoomError(f"{tname} waits for approval by {', '.join(waiting)}", 409)
        self._check_sql(room, t["sql"], t["params"])         # the room's tables may have changed since
        args = args or {}
        types = {p["name"]: p["type"] for p in t["params"]}
        if set(args) != set(types):
            raise RoomError(f"{tname} takes {', '.join(sorted(types)) or 'no arguments'}")
        try:
            values = {n: literal(types[n], v) for n, v in args.items()}
        except WorkspaceError as e:
            raise RoomError(str(e))
        tree = sqlglot.parse_one(t["sql"], read="trino").transform(
            lambda node: values[node.name].copy() if isinstance(node, exp.Placeholder) and node.name in values else node)
        # Small groups are never released: Cairn's own HAVING, whatever the template says.
        floor = exp.GTE(this=exp.Count(this=exp.Star()), expression=exp.Literal.number(room["min_group"]))
        having = tree.args.get("having")
        tree.set("having", exp.Having(this=exp.and_(having.this, floor) if having else floor))
        sql = tree.sql(dialect="trino")
        identity = f"urn:li:corpuser:{IDENTITY_PREFIX}{name}"
        columns, rows = self.run(identity, sql)
        if self.audit is not None:
            self.audit.record("sharing", "clean_room.run", actor, f"urn:li:cleanRoom:{name}",
                              {"template": tname, "party": party, "args": args, "rows": len(rows)})
        return {"room": name, "template": tname, "columns": columns, "rows": rows, "min_group": room["min_group"]}


def build_clean_rooms_router(cr: CleanRooms, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/clean-rooms", tags=["sharing"])

    async def call(fn, *a):
        try:
            return await run_in_threadpool(fn, *a)
        except RoomError as e:
            raise HTTPException(e.status, str(e))

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        return await call(cr.create, p.urn, body)

    @router.get("/{name}")
    async def get(name: str, p=Depends(current_principal)):
        return await call(cr.get, p.urn, name)

    @router.post("/{name}/tables")
    async def add_table(name: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(cr.contribute, p.urn, name, str(body.get("table") or ""))

    @router.put("/{name}/templates/{tname}")
    async def propose(name: str, tname: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(cr.propose, p.urn, name, {**body, "name": tname})

    @router.post("/{name}/templates/{tname}/approve")
    async def approve(name: str, tname: str, p=Depends(current_principal)):
        return await call(cr.approve, p.urn, name, tname)

    @router.post("/{name}/templates/{tname}/run")
    async def run(name: str, tname: str, body: dict = Body(default={}), p=Depends(current_principal)):
        return await call(cr.run_template, p.urn, name, tname, (body or {}).get("args") or {})

    return router
