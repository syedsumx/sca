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

logger = logging.getLogger(__name__)

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
            "ShowFunctions", "FilterFunctions", "ExecuteFunction", "SetSystemSessionProperty",
            "SetCatalogSessionProperty", "ShowCurrentRoles", "ShowRoleGrants", "ShowRoles"}
# Schema-level changes: MANAGE_DATASET without a particular dataset.
SCHEMA_OPS = {"CreateSchema", "DropSchema", "RenameSchema", "SetSchemaAuthorization", "CreateFunction", "DropFunction"}
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


def mask_expression(column: str, column_type: str, strategy: str) -> str:
    """A Trino expression that masks ``column`` and keeps its type (non-text columns become NULL)."""
    c = ident(column)
    text = column_type.lower().startswith(("varchar", "char"))
    if strategy == "null" or not text:
        return f"CAST(NULL AS {column_type})"
    if strategy == "redact":
        return f"CASE WHEN {c} IS NULL THEN NULL ELSE '••••' END"
    if strategy == "hash":
        return f"CASE WHEN {c} IS NULL THEN NULL ELSE 'sha256:' || substr(lower(to_hex(sha256(to_utf8({c})))), 1, 12) END"
    # partial: like the catalog's masking of sample values
    return (f"CASE WHEN {c} IS NULL THEN NULL "
            f"WHEN strpos({c}, '@') > 0 THEN substr({c}, 1, 1) || '•••@•••' || "
            f"coalesce('.' || regexp_extract(split_part({c}, '@', 2), '\\.([^.]+)$', 1), '') "
            f"WHEN length({c}) <= 4 THEN rpad('', length({c}), '•') "
            f"ELSE substr({c}, 1, 1) || rpad('', least(length({c}) - 2, 8), '•') || substr({c}, length({c})) END")


class TrinoAccess:
    """Turns Trino's access requests into Cairn decisions."""

    def __init__(self, decide: Callable[[str, list[str], str, Optional[str]], Any],
                 plan_for: Callable[[str, str], Any], catalogs: dict[str, CatalogMapping]):
        # decide(actor, groups, privilege, urn) -> Decision (pdp)
        self.decide = decide
        self.plan_for = plan_for
        self.catalogs = catalogs

    def urn(self, catalog: str, schema: str, table: str) -> str:
        m = self.catalogs.get(catalog, CatalogMapping())
        db = catalog if m.database is None else m.database
        name = ".".join(p for p in (db, schema, table) if p).lower()
        return f"urn:li:dataset:(urn:li:dataPlatform:{m.platform},{name},{m.env})"

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

    def table_urn(self, t: dict) -> str:
        return self.urn(t.get("catalogName", ""), t.get("schemaName", ""), t.get("tableName", ""))

    def allow(self, inp: dict) -> bool:
        actor, groups = self.who(inp.get("context"))
        action = inp.get("action") or {}
        op = action.get("operation", "")
        res = action.get("resource") or {}
        user = actor.removeprefix("urn:li:corpuser:")
        if op in OPEN_OPS:
            return True
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
        if op in ("FilterCatalogs", "FilterSchemas", "FilterFunctions"):
            return list(range(len(items)))
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
                       current_principal=None, outbox=None) -> APIRouter:
    router = APIRouter(prefix="/v1/trino", tags=["trino"], include_in_schema=False)

    if tokens is not None:
        from fastapi import Body, Depends

        @router.get("/jwks.json")
        def jwks():
            """Trino's ``http-server.authentication.jwt.key-file``: the public key Cairn signs tokens with."""
            return tokens.jwks()

        @router.post("/token")
        def token(ttl_seconds: Optional[int] = Body(None, embed=True), p=Depends(current_principal)):
            """A Trino sign-in token for the caller (BI tools, the Trino CLI, notebooks)."""
            if p.is_service or p.via in ("agent", "extension"):
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
        return {"result": access.allow(await body(request))}

    @router.post("/{given}/batch")
    async def batch(given: str, request: Request):
        check(given)
        return {"result": access.batch(await body(request))}

    @router.post("/{given}/row-filters")
    async def row_filters(given: str, request: Request):
        check(given)
        return {"result": access.row_filters(await body(request))}

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
        events = [name_jobs_by_outputs(e) for e in (body if isinstance(body, list) else [body])]
        try:
            return lineage.handle_many(events, LINEAGE_ACTOR)
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

    @router.post("/{given}/column-mask")
    async def column_mask(given: str, request: Request):
        check(given)
        return {"result": access.column_mask(await body(request))}

    return router
