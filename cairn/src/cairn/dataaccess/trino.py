"""Trino access control through Cairn: Cairn's policy engine answers Trino's OPA access-control plugin.

Trino (``access-control.name=opa``) asks before every action. Cairn decides with the same policies,
masking plans and row filters that govern the catalog and its SQL editor:

- **Who.** The Trino user ``alice`` is ``urn:li:corpuser:alice``; Trino's groups add
  ``urn:li:corpGroup:<group>`` memberships.
- **Which dataset.** A Trino table ``catalog.schema.table`` is a catalog dataset. Its URN comes from the
  catalog's mapping (``CAIRN_TRINO_CATALOGS``, e.g. ``{"lakehouse": {"platform": "iceberg"}}``), by
  default ``urn:li:dataset:(urn:li:dataPlatform:trino,<catalog>.<schema>.<table>,PROD)``.
- **What it may do.**
  - Seeing a table (listings, ``SHOW COLUMNS``) needs ``VIEW_ENTITY_PAGE``.
  - Reading rows needs ``VIEW_DATASET_PROFILE``, as for the SQL editor.
  - Changing rows (``INSERT``, ``UPDATE``, ``DELETE``, ``MERGE``, ``TRUNCATE``) needs ``WRITE_DATASET``.
  - Changing tables, views, schemas and running table procedures (``optimize``) needs ``MANAGE_DATASET``.
  - Other users' queries and system information need ``MANAGE_SYSTEM_OPERATIONS``.
  - AI functions (the catalogs in ``CAIRN_TRINO_AI_CATALOGS``, default ``llm``: Trino's ``ai`` connector)
    send rows to a model outside the lakehouse, so calling one needs ``USE_AI_FUNCTIONS``. They see
    what the caller sees: masks and row filters apply before any function runs.
  - A warehouse (a Trino of a team's own, ``cairn.dataaccess.warehouses``) asks as itself
    (``/v1/trino/<key>/warehouse/<name>/...``): running a query there needs ``USE_WAREHOUSE`` on
    ``urn:li:warehouse:<name>``, granted to the team like any policy.
  - An action Cairn does not know is refused.
- **Masks and filters.** Columns the caller's masking plan covers get a Trino mask with the plan's strategy
  (``partial``, ``hash``, ``redact`` or ``null``). ``row_filter`` obligations (``{"expression": "region =
  'EU'"}``, with ``{user}`` standing for the Trino user as a quoted string) become row filters.

**Lineage.** Trino's OpenLineage event listener posts to ``/v1/trino/<key>/lineage``: each query that
writes becomes a run of a job named after the tables it writes (``trino:<catalog>.<schema>.<table>``), with
its inputs, outputs, column lineage and SQL, so repeated loads into a table share one job and its history.

Requests are refused unless they carry the shared key in the path: ``/v1/trino/<key>/allow``, so only
the Trino servers given ``CAIRN_TRINO_ACCESS_KEY`` can ask. Every refused table action is audited by the
policy engine like any other deny.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

AI = "USE_AI_FUNCTIONS"
WAREHOUSE = "USE_WAREHOUSE"
FUNCTION = "USE_FUNCTION"
VIEW, READ, WRITE, MANAGE, SYSTEM = ("VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE", "WRITE_DATASET", "MANAGE_DATASET",
                                     "MANAGE_SYSTEM_OPERATIONS")

# Table operations and the privilege each needs on the table's dataset.
TABLE_OPS: dict[str, str] = {
    "ShowColumns": VIEW, "ShowCreateTable": VIEW, "FilterColumns": VIEW, "FilterTables": VIEW,
    "SelectFromColumns": READ,
    "InsertIntoTable": WRITE, "DeleteFromTable": WRITE, "TruncateTable": WRITE, "UpdateTableColumns": WRITE,
    "CreateTable": MANAGE, "DropTable": MANAGE, "RenameTable": MANAGE, "SetTableProperties": MANAGE,
    "SetTableComment": MANAGE, "SetColumnComment": MANAGE, "AddColumn": MANAGE, "DropColumn": MANAGE,
    "RenameColumn": MANAGE, "AlterColumn": MANAGE, "SetTableAuthorization": MANAGE,
    "CreateView": MANAGE, "DropView": MANAGE, "RenameView": MANAGE, "SetViewComment": MANAGE,
    "SetViewAuthorization": MANAGE, "CreateViewWithSelectFromColumns": READ,
    "CreateMaterializedView": MANAGE, "DropMaterializedView": MANAGE, "RenameMaterializedView": MANAGE,
    "RefreshMaterializedView": WRITE, "SetMaterializedViewProperties": MANAGE,
    "ExecuteTableProcedure": MANAGE,
}
# Operations every signed-in Trino user may perform (what they reach is still filtered per table).
OPEN_OPS = {"ExecuteQuery", "AccessCatalog", "FilterCatalogs", "ShowSchemas", "FilterSchemas", "ShowTables",
            "ShowFunctions", "FilterFunctions", "SetSystemSessionProperty",
            "SetCatalogSessionProperty", "ShowCurrentRoles", "ShowRoleGrants", "ShowRoles"}
# Schema-level changes: MANAGE_DATASET without a particular dataset.
SCHEMA_OPS = {"CreateSchema", "DropSchema", "RenameSchema", "SetSchemaAuthorization"}
# Stored functions (in the catalogs of CAIRN_TRINO_ROUTINE_CATALOGS): created and dropped with MANAGE_DATASET
# on the function, called with USE_FUNCTION on it.
FUNCTION_OPS = {"CreateFunction", "DropFunction"}
# Other people's queries, system information and procedures.
SYSTEM_OPS = {"ReadSystemInformation", "WriteSystemInformation", "KillQueryOwnedBy", "ViewQueryOwnedBy",
              "FilterViewQueryOwnedBy", "ExecuteProcedure", "CreateRole", "DropRole", "GrantRoles", "RevokeRoles",
              "GrantTablePrivilege", "DenyTablePrivilege", "RevokeTablePrivilege", "GrantSchemaPrivilege",
              "DenySchemaPrivilege", "RevokeSchemaPrivilege", "SetCatalogAuthorization"}


@dataclass
class CatalogMapping:
    platform: str = "trino"
    database: Optional[str] = None  # None: the Trino catalog's name
    env: str = "PROD"


def parse_catalogs(raw: dict[str, Any]) -> dict[str, CatalogMapping]:
    out = {}
    for name, m in (raw or {}).items():
        if not isinstance(m, dict) or set(m) - {"platform", "database", "env"}:
            raise ValueError(f"CAIRN_TRINO_CATALOGS[{name}]: give platform, database and env only")
        out[name] = CatalogMapping(platform=m.get("platform", "trino"), database=m.get("database"), env=m.get("env", "PROD"))
    return out


def quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def ident(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


INTEGER_TYPES = {"tinyint": 2**7, "smallint": 2**15, "integer": 2**31, "int": 2**31, "bigint": None}


def _hash64(c: str) -> str:
    """A stable 64-bit number from the value (the same value always masks the same, so joins still match)."""
    return f"from_big_endian_64(substr(sha256(to_utf8(CAST({c} AS varchar))), 1, 8))"


def mask_expression(column: str, column_type: str, strategy: str) -> str:
    """A Trino expression that masks ``column`` and keeps its type (Trino requires the mask's type to be
    the column's). Text: partial (``j•••@•••.com``), hash, redact. Numbers: partial keeps one significant
    figure, cut toward zero (37 → 30, 123456 → 100000), hash a stable number of the same type, redact 0. Dates and
    timestamps: partial keeps the month, hash a stable date, redact 1970-01-01. Anything else (booleans,
    binary, JSON, arrays, maps, rows) becomes NULL."""
    c = ident(column)
    t = column_type.strip()
    base = t.lower().split("(")[0].strip()
    null = f"CAST(NULL AS {t})"

    def typed(expr: str) -> str:
        return f"CASE WHEN {c} IS NULL THEN NULL ELSE CAST({expr} AS {t}) END"

    if strategy == "null":
        return null
    if base in ("varchar", "char"):
        v = f"CAST({c} AS varchar)"
        if strategy == "redact":
            return typed("'••••'")
        if strategy == "hash":
            digest = f"lower(to_hex(sha256(to_utf8({v}))))"
            size = t[t.find("(") + 1:t.rfind(")")] if "(" in t else ""
            if size.isdigit() and int(size) < 19:  # too short for the prefix: the digest alone, cut to fit
                return typed(f"substr({digest}, 1, {size})")
            return typed(f"'sha256:' || substr({digest}, 1, 12)")
        # partial: like the catalog's masking of sample values
        return typed(f"CASE WHEN strpos({v}, '@') > 0 THEN substr({v}, 1, 1) || '•••@•••' || "
                     f"coalesce('.' || regexp_extract(split_part({v}, '@', 2), '\\.([^.]+)$', 1), '') "
                     f"WHEN length({v}) <= 4 THEN rpad('', length({v}), '•') "
                     f"ELSE substr({v}, 1, 1) || rpad('', least(length({v}) - 2, 8), '•') || substr({v}, length({v})) END")
    if base in INTEGER_TYPES or base in ("decimal", "real", "double"):
        if strategy == "redact":
            return typed("0")
        if strategy == "hash":
            if base in INTEGER_TYPES:
                bound = INTEGER_TYPES[base]
                return typed(_hash64(c) if bound is None else f"mod({_hash64(c)}, {bound})")
            if base == "decimal":
                parts = [p.strip() for p in t[t.find("(") + 1:t.rfind(")")].split(",")] if "(" in t else ["38", "0"]
                digits = min(18, max(1, int(parts[0]) - int(parts[1] if len(parts) > 1 else 0)))
                return typed(f"mod({_hash64(c)}, {10 ** digits})")
            return typed(f"CAST({_hash64(c)} AS double) / 1e9")
        # partial: one significant figure, cut toward zero (rounding up could overflow the type)
        mag = f"floor(log10(abs(CAST({c} AS double))))"
        if base == "decimal":
            cut = f"truncate({c}, -CAST({mag} AS integer))"
        else:
            unit = f"power(10e0, {mag})"
            cut = f"sign(CAST({c} AS double)) * floor(abs(CAST({c} AS double)) / {unit}) * {unit}"
        return f"CASE WHEN {c} IS NULL THEN NULL WHEN {c} = 0 THEN {c} ELSE CAST({cut} AS {t}) END"
    if base == "date":
        if strategy == "redact":
            return typed("DATE '1970-01-01'")
        if strategy == "hash":
            return typed(f"date_add('day', abs(mod({_hash64(c)}, 36525)), DATE '1970-01-01')")
        return typed(f"date_trunc('month', {c})")
    if base == "timestamp":
        epoch = ("TIMESTAMP '1970-01-01 00:00:00 UTC'" if "with time zone" in t.lower()
                 else "TIMESTAMP '1970-01-01 00:00:00'")
        if strategy == "redact":
            return typed(epoch)
        if strategy == "hash":
            return typed(f"date_add('second', abs(mod({_hash64(c)}, 86400)), "
                         f"date_add('day', abs(mod({_hash64(c)}, 36525)), {epoch}))")
        return typed(f"date_trunc('month', {c})")
    if base == "time":
        if strategy == "partial":
            return typed(f"date_trunc('hour', {c})")
        if strategy == "hash":
            return typed(f"date_add('second', abs(mod({_hash64(c)}, 86400)), TIME '00:00:00')")
        return typed("TIME '00:00:00'")
    return null


# Cairn's own catalog crawler (trino_catalog.py): it may see every table and column, and read no rows.
# Only Cairn signs in as it; /v1/trino/token never issues a token for it.
CRAWLER = "cairn-catalog"


class TrinoAccess:
    """Turns Trino's access requests into Cairn decisions."""

    def __init__(self, decide: Callable[[str, list[str], str, Optional[str]], Any],
                 plan_for: Callable[[str, str], Any], catalogs: dict[str, CatalogMapping],
                 ai_catalogs: Optional[set[str]] = None, routine_catalogs: Optional[set[str]] = None):
        # decide(actor, groups, privilege, urn) -> Decision (pdp)
        self.decide = decide
        self.ai_catalogs = {"llm"} if ai_catalogs is None else set(ai_catalogs)
        self.routine_catalogs = {"routines"} if routine_catalogs is None else set(routine_catalogs)
        # Clean rooms: the room's identity (cleanroom-<room>) reads its contributed tables and nothing else.
        self.cleanroom_tables: Optional[Callable[[str], set[str]]] = None
        # ai_admit(actor): budgets and daily caps for AI functions (cairn.ai.sql_gateway); None: none.
        self.ai_admit: Optional[Callable[[str], bool]] = None
        self.plan_for = plan_for
        self.catalogs = catalogs

    def dataset_name(self, catalog: str, schema: str, table: str) -> tuple[str, str, str]:
        """(platform, name, env) of a Trino table's dataset."""
        m = self.catalogs.get(catalog, CatalogMapping())
        db = catalog if m.database is None else m.database
        return m.platform, ".".join(p for p in (db, schema, table) if p).lower(), m.env

    def remap_lineage(self, event: dict) -> dict:
        """Names a Trino lineage event's datasets the way the catalog, access control and usage name them
        (``CAIRN_TRINO_CATALOGS``): Trino sends ``trino://host:port`` + ``catalog.schema.table``."""
        def one(d):
            if not isinstance(d, dict) or not str(d.get("namespace", "")).startswith("trino://"):
                return d
            parts = str(d.get("name", "")).split(".")
            if len(parts) != 3:
                return d
            platform, name, _ = self.dataset_name(*parts)
            return {**d, "namespace": f"{platform}://", "name": name}

        def fields(facets):
            cl = (facets or {}).get("columnLineage")
            if not isinstance(cl, dict) or not isinstance(cl.get("fields"), dict):
                return facets
            out = {k: {**v, "inputFields": [one(i) for i in v.get("inputFields") or []]} if isinstance(v, dict) else v
                   for k, v in cl["fields"].items()}
            return {**facets, "columnLineage": {**cl, "fields": out}}

        if not isinstance(event, dict):
            return event
        ev = dict(event)
        for side in ("inputs", "outputs"):
            if isinstance(ev.get(side), list):
                ev[side] = [{**one(d), **({"facets": fields(d.get("facets"))} if isinstance(d, dict) and d.get("facets") else {})}
                            for d in ev[side]]
        return ev

    def urn(self, catalog: str, schema: str, table: str) -> str:
        platform, name, env = self.dataset_name(catalog, schema, table)
        return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},{env})"

    @staticmethod
    def who(ctx: dict) -> tuple[str, list[str]]:
        ident_ = (ctx or {}).get("identity") or {}
        user = str(ident_.get("user") or "")
        if not user:
            raise HTTPException(400, "no Trino user")
        groups = [g if g.startswith("urn:li:corpGroup:") else f"urn:li:corpGroup:{g}" for g in ident_.get("groups") or []]
        return f"urn:li:corpuser:{user}", groups

    def allowed(self, actor: str, groups: list[str], privilege: str, urn: Optional[str]) -> bool:
        try:
            return self.decide(actor, groups, privilege, urn).decision == "ALLOW"
        except Exception as e:  # fail closed
            logger.warning("trino access decision failed (%s %s %s): %s", actor, privilege, urn, e)
            return False

    @staticmethod
    def function_urn(f: dict) -> str:
        """A stored function's resource: ``urn:li:function:<catalog>.<schema>.<name>``."""
        return "urn:li:function:" + ".".join(str(f.get(k) or "").lower() for k in ("catalogName", "schemaName", "functionName"))

    def table_urn(self, t: dict) -> str:
        return self.urn(t.get("catalogName", ""), t.get("schemaName", ""), t.get("tableName", ""))

    def _room_tables(self, user: str) -> Optional[set[str]]:
        if self.cleanroom_tables is None or not user.startswith("cleanroom-"):
            return None
        return self.cleanroom_tables(user.removeprefix("cleanroom-"))

    @staticmethod
    def _tkey(t: dict) -> str:
        return ".".join(str(t.get(k) or "") for k in ("catalogName", "schemaName", "tableName")).lower()

    def allow(self, inp: dict, warehouse: Optional[str] = None) -> bool:
        actor, groups = self.who(inp.get("context"))
        action = inp.get("action") or {}
        op = action.get("operation", "")
        res = action.get("resource") or {}
        user = actor.removeprefix("urn:li:corpuser:")
        room = self._room_tables(user)
        if room is not None:                          # a clean room: read its tables, and nothing else
            if op in ("ExecuteQuery", "AccessCatalog", "FilterCatalogs", "ShowSchemas", "FilterSchemas", "ShowTables"):
                return True
            if op == "ExecuteFunction":
                f = res.get("function") or {}
                return str(f.get("catalogName") or "") not in self.ai_catalogs | self.routine_catalogs
            t = res.get("table") or {}
            return op in TABLE_OPS and TABLE_OPS[op] in (VIEW, READ) and self._tkey(t) in room
        if op == "ExecuteQuery" and warehouse is not None:   # a team's warehouse: its members (and operators)
            return user == CRAWLER or self.allowed(actor, groups, WAREHOUSE, f"urn:li:warehouse:{warehouse}")
        if op in OPEN_OPS:
            return True
        if op == "ExecuteFunction":   # built-in and inline functions are open; AI functions need USE_AI_FUNCTIONS
            f = res.get("function") or {}
            if str(f.get("catalogName") or "") in self.ai_catalogs:
                if user == CRAWLER or not self.allowed(actor, groups, AI, None):
                    return False
                return self.ai_admit is None or self.ai_admit(actor)
            if str(f.get("catalogName") or "") in self.routine_catalogs:   # stored functions: USE_FUNCTION on each
                return user != CRAWLER and self.allowed(actor, groups, FUNCTION, self.function_urn(f))
            return True
        if op in FUNCTION_OPS:
            f = res.get("function") or {}
            return user != CRAWLER and self.allowed(actor, groups, MANAGE, self.function_urn(f) if f else None)
        if user == CRAWLER:  # structure only: listings and descriptions, Trino's metadata tables, no rows
            t = res.get("table") or {}
            meta = t.get("schemaName") == "information_schema" or t.get("catalogName") == "system"
            return op in TABLE_OPS and (TABLE_OPS[op] == VIEW or (meta and TABLE_OPS[op] == READ))
        if op == "ImpersonateUser":
            return (res.get("user") or {}).get("user") == user or self.allowed(actor, groups, SYSTEM, None)
        if op in ("ViewQueryOwnedBy", "KillQueryOwnedBy"):
            return (res.get("user") or {}).get("user") == user or self.allowed(actor, groups, SYSTEM, None)
        if op in SCHEMA_OPS:
            return self.allowed(actor, groups, MANAGE, None)
        if op in SYSTEM_OPS:
            return self.allowed(actor, groups, SYSTEM, None)
        if op in TABLE_OPS:
            t = res.get("table")
            if not t:
                return False
            # Trino's own metadata tables: what they show is filtered table by table (FilterTables,
            # FilterColumns, FilterViewQueryOwnedBy), so reading them is open; changing them is not.
            if (t.get("schemaName") == "information_schema" or t.get("catalogName") == "system") and TABLE_OPS[op] in (VIEW, READ):
                return True
            ok = self.allowed(actor, groups, TABLE_OPS[op], self.table_urn(t))
            if ok and op in ("RenameTable", "RenameView", "RenameMaterializedView"):
                target = (action.get("targetResource") or {}).get("table")
                ok = bool(target) and self.allowed(actor, groups, MANAGE, self.table_urn(target))
            return ok
        logger.info("trino operation %s is not known to Cairn: refused", op)
        return False

    def batch(self, inp: dict) -> list[int]:
        actor, groups = self.who(inp.get("context"))
        action = inp.get("action") or {}
        op = action.get("operation", "")
        items = action.get("filterResources") or []
        user = actor.removeprefix("urn:li:corpuser:")
        room = self._room_tables(user)
        if room is not None:
            if op in ("FilterCatalogs", "FilterSchemas"):
                return list(range(len(items)))
            if op == "FilterTables":
                return [i for i, r in enumerate(items) if self._tkey(r.get("table") or {}) in room]
            if op == "FilterColumns":
                t = (items[0] if items else {}).get("table") or {}
                return list(range(len(t.get("columns") or []))) if self._tkey(t) in room else []
            return [i for i, r in enumerate(items) if self.allow({**inp, "action": {"operation": op, "resource": r}})]
        if op in ("FilterCatalogs", "FilterSchemas", "FilterFunctions"):
            return list(range(len(items)))
        if user == CRAWLER and op in ("FilterTables", "FilterColumns"):
            t = (items[0] if items else {}).get("table") or {}
            return list(range(len(t.get("columns") or []))) if op == "FilterColumns" else list(range(len(items)))
        if op == "FilterTables":
            return [i for i, r in enumerate(items) if r.get("table") and self.allowed(actor, groups, VIEW, self.table_urn(r["table"]))]
        if op == "FilterColumns":
            t = (items[0] if items else {}).get("table") or {}
            cols = t.get("columns") or []
            return list(range(len(cols))) if t and self.allowed(actor, groups, VIEW, self.table_urn(t)) else []
        if op == "FilterViewQueryOwnedBy":
            if self.allowed(actor, groups, SYSTEM, None):
                return list(range(len(items)))
            return [i for i, r in enumerate(items) if (r.get("user") or {}).get("user") == user]
        # Anything else, one at a time.
        return [i for i, r in enumerate(items) if self.allow({**inp, "action": {"operation": op, "resource": r}})]

    def row_filters(self, inp: dict) -> list[dict]:
        actor, groups = self.who(inp.get("context"))
        if self._room_tables(actor.removeprefix("urn:li:corpuser:")) is not None:
            return []                                 # parties agreed to the room; only aggregates leave it
        t = ((inp.get("action") or {}).get("resource") or {}).get("table") or {}
        if not t:
            return []
        urn = self.table_urn(t)
        try:
            d = self.decide(actor, groups, READ, urn)
        except Exception as e:  # fail closed: a filter nothing passes
            logger.warning("trino row filter decision failed for %s on %s: %s", actor, urn, e)
            return [{"expression": "false"}]
        user = actor.removeprefix("urn:li:corpuser:")
        out = []
        for o in d.obligations:
            if o.type != "row_filter":
                continue
            expr = str(o.params.get("expression") or "").strip()
            if not expr:
                return [{"expression": "false"}]  # a filter without an expression: nothing passes
            out.append({"expression": expr.replace("{user}", quote(user))})
        return out

    def column_mask(self, inp: dict) -> Optional[dict]:
        if self._room_tables(self.who(inp.get("context"))[0].removeprefix("urn:li:corpuser:")) is not None:
            return None
        actor, _ = self.who(inp.get("context"))
        col = ((inp.get("action") or {}).get("resource") or {}).get("column") or {}
        if not col:
            return None
        urn = self.urn(col.get("catalogName", ""), col.get("schemaName", ""), col.get("tableName", ""))
        try:
            plan = self.plan_for(actor, urn)
        except Exception as e:  # fail closed
            logger.warning("trino mask plan failed for %s on %s: %s", actor, urn, e)
            return {"expression": f"CAST(NULL AS {col.get('columnType', 'varchar')})"}
        name = col.get("columnName", "")
        if not plan.masks_field(name):
            return None
        return {"expression": mask_expression(name, col.get("columnType", "varchar"), plan.strategy)}


LINEAGE_ACTOR = "urn:li:corpuser:cairn-trino"


def name_jobs_by_outputs(event: dict) -> dict:
    """Trino names a job by its query id; name it by what it writes instead (a run keeps the query id)."""
    if not isinstance(event, dict) or not isinstance(event.get("job"), dict):
        return event
    outs = sorted({o.get("name", "") for o in event.get("outputs") or [] if isinstance(o, dict) and o.get("name")})
    if not outs:
        return event
    job = dict(event["job"])
    job["facets"] = {**(job.get("facets") or {}), "documentation": {"description": f"Trino query {job.get('name', '')}"}}
    job["name"] = "trino:" + ",".join(outs)
    return {**event, "job": job}


def build_trino_router(access: TrinoAccess, key: str, audit=None, lineage=None, tokens=None,
                       current_principal=None, outbox=None, usage=None, catalog_sync=None,
                       authorize=None, oauth=None, groups_of=None, history=None, functions=None) -> APIRouter:
    router = APIRouter(prefix="/v1/trino", tags=["trino"], include_in_schema=False)

    if tokens is not None:
        from fastapi import Body, Depends

        @router.get("/jwks.json")
        def jwks():
            """Trino's ``http-server.authentication.jwt.key-file``: the public key Cairn signs tokens with."""
            return tokens.jwks()

        if catalog_sync is not None:
            @router.post("/catalog-sync")
            def catalog_sync_now(connection: str = Body(..., embed=True), p=Depends(current_principal)):
                """Reads a Trino connection's tables into Cairn's catalog now."""
                from cairn.dataaccess.service import DataAccessError
                from cairn.dataaccess.trino_catalog import CatalogSyncError

                authorize(p, "MANAGE_INGESTION", None)
                try:
                    return catalog_sync.sync(connection, p.urn)
                except CatalogSyncError as e:
                    raise HTTPException(e.status, str(e))
                except DataAccessError as e:
                    raise HTTPException(e.status if e.status != 403 else 502, f"Trino: {e}")

            @router.get("/catalog-sync")
            def catalog_sync_connections(p=Depends(current_principal)):
                authorize(p, "MANAGE_INGESTION", None)
                return {"connections": sorted(catalog_sync.connections())}

            if functions is not None:
                @router.post("/functions/sync")
                def functions_sync_now(connection: str = Body(..., embed=True), p=Depends(current_principal)):
                    """Reads the routines catalogs' stored functions into Cairn's catalog now."""
                    from cairn.dataaccess.service import DataAccessError

                    authorize(p, "MANAGE_INGESTION", None)
                    definition = catalog_sync.connections().get(connection)
                    if definition is None:
                        raise HTTPException(404, f"no trino:// connection {connection!r} with run_as_user")
                    try:
                        return functions.sync(lambda sql: catalog_sync._query(definition, sql))
                    except DataAccessError as e:
                        raise HTTPException(e.status if e.status != 403 else 502, f"Trino: {e}")

        if oauth is not None:
            from urllib.parse import quote, urlencode

            from fastapi.responses import JSONResponse, RedirectResponse

            from cairn.dataaccess.trino_oauth import OAuthError

            def back(redirect_uri: str, **params) -> RedirectResponse:
                sep = "&" if "?" in redirect_uri else "?"
                return RedirectResponse(redirect_uri + sep + urlencode({k: v for k, v in params.items() if v}), 302)

            @router.get("/oauth/authorize")
            def oauth_authorize(request: Request, response_type: str = "", client_id: str = "", redirect_uri: str = "",
                                state: str = "", code_challenge: str = "", code_challenge_method: str = ""):
                """Trino's sign-in: a person signed in to Cairn goes back to Trino with a one-time code."""
                try:
                    oauth.check(client_id, redirect_uri, response_type, code_challenge_method if code_challenge else "")
                except OAuthError as e:
                    if e.error in ("unauthorized_client", "invalid_request") and not oauth.redirect_ok(redirect_uri, client_id):
                        raise HTTPException(400, str(e))  # never redirect to an address Cairn does not trust
                    return back(redirect_uri, error=e.error, error_description=str(e), state=state)
                try:
                    p = current_principal(request)
                except HTTPException:
                    p = None
                if p is None or p.via not in ("session", "dev") or p.urn == f"urn:li:corpuser:{CRAWLER}":
                    here = f"/cairn/api/v1/trino/oauth/authorize?{request.url.query}"
                    return RedirectResponse(f"/login?redirect_uri={quote(here, safe='')}", 302)
                groups = tuple(p.groups) or (tuple(groups_of(p.urn)) if groups_of is not None else ())
                code = oauth.issue_code(p.urn, redirect_uri, code_challenge, "S256" if code_challenge else "", client_id,
                                        groups)
                if audit is not None:
                    audit.record("trino" if client_id == "trino" else "identity", f"{client_id}.sign_in", p.urn, None,
                                 {"redirect": redirect_uri.split("?")[0], "client": client_id})
                return back(redirect_uri, code=code, state=state)

            @router.post("/oauth/token")
            async def oauth_token(request: Request):
                """Trino exchanges the code for an access token (its client secret, HTTP Basic or form)."""
                import base64
                from urllib.parse import parse_qs

                form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
                cid, secret = form.get("client_id", ""), form.get("client_secret", "")
                basic = request.headers.get("authorization", "")
                if basic.lower().startswith("basic "):
                    try:
                        from urllib.parse import unquote

                        u, _, pw = base64.b64decode(basic[6:]).decode().partition(":")
                        cid, secret = unquote(u), unquote(pw)
                    except ValueError:
                        pass
                if form.get("grant_type") != "authorization_code":
                    return JSONResponse({"error": "unsupported_grant_type"}, 400)
                try:
                    out = await run_in_threadpool(oauth.exchange, form.get("code", ""), form.get("redirect_uri", ""), cid,
                                                  secret, form.get("code_verifier", ""))
                except OAuthError as e:
                    return JSONResponse({"error": e.error, "error_description": str(e)}, e.status)
                return JSONResponse(out, headers={"Cache-Control": "no-store"})

        @router.post("/token")
        def token(ttl_seconds: Optional[int] = Body(None, embed=True), p=Depends(current_principal)):
            """A Trino sign-in token for the caller (BI tools, the Trino CLI, notebooks)."""
            if p.is_service or p.via in ("agent", "extension") or p.urn == f"urn:li:corpuser:{CRAWLER}":
                raise HTTPException(403, "Trino tokens are for people signed in to Cairn")
            try:
                out = tokens.mint(p.urn, ttl_seconds)
            except ValueError as e:
                raise HTTPException(422, str(e))
            if audit is not None:
                audit.record("trino", "trino.token_issued", p.urn, None,
                             {"token_id": out["token_id"], "expires_at": out["expires_at"]})
            return out

    def check(given: str):
        if not hmac.compare_digest(given.encode(), key.encode()):
            raise HTTPException(404)

    async def body(request: Request) -> dict:
        doc = await request.json()
        inp = doc.get("input") if isinstance(doc, dict) else None
        if not isinstance(inp, dict):
            raise HTTPException(400, "expected {\"input\": ...}")
        return inp

    @router.post("/{given}/allow")
    async def allow(given: str, request: Request):
        check(given)
        return {"result": await run_in_threadpool(access.allow, await body(request))}

    import re as _re

    def wh_name(name: str) -> str:
        if not _re.match(r"^[a-z][a-z0-9-]{0,40}$", name):
            raise HTTPException(404)
        return name

    @router.post("/{given}/warehouse/{warehouse}/allow")
    async def warehouse_allow(given: str, warehouse: str, request: Request):
        check(given)
        name = wh_name(warehouse)
        return {"result": await run_in_threadpool(access.allow, await body(request), name)}

    @router.post("/{given}/warehouse/{warehouse}/batch")
    async def warehouse_batch(given: str, warehouse: str, request: Request):
        check(given)
        wh_name(warehouse)
        return {"result": await run_in_threadpool(access.batch, await body(request))}

    @router.post("/{given}/warehouse/{warehouse}/row-filters")
    async def warehouse_row_filters(given: str, warehouse: str, request: Request):
        check(given)
        wh_name(warehouse)
        return {"result": await run_in_threadpool(access.row_filters, await body(request))}

    @router.post("/{given}/batch")
    async def batch(given: str, request: Request):
        check(given)
        return {"result": await run_in_threadpool(access.batch, await body(request))}

    @router.post("/{given}/row-filters")
    async def row_filters(given: str, request: Request):
        check(given)
        return {"result": await run_in_threadpool(access.row_filters, await body(request))}

    @router.post("/{given}/lineage")
    async def lineage_events(given: str, request: Request):
        check(given)
        if lineage is None:
            raise HTTPException(503, "lineage is not available")
        from cairn.sources.openlineage import OpenLineageError

        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "the body is not JSON")
        # Jobs are named by what they write (as Trino names it), then datasets as Cairn names them.
        events = [access.remap_lineage(name_jobs_by_outputs(e)) for e in (body if isinstance(body, list) else [body])]
        try:
            return await run_in_threadpool(lineage.handle_many, events, LINEAGE_ACTOR)
        except OpenLineageError as e:
            raise HTTPException(422, str(e))
        except Exception as e:  # the metadata store is down or refused the write
            logger.warning("trino lineage could not be recorded now: %s", e)
            if outbox is not None:
                try:  # Trino never resends: keep it and write it when Core is back
                    from fastapi.responses import JSONResponse

                    held = outbox.hold(events, str(e))
                    return JSONResponse({"held": held, "events": len(events)}, status_code=202)
                except Exception as e2:
                    logger.error("trino lineage could not be held either: %s", e2)
            raise HTTPException(503, "lineage could not be recorded: the metadata store is unavailable")

    @router.post("/{given}/queries")
    async def queries(given: str, request: Request):
        """Trino's HTTP event listener: each finished query, for usage statistics (who read what)."""
        check(given)
        if usage is None and history is None:
            raise HTTPException(503, "usage is not available")
        from cairn.dataaccess.trino_usage import UsageError

        try:
            event = await request.json()
        except ValueError:
            raise HTTPException(400, "the body is not JSON")
        if history is not None and isinstance(event, dict):
            try:
                await run_in_threadpool(history.record, event)       # the query's profile, for its author
            except Exception as e:  # noqa: BLE001 - history never costs the usage statistics
                logger.warning("query history: %s", e)
        if functions is not None and isinstance(event, dict):
            try:
                await run_in_threadpool(functions.from_event, event)  # a stored function created or dropped
            except Exception as e:  # noqa: BLE001 - nor does the function catalog
                logger.warning("stored functions: %s", e)
        if usage is None:
            return {"query": (event.get("metadata") or {}).get("queryId") if isinstance(event, dict) else None}
        try:
            return await run_in_threadpool(usage.record, event)
        except UsageError as e:
            raise HTTPException(422, str(e))

    @router.post("/{given}/column-mask")
    async def column_mask(given: str, request: Request):
        check(given)
        return {"result": await run_in_threadpool(access.column_mask, await body(request))}

    @router.post("/{given}/warehouse/{warehouse}/column-mask")
    async def warehouse_column_mask(given: str, warehouse: str, request: Request):
        check(given)
        wh_name(warehouse)
        return {"result": await run_in_threadpool(access.column_mask, await body(request))}

    return router
