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

**Differential privacy** (queue row 25). A room made with ``privacy: {epsilon, budget}`` releases only noisy
answers. Under it a template's results are group keys, ``count(...)`` and ``sum(column)``, and every summed column
has declared ``bounds`` (``{"b.spend": [0, 100]}``). Before the run, Cairn clips each summed value to its bounds
and takes ``epsilon`` from the room's ``budget``. It refuses the run once the budget is spent, and spends before
running, so a failed run costs too. The epsilon is shared between the result's aggregates, and each gets Laplace
noise scaled to its sensitivity (1 for a count, the larger bound for a sum). Counts come back as whole numbers of
at least 0, and sums rounded to cents.

The sensitivities assume each person is at most one row of the joined result: join on a key that is unique per
person. The k floor still applies, to the true counts. It is defence in depth on top of the noise, and which
groups appear is not itself noised, so group keys should be public values (regions, campaigns).

**Outside parties** (queue row 25). A party may be an organisation outside Cairn, named ``partner:<name>``. When
the room is made, each one gets a secret, shown once and kept only as a hash. With it (HTTP Basic: the name after
``partner:``, and the secret) the partner reads the room and proposes, approves and runs templates at ``/v1/clean-rooms/<room>/
partner/...``. It has no lakehouse tables, so it uploads its data instead (CSV or Parquet, ``PUT
.../partner/data/<table>``). Cairn writes the file to ``<catalog>.cleanrooms.<room>__<partner>__<table>``, which
only the room's identity reads; an upload replaces that table's earlier contents. A room is always made by a
Cairn party, and needs one.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import secrets
import time
import uuid
from typing import Any, Callable, Optional

try:  # module level: with postponed annotations FastAPI resolves the router's parameter types from here
    from fastapi import Request
    from fastapi.security import HTTPBasic, HTTPBasicCredentials
except ImportError:  # the rooms themselves work without the server extra
    Request = HTTPBasic = HTTPBasicCredentials = None  # type: ignore[assignment,misc]
from sqlalchemy import BigInteger, Column, Float, Integer, String, Table, Text, select, update

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
    Column("privacy", Text, nullable=True),         # JSON {epsilon, budget}: differentially private answers only
    Column("epsilon_spent", Float, nullable=False, server_default="0"),
    Column("partners", Text, nullable=True),        # JSON {partner:<name>: sha256 of its secret}
)

NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
TABLE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+$")
IDENTITY_PREFIX = "cleanroom-"
PARTNER = re.compile(r"^partner:[a-z0-9][a-z0-9-]{1,39}$")
MAX_UPLOAD = 100 * 1024 * 1024


class RoomError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class CleanRooms:
    def __init__(self, engine, run: Callable[[str, str], tuple[list[str], list[list[Any]]]],
                 member_of: Callable[[str, str], bool], may_read: Callable[[str, str], bool], audit=None,
                 default_catalog: str = "lakehouse", write_table: Optional[Callable[[str, Any], None]] = None):
        """``run(actor, sql)``: a statement on Trino as ``actor``; ``member_of(actor, party)``: is the actor that
        party (itself, or in that group); ``may_read(actor, table)``: may the actor read the table;
        ``write_table("cleanrooms.<table>", arrow_table)``: replaces a lakehouse table's contents (partners' uploads)."""
        self.engine, self.run, self.member_of, self.may_read, self.audit = engine, run, member_of, may_read, audit
        self.default_catalog, self.write_table = default_catalog, write_table

    # ---- rooms ---------------------------------------------------------------------------
    def _row(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(rooms).where(rooms.c.name == name)).first()
        if r is None:
            raise RoomError("no such clean room", 404)
        d = dict(r._mapping)
        for k in ("parties", "tables", "templates"):
            d[k] = json.loads(d[k])
        d["privacy"] = json.loads(d["privacy"]) if d.get("privacy") else None
        d["partners"] = json.loads(d["partners"]) if d.get("partners") else {}
        return d

    def _party(self, room: dict, actor: str) -> str:
        if actor.startswith("partner:"):                    # signed in with the room's partner secret
            if actor in room["parties"]:
                return actor
            raise RoomError(f"you are not a party to {room['name']}", 403)
        for p in room["parties"]:
            if p == actor or (not p.startswith("partner:") and self.member_of(actor, p)):
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
        if not isinstance(parties, list) or len(set(parties)) < 2 or \
                not all(str(p).startswith("urn:li:corp") or PARTNER.match(str(p)) for p in parties):
            raise RoomError("parties: at least two, each a Cairn user or group, or partner:<name> for an outside party")
        if not any(str(p).startswith("urn:li:corp") for p in parties):
            raise RoomError("parties: at least one Cairn user or group")
        if isinstance(k, bool) or not isinstance(k, int) or not 2 <= k <= 1_000_000:
            raise RoomError("min_group is 2 or more")
        if actor not in parties and not any(self.member_of(actor, p) for p in parties):
            raise RoomError("a room is made by one of its parties", 403)
        privacy = _privacy(body.get("privacy"))
        issued = {p: secrets.token_urlsafe(32) for p in sorted(set(parties)) if p.startswith("partner:")}
        row = {"id": uuid.uuid4().hex, "name": name, "parties": json.dumps(sorted(set(parties))), "min_group": k,
               "tables": "{}", "templates": "{}", "created_by": actor, "created_ms": int(time.time() * 1000),
               "privacy": json.dumps(privacy) if privacy else None, "epsilon_spent": 0.0,
               "partners": json.dumps({p: _digest(v) for p, v in issued.items()})}
        try:
            with self.engine.begin() as conn:
                conn.execute(rooms.insert().values(**row))
                if self.audit is not None:
                    self.audit.record("sharing", "clean_room.created", actor, f"urn:li:cleanRoom:{name}",
                                      {"parties": sorted(set(parties)), "min_group": k, "privacy": privacy}, conn=conn)
        except IntegrityError:
            raise RoomError("a clean room of that name exists", 409)
        out = self.get(actor, name)
        if issued:   # shown this once: give each partner its own; Cairn keeps only their hashes
            out["partner_secrets"] = issued
        return out

    def partner(self, name: str, party: str, secret: str) -> str:
        """The actor a partner acts as, once its secret checks out (401 otherwise, the same for every reason)."""
        try:
            room = self._row(name)
        except RoomError:
            room = None
        want = (room or {}).get("partners", {}).get(party)
        if not (want and secret and hmac.compare_digest(want, _digest(secret))):
            raise RoomError("not signed in as a partner of this room", 401)
        return party

    def get(self, actor: str, name: str) -> dict:
        room = self._row(name)
        self._party(room, actor)
        return {k: v for k, v in room.items() if k not in ("id", "partners")}

    def upload(self, actor: str, name: str, table: str, data: bytes, fmt: str) -> dict:
        """A partner's data, as a table only the room reads (replacing what it uploaded there before)."""
        import pyarrow as pa
        import pyarrow.csv as pacsv
        import pyarrow.parquet as pq

        room = self._row(name)
        party = self._party(room, actor)
        if not party.startswith("partner:"):
            raise RoomError("Cairn parties bring their tables (POST .../tables); uploads are for outside parties", 403)
        if self.write_table is None:
            raise RoomError("uploads need a lakehouse catalog to write to (CAIRN_PIPES_CATALOG)", 503)
        if not NAME.match(table):
            raise RoomError("table name: lowercase letters, digits and _")
        if not data or len(data) > MAX_UPLOAD:
            raise RoomError(f"an upload is 1 byte to {MAX_UPLOAD // (1024 * 1024)} MB", 413)
        try:
            arrow = pq.read_table(io.BytesIO(data)) if fmt == "parquet" else pacsv.read_csv(io.BytesIO(data))
        except (pa.ArrowInvalid, OSError) as e:
            raise RoomError(f"the {fmt} file does not read: {str(e)[:200]}")
        cols = [c.lower() for c in arrow.column_names]
        if len(set(cols)) != len(cols) or not all(NAME.match(c) for c in cols):
            raise RoomError("column names: lowercase letters, digits and _, each once")
        arrow = arrow.rename_columns(cols)
        local = f"{name}__{party.split(':', 1)[1].replace('-', '_')}__{table}"
        full = f"{self.default_catalog}.cleanrooms.{local}"
        # Names joined with "__" can coincide (room a__b's partner c and room a's partner b--c): never write over
        # a table another party or another room holds.
        if room["tables"].get(full) not in (None, party) or self._held_elsewhere(name, full):
            raise RoomError(f"the table name {local} is taken by another party's upload: choose another name", 409)
        self.write_table(f"cleanrooms.{local}", arrow)
        room["tables"][full] = party
        self._save(room, "clean_room.data_uploaded", actor, {"table": full, "rows": arrow.num_rows,
                                                             "columns": cols, "bytes": len(data)})
        return {"table": full, "rows": arrow.num_rows, "columns": cols}

    def _held_elsewhere(self, name: str, full: str) -> bool:
        """Is ``full`` among another room's tables."""
        with self.engine.connect() as conn:
            others = conn.execute(select(rooms.c.tables).where(rooms.c.name != name)).all()
        return any(full in json.loads(t or "{}") for (t,) in others)

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
    def _check_sql(self, room: dict, sql: str, params: list[dict], bounds: Optional[dict] = None) -> None:
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
        if room.get("privacy"):
            _dp_outputs(tree, bounds or {})
            # Only the noised result leaves: a HAVING, ORDER BY or LIMIT acts on the true aggregates (which groups
            # appear, and in what order), and an aggregate in a subquery or CTE is released un-noised.
            if any(tree.args.get(k) is not None for k in ("having", "order", "limit", "offset")):
                raise RoomError("under differential privacy a template has no HAVING, ORDER BY or LIMIT "
                                "(they act on the true answers): sort and filter the noisy result instead")
            released = {id(a) for e in tree.expressions for a in e.find_all(exp.AggFunc)}
            if any(id(a) not in released for a in tree.find_all(exp.AggFunc)):
                raise RoomError("under differential privacy aggregates are only in the template's result "
                                "(not in subqueries, CTEs or WHERE)")

    def _bounds(self, room: dict, body: dict) -> dict:
        bounds = body.get("bounds") or {}
        if not isinstance(bounds, dict):
            raise RoomError("bounds: {column: [lowest, highest]}")
        out = {}
        for col, b in bounds.items():
            ok = isinstance(b, list) and len(b) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in b)
            if not ok or not b[0] < b[1] or max(abs(b[0]), abs(b[1])) > 1e12:
                raise RoomError(f"bounds of {col}: [lowest, highest], lowest below highest")
            out[str(col).lower()] = [float(b[0]), float(b[1])]
        return out

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
        bounds = self._bounds(room, body)
        self._check_sql(room, sql, params, bounds)
        old = room["templates"].get(tname)
        same = old is not None and old["sql"] == sql and old["params"] == params and old.get("bounds", {}) == bounds
        room["templates"][tname] = {"sql": sql, "params": params, "bounds": bounds, "proposed_by": party,
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
        self._check_sql(room, t["sql"], t["params"], t.get("bounds"))   # the room's tables may have changed since
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
        privacy, noise = room.get("privacy"), None
        if privacy:
            outputs = _dp_outputs(tree, t.get("bounds") or {}, clip=True)
            self._spend(room, privacy["epsilon"])               # before running: a failed run costs too
        sql = tree.sql(dialect="trino")
        identity = f"urn:li:corpuser:{IDENTITY_PREFIX}{name}"
        columns, rows = self.run(identity, sql)
        result = {"room": name, "template": tname, "columns": columns, "rows": rows, "min_group": room["min_group"]}
        if privacy:
            rows, noise = _add_noise(rows, outputs, privacy["epsilon"])
            spent = self._row(name)["epsilon_spent"]
            result.update(rows=rows, epsilon=privacy["epsilon"], epsilon_spent=round(spent, 6),
                          epsilon_left=round(privacy["budget"] - spent, 6), noise_scale=noise)
        if self.audit is not None:
            self.audit.record("sharing", "clean_room.run", actor, f"urn:li:cleanRoom:{name}",
                              {"template": tname, "party": party, "args": args, "rows": len(rows),
                               **({"epsilon": privacy["epsilon"]} if privacy else {})})
        return result

    def _spend(self, room: dict, epsilon: float) -> None:
        """Takes epsilon from the room's budget, atomically: two runs cannot both take the last of it."""
        budget = room["privacy"]["budget"]
        with self.engine.begin() as conn:
            done = conn.execute(update(rooms).where(rooms.c.id == room["id"])
                                .where(rooms.c.epsilon_spent + epsilon <= budget + 1e-9)
                                .values(epsilon_spent=rooms.c.epsilon_spent + epsilon)).rowcount
        if not done:
            raise RoomError(f"the room's privacy budget is spent (epsilon {budget}): no more answers from it", 409)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def catalog_writer(props: dict) -> Callable[[str, Any], None]:
    """``write_table`` over a pyiceberg REST catalog: creates ``namespace.table`` when missing, replaces its rows
    (and its columns, when an upload brings other ones than the last)."""
    def write(target: str, data) -> None:
        from pyiceberg.catalog.rest import RestCatalog

        from cairn.pipelines.pipes import open_table

        cat = RestCatalog("cairn-cleanrooms", **props)
        try:
            open_table(cat, target, data.schema, True).overwrite(data)
        except ValueError:                      # pyiceberg: the data's schema is not the table's
            cat.purge_table(tuple(target.split(".")))
            open_table(cat, target, data.schema, True).overwrite(data)
    return write


def _privacy(raw) -> Optional[dict]:
    if raw in (None, {}, False):
        return None
    if not isinstance(raw, dict):
        raise RoomError("privacy: {epsilon, budget}")
    try:
        eps, budget = float(raw.get("epsilon")), float(raw.get("budget"))
    except (TypeError, ValueError):
        raise RoomError("privacy: epsilon and budget are numbers")
    if not 0 < eps <= 10:
        raise RoomError("privacy.epsilon: above 0, at most 10 (smaller is more private; 0.1 to 1 is usual)")
    if not eps <= budget <= 1000:
        raise RoomError("privacy.budget: at least one run's epsilon, at most 1000")
    return {"epsilon": eps, "budget": budget}


def _dp_outputs(tree, bounds: dict, clip: bool = False) -> list[Optional[tuple[str, float]]]:
    """Under differential privacy: for each result column, None (a group key) or (kind, sensitivity) of its
    count or bounded sum; with ``clip``, rewrites each summed value to its bounds."""
    from sqlglot import exp

    out: list[Optional[tuple[str, float]]] = []
    for e in tree.expressions:
        inner = e.this if isinstance(e, exp.Alias) else e
        if inner.find(exp.AggFunc) is None:
            out.append(None)
            continue
        if isinstance(inner, exp.Count) and not inner.args.get("distinct") and not isinstance(inner.this, exp.Distinct):
            out.append(("count", 1.0))
            continue
        if isinstance(inner, exp.Sum) and isinstance(inner.this, exp.Column):
            col = inner.this.sql(dialect="trino").lower()
            b = bounds.get(col) or bounds.get(inner.this.name.lower())
            if b is None:
                raise RoomError(f"sum({col}) needs bounds: the room is differentially private")
            if clip:
                inner.set("this", exp.func("least", exp.func("greatest", inner.this.copy(), exp.Literal.number(b[0])),
                                            exp.Literal.number(b[1])))
            out.append(("sum", max(abs(b[0]), abs(b[1]))))
            continue
        raise RoomError("under differential privacy a template's results are group keys, count(...) and "
                        f"sum(column); not {inner.sql(dialect='trino')[:80]}")
    if not any(out):
        raise RoomError("a template's result is an aggregate (count, sum)")
    return out


def _laplace(scale: float) -> float:
    """Laplace noise from the operating system's random source (not a seeded generator)."""
    import math
    import secrets

    u = (secrets.randbits(53) + 0.5) / (1 << 53) - 0.5          # in (-0.5, 0.5), never exactly 0 at the ends
    return -scale * math.copysign(1.0, u) * math.log(1 - 2 * abs(u))


def _add_noise(rows: list[list], outputs: list, epsilon: float) -> tuple[list[list], dict]:
    n = sum(1 for o in outputs if o)
    each = epsilon / n                                         # sequential composition across the aggregates
    scales = {i: o[1] / each for i, o in enumerate(outputs) if o}
    noisy = []
    for r in rows:
        r = list(r)
        for i, o in enumerate(outputs):
            if not o or r[i] is None:
                continue
            v = float(r[i]) + _laplace(scales[i])
            r[i] = max(0, round(v)) if o[0] == "count" else round(v, 2)
        noisy.append(r)
    return noisy, {str(i): round(sc, 6) for i, sc in scales.items()}


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

    # ---- outside parties: HTTP Basic, the partner's name (after partner:) and the secret it was given ------
    basic = HTTPBasic(auto_error=False)

    def partner(name: str, cred: Optional[HTTPBasicCredentials] = Depends(basic)) -> str:
        if cred is None:
            raise HTTPException(401, "sign in as the room's partner (HTTP Basic)", headers={"WWW-Authenticate": "Basic"})
        try:   # the username is the partner's name without "partner:" (a Basic username cannot hold a colon)
            return cr.partner(name, f"partner:{cred.username}", cred.password)
        except RoomError as e:
            raise HTTPException(e.status, str(e), headers={"WWW-Authenticate": "Basic"})

    @router.get("/{name}/partner")
    async def p_get(name: str, who: str = Depends(partner)):
        return await call(cr.get, who, name)

    @router.put("/{name}/partner/templates/{tname}")
    async def p_propose(name: str, tname: str, body: dict = Body(...), who: str = Depends(partner)):
        return await call(cr.propose, who, name, {**body, "name": tname})

    @router.post("/{name}/partner/templates/{tname}/approve")
    async def p_approve(name: str, tname: str, who: str = Depends(partner)):
        return await call(cr.approve, who, name, tname)

    @router.post("/{name}/partner/templates/{tname}/run")
    async def p_run(name: str, tname: str, body: dict = Body(default={}), who: str = Depends(partner)):
        return await call(cr.run_template, who, name, tname, (body or {}).get("args") or {})

    @router.put("/{name}/partner/data/{table}")
    async def p_upload(name: str, table: str, request: Request, who: str = Depends(partner)):
        kind = (request.headers.get("content-type") or "").split(";")[0].strip()
        fmt = {"text/csv": "csv", "application/vnd.apache.parquet": "parquet"}.get(kind)
        if fmt is None:
            raise HTTPException(415, "send text/csv or application/vnd.apache.parquet")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, "the upload is too large")
        return await call(cr.upload, who, name, table, bytes(data), fmt)

    return router
