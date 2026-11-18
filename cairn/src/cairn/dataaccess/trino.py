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
  - External Iceberg catalogs (``cairn.dataaccess.external_catalogs``) are named like the lakehouse
    (``iceberg`` datasets ``<catalog>.<schema>.<table>``) and are read-only unless registered writable:
    every write, table or schema change in one is refused. Only Cairn's catalog administrator
    (``cairn-catalogs``) may create or drop catalogs, and it may do nothing else.
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
import re
import threading
import time
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
# Operations that remove or rewrite a table's rows or its history: refused for everyone while a legal hold is on.
# Writes whose rows can come from other tables in the same statement: data residency follows them (CopyGuard).
COPY_OPS = {"InsertIntoTable", "CreateTable", "UpdateTableColumns", "CreateMaterializedView", "RefreshMaterializedView"}
# CreateTable is among them: Trino checks CREATE OR REPLACE TABLE as CreateTable alone, and it replaces the rows.
HOLD_OPS = {"DeleteFromTable", "TruncateTable", "UpdateTableColumns", "DropTable", "RenameTable", "DropColumn",
            "AlterColumn", "RenameColumn", "SetTableProperties", "ExecuteTableProcedure", "CreateTable"}
# Operations every signed-in Trino user may perform (what they reach is still filtered per table).
OPEN_OPS = {"ExecuteQuery", "AccessCatalog", "FilterCatalogs", "ShowSchemas", "FilterSchemas", "ShowTables",
            "ShowFunctions", "FilterFunctions", "SetSystemSessionProperty",
            "SetCatalogSessionProperty", "ShowCurrentRoles", "ShowRoleGrants", "ShowRoles"}
# System session properties that lift the cluster's limits for one query: whoever sets them needs the system privilege.
# The time limits are among them: Trino takes a session's query_max_run_time, query_max_execution_time and
# query_max_planning_time as they are, not capped at the cluster's configured ones (Cairn's own statement runner
# enforces its limit itself and sets none of them).
LIMIT_SESSION_PROPERTIES = {
    "resource_overcommit", "query_max_memory", "query_max_total_memory", "query_max_memory_per_node",
    "query_max_total_memory_per_node", "query_max_cpu_time", "query_max_scan_physical_bytes",
    "query_max_stage_count", "query_priority", "query_max_output_size", "query_max_write_physical_size",
    "query_max_run_time", "query_max_execution_time", "query_max_planning_time"}
# Schema-level changes: MANAGE_DATASET without a particular dataset.
SCHEMA_OPS = {"CreateSchema", "DropSchema", "RenameSchema", "SetSchemaAuthorization"}
# Ownership changes. A SECURITY DEFINER view runs as its owner (its tables read with the owner's rights, masks and row
# filters), so handing one to another principal would borrow their reads: only to oneself, or by an operator
# (MANAGE_SYSTEM_OPERATIONS), on top of the privilege the operation itself needs.
OWNER_OPS = {"SetViewAuthorization", "SetTableAuthorization", "SetSchemaAuthorization"}
# Published metric views: the statements Cairn's own service identity makes in a metrics view location (and the only
# changes anyone may make there).
METRIC_VIEW_DDL = {"CreateView", "DropView"}
RENAME_OPS = {"RenameTable", "RenameView", "RenameMaterializedView"}
# Stored functions (in the catalogs of CAIRN_TRINO_ROUTINE_CATALOGS): created and dropped with MANAGE_DATASET
# on the function, called with USE_FUNCTION on it.
FUNCTION_OPS = {"CreateFunction", "DropFunction"}
# Iceberg metadata tables (<table>$<name>) that show no row values: readable by whoever may read the table.
SAFE_METADATA = {"snapshots", "history", "refs"}
# Connector functions (<catalog>.system.<name>) that compute on what they are given and return no stored rows.
SAFE_CONNECTOR_FUNCTIONS = {"bucket"}
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


def geo_decimals() -> int:
    """Decimal places a location keeps under the ``geo`` mask (2: about 1 km; 1: about 11 km)."""
    import os

    try:
        return min(6, max(0, int(os.environ.get("CAIRN_GEO_MASK_DECIMALS", "2"))))
    except ValueError:
        return 2


def _geo_mask(column: str, c: str, t: str, base: str, typed) -> Optional[str]:
    """The ``geo`` mask: a location kept, coarsened. Coordinates are rounded; a shape (WKT text, Geometry,
    SphericalGeography) becomes its centre point, rounded. Other values: None (they get the partial mask)."""
    d = geo_decimals()

    def point(g: str) -> str:
        return f"ST_Point(round(ST_X(ST_Centroid({g})), {d}), round(ST_Y(ST_Centroid({g})), {d}))"
    if base in ("double", "real", "decimal"):
        return typed(f"round({c}, {d})")
    if base in ("varchar", "char"):
        g = f"try(ST_GeometryFromText(CAST({c} AS varchar)))"
        partial = mask_expression(column, t, "partial")   # the column as named (``c`` is it quoted)
        return (f"CASE WHEN {c} IS NULL THEN NULL WHEN {g} IS NOT NULL AND NOT ST_IsEmpty({g}) "
                f"THEN CAST(ST_AsText({point(g)}) AS {t}) ELSE {partial} END")
    if base == "geometry":
        return f"CASE WHEN {c} IS NULL OR ST_IsEmpty({c}) THEN {c} ELSE {point(c)} END"
    if base == "sphericalgeography":
        g = f"to_geometry({c})"
        return f"CASE WHEN {c} IS NULL THEN NULL ELSE to_spherical_geography({point(g)}) END"
    return None


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
    if strategy == "geo":
        geo = _geo_mask(column, c, t, base, typed)
        if geo is not None:
            return geo
        strategy = "partial"                   # not a location: masked as partial is
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
                scale = int(parts[1] if len(parts) > 1 else 0)
                digits = min(18, int(parts[0]) - scale)
                if digits < 1:   # decimal(p,p): values below 1, so a fraction of up to 15 digits (a double holds them)
                    k = min(15, max(1, scale))
                    return typed(f"CAST(mod({_hash64(c)}, {10 ** k}) AS double) / 1e{k}")
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


_V2_PART = re.compile(r"\[[^\]]*\]\.?")


def masked_path(plan: Any, column: str) -> Optional[str]:
    """The field path in ``plan`` that masks Trino column ``column``, or None. Trino asks per top-level column, in
    lower case; the catalog's field paths keep the source's case (Iceberg keeps what Spark wrote) and name fields
    inside row columns (``customer.email``, or v2 ``[version=2.0].[type=struct].customer.[type=string].email``). A
    labelled field anywhere inside a column masks the whole column (a row becomes NULL)."""
    if plan.masks_field(column):
        return column
    want = str(column or "").lower()
    if not want:
        return None
    for f in sorted(getattr(plan, "fields", None) or ()):
        simple = _V2_PART.sub("", str(f))
        if simple.lower() == want or simple.lower().split(".")[0] == want:
            return f
    return None


# Cairn's own catalog crawler (trino_catalog.py): it may see every table and column, and read no rows.
# Only Cairn signs in as it; /v1/trino/token never issues a token for it.
CRAWLER = "cairn-catalog"
# Cairn's catalog administrator (external_catalogs.py): creates and drops external catalogs, nothing else.
CATALOG_ADMIN = "cairn-catalogs"
CATALOG_ADMIN_OPS = {"ExecuteQuery", "AccessCatalog", "FilterCatalogs", "CreateCatalog", "DropCatalog"}


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
        # held(urn): a legal hold is on the table (cairn.governance.records); None: holds are not in use.
        self.held: Optional[Callable[[str], bool]] = None
        # resident(catalog, schema): may this deployment read and write the schema's tables (data residency,
        # cairn.governance.residency)? None: no residency rules.
        self.resident: Optional[Callable[[str, str], bool]] = None
        # copies(op, query_id, catalog, schema): copies made by SQL follow residency pins (CopyGuard): the reason to
        # refuse a read or write of the statement, or None; None: no residency rules.
        self.copies: Optional[Callable[[str, Optional[str], str, str], Optional[str]]] = None
        self.plan_for = plan_for
        self.catalogs = catalogs
        # metric_view_source(table): a published metric's view (SECURITY INVOKER) is read on the terms of the
        # metric's table, its source's urn; None otherwise (cairn.sqlworkspace.metrics).
        self.metric_view_source: Optional[Callable[[dict], Optional[str]]] = None
        # metric_view_writers(catalog, schema): when the schema is where published metric views are made, the Trino
        # users (Cairn's service identities) that alone may create or drop views in it, nobody changing anything else
        # there; None for any other schema. A read of a view there is decided on a metric's table, so a view of that
        # name must be Cairn's own invoker view, never one a person made (or swapped in) as definer.
        self.metric_view_writers: Optional[Callable[[str, str], Optional[set[str]]]] = None
        # service_identities(): the Trino users Cairn's own statements run as on its governed connections (each one's
        # service_user, which makes the metric views); no person is issued a Trino token as one. It raises when the
        # connections cannot be read (no token then).
        self.service_identities: Optional[Callable[[], set[str]]] = None
        # checked(user): a one-query identity ``checked-<id>`` Cairn registered after checking a query against
        # aggregation and projection policies -> {actor, groups, tables}; None: not one (cairn.governance.aggregation).
        self.checked: Optional[Callable[[str], Optional[dict]]] = None
        # app_identity(user): an installed app's identity ``cairnapp-<install>`` -> {actor: its installer, tables:
        # the dataset urns bound to it}; None: not one (cairn.dataaccess.app_packages).
        self.app_identity: Optional[Callable[[str], Optional[dict]]] = None
        # external_catalog(catalog): a registered external Iceberg catalog -> {platform, database, writable};
        # None: not one (cairn.dataaccess.external_catalogs). It raises when it cannot tell (refused).
        self.external_catalog: Optional[Callable[[str], Optional[dict]]] = None
        # system_tables: Cairn's system tables (cairn.dataaccess.system_tables): read-only, a person's own rows unless an
        # operator, statement text masked without VIEW_QUERY_TEXT; None: not in use.
        self.system_tables: Optional[Any] = None
        # derived_source(urn): the table whose raw values ``urn`` holds when Cairn derives it from another (a vector
        # index's vectors table: chunks of its source's text, and their embeddings), else None; it raises when it
        # cannot tell (refused). Such a table is read only by those who read its source without a mask, row filter
        # or aggregation policy: none of them can be applied to the copy (cairn.pipelines.vectors).
        self.derived_source: Optional[Callable[[str], Optional[str]]] = None
        # user_status(urn): "ACTIVE" | "SUSPENDED" (Cairn Core's corpUserStatus); it raises when it cannot tell
        # (refused). Trino checks only a token's signature and expiry, so a suspended person's token (up to 12 hours)
        # is refused here, on every request; the answer is reused for STATUS_TTL seconds. None: not checked.
        self.user_status: Optional[Callable[[str], str]] = None
        self._status: dict[str, tuple[float, bool]] = {}
        self._status_lock = threading.Lock()

    STATUS_TTL = 30.0

    def _refuse_suspended(self, actor: str) -> None:
        if self.user_status is None:
            return
        now = time.monotonic()
        with self._status_lock:
            hit = self._status.get(actor)
        if hit is not None and now - hit[0] < self.STATUS_TTL:
            active = hit[1]
        else:
            try:
                active = str(self.user_status(actor) or "ACTIVE").upper() != "SUSPENDED"
            except Exception as e:  # noqa: BLE001 - fail closed: a person whose status cannot be read is refused
                logger.warning("trino: cannot check whether %s is suspended: %s", actor, e)
                raise HTTPException(403, "Cairn cannot check the user right now")
            with self._status_lock:
                if len(self._status) > 10_000:
                    self._status.clear()
                self._status[actor] = (now, active)
        if not active:
            raise HTTPException(403, "the user is suspended")

    def _system_spec(self, catalog: str, schema: str, table: str):
        """The system table (its spec) a Trino table is, or None."""
        st = self.system_tables
        return None if st is None else st.spec_of(str(catalog or ""), str(schema or ""), str(table or ""))

    def _in_system_catalog(self, catalog: str) -> bool:
        return self.system_tables is not None and str(catalog or "") == self.system_tables.trino_catalog

    def _decided_on(self, t: dict, need: str) -> str:
        """The urn a read or view of ``t`` is decided on: a published metric view's source table, else ``t``."""
        if need in (VIEW, READ) and self.metric_view_source is not None:
            try:
                src = self.metric_view_source(t)
            except Exception as e:  # fail closed: decided on the view itself, which no policy names
                logger.warning("metric view lookup failed for %s: %s", self._tkey(t), e)
                src = None
            if src:
                return src
        return self.table_urn(t)

    def service_identity(self, user: str) -> bool:
        """Is ``user`` (a name or ``urn:li:corpuser:`` urn) one of Cairn's own Trino identities, which Trino gives rights
        no person has? Raises when that cannot be told (the caller refuses)."""
        from cairn.common.security import reserved_name

        name = str(user or "").removeprefix("urn:li:corpuser:")
        if name in (CRAWLER, CATALOG_ADMIN) or reserved_name(name):
            return True
        if self.service_identities is None:
            return False
        return name in {str(u).removeprefix("urn:li:corpuser:") for u in self.service_identities()}

    def _metric_writers(self, catalog: Any, schema: Any) -> Optional[set[str]]:
        """Who may change ``catalog.schema`` when it is a metrics view location (see ``metric_view_writers``); None
        when it is not one."""
        if self.metric_view_writers is None:
            return None
        try:
            got = self.metric_view_writers(str(catalog or ""), str(schema or ""))
        except Exception as e:  # noqa: BLE001 - fail closed: guarded, and nobody may change it
            logger.warning("metric view location lookup failed for %s.%s: %s", catalog, schema, e)
            return set()
        return None if got is None else {str(u) for u in got}

    def _metric_location(self, inp: dict, op: str, places: list[dict], ddl: set[str]) -> Optional[bool]:
        """A change touching a metrics view location: True when Cairn's service identity makes one of ``ddl``
        there, False (refused) for anything else; None when none of ``places`` is one."""
        guarded = [w for w in (self._metric_writers(p.get("catalogName"), p.get("schemaName")) for p in places)
                   if w is not None]
        if not guarded:
            return None
        user = self._raw_user(inp)
        if op in ddl and all(user in w for w in guarded):
            return True
        logger.info("%s by %s refused: only Cairn makes or changes views in the metrics schema (%s)", op, user,
                    ", ".join(f"{p.get('catalogName')}.{p.get('schemaName')}" for p in places))
        return False

    def dataset_name(self, catalog: str, schema: str, table: str) -> tuple[str, str, str]:
        """(platform, name, env) of a Trino table's dataset."""
        m = self.catalogs.get(catalog)
        if m is None and self.external_catalog is not None and catalog:
            ext = self.external_catalog(catalog)
            if ext is not None:
                m = CatalogMapping(platform=ext["platform"], database=ext["database"])
        m = m or CatalogMapping()
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

    def who(self, ctx: dict) -> tuple[str, list[str]]:
        """The person a Trino request is for: a checked query's identity stands for the person it was checked for
        (an unknown or expired one for nobody: refused)."""
        ident_ = (ctx or {}).get("identity") or {}
        user = str(ident_.get("user") or "")
        if not user:
            raise HTTPException(400, "no Trino user")
        groups = [g if g.startswith("urn:li:corpGroup:") else f"urn:li:corpGroup:{g}" for g in ident_.get("groups") or []]
        if user.startswith("checked-"):
            reg = self._checked(user)
            if reg is None:
                raise HTTPException(403, "unknown or expired checked query")
            self._refuse_suspended(reg["actor"])
            return reg["actor"], sorted(set(groups) | set(reg["groups"]))
        app = self._app(user)
        if app is not None:                            # an installed app reads as its installer, bound tables only
            if not app.get("actor"):
                raise HTTPException(403, "this app is not installed")
            return app["actor"], []
        self._refuse_suspended(f"urn:li:corpuser:{user}")
        return f"urn:li:corpuser:{user}", groups

    def _app(self, user: str) -> Optional[dict]:
        if self.app_identity is None or not user.startswith("cairnapp-"):
            return None
        try:
            return self.app_identity(user) or {"actor": None, "tables": set()}
        except Exception as e:  # noqa: BLE001 - fail closed: an app that cannot be looked up reads nothing
            logger.warning("app identity lookup failed: %s", e)
            return {"actor": None, "tables": set()}

    @staticmethod
    def _raw_user(inp: dict) -> str:
        return str((((inp or {}).get("context") or {}).get("identity") or {}).get("user") or "")

    def _checked(self, user: str) -> Optional[dict]:
        if self.checked is None or not user.startswith("checked-"):
            return None
        try:
            return self.checked(user)
        except Exception as e:  # noqa: BLE001 - fail closed: an identity that cannot be looked up is nobody
            logger.warning("checked query lookup failed: %s", e)
            return None

    def _policy_refuses(self, ctx: dict, actor: str, groups: list[str], t: dict, urn: str) -> bool:
        """A direct read of a table under an aggregation policy, or one naming a projection-protected column, is
        refused: such queries run through Cairn's query endpoint, which checks them (a checked identity may read
        the tables it was checked for)."""
        from cairn.governance.aggregation import PolicyViolation, from_obligations

        try:
            pol = from_obligations(self.decide(actor, groups, READ, urn).obligations)
        except PolicyViolation as e:
            logger.warning("aggregation/projection policy on %s is misconfigured: %s", urn, e)
            return True
        except Exception as e:  # noqa: BLE001 - fail closed
            logger.warning("aggregation/projection policy check failed for %s on %s: %s", actor, urn, e)
            return True
        if pol is None:
            return False
        cols = {str(c).lower() for c in t.get("columns") or []}
        if not pol.aggregation and not (pol.projection & cols):
            return False
        reg = self._checked(str(((ctx or {}).get("identity") or {}).get("user") or ""))
        if reg is not None and urn in reg["tables"]:
            return False
        logger.info("direct read of %s refused for %s: it has an aggregation or projection policy", urn, actor)
        return True

    def allowed(self, actor: str, groups: list[str], privilege: str, urn: Optional[str]) -> bool:
        try:
            return self.decide(actor, groups, privilege, urn).decision == "ALLOW"
        except Exception as e:  # fail closed
            logger.warning("trino access decision failed (%s %s %s): %s", actor, privilege, urn, e)
            return False

    def restricted(self, actor: str, groups: list[str], urn: str) -> bool:
        """Does a read of ``urn`` by the caller come with what only a governed query applies: a row filter, a column
        mask, or an aggregation or projection policy? It raises when it cannot tell."""
        from cairn.governance.aggregation import PolicyViolation, from_obligations

        d = self.decide(actor, groups, READ, urn)
        if any(o.type == "row_filter" for o in d.obligations):
            return True
        try:
            if from_obligations(d.obligations) is not None:
                return True
        except PolicyViolation:                   # misconfigured: never read as no policy
            return True
        return bool(self.plan_for(actor, urn).active)

    def source_of(self, urn: str) -> Optional[str]:
        """The table whose raw values ``urn`` holds (``derived_source``), or None. It raises when it cannot tell."""
        return self.derived_source(urn) if self.derived_source is not None else None

    def _derived_refuses(self, actor: str, groups: list[str], urn: str) -> bool:
        """A read of a table holding another's raw values (a vector index's chunks) is refused unless the caller reads
        that source as it is: with READ on it, and no mask, row filter or aggregation policy there."""
        try:
            src = self.source_of(urn)
            if not src:
                return False
            if self.allowed(actor, groups, READ, src) and not self.restricted(actor, groups, src):
                return False
        except Exception as e:  # noqa: BLE001 - fail closed
            logger.warning("derived table check failed for %s on %s: %s", actor, urn, e)
            return True
        logger.info("read of %s refused for %s: it holds %s's raw values, which %s does not read unrestricted",
                    urn, actor, src, actor)
        return True

    @staticmethod
    def function_urn(f: dict) -> str:
        """A stored function's resource: ``urn:li:function:<catalog>.<schema>.<name>``."""
        return "urn:li:function:" + ".".join(str(f.get(k) or "").lower() for k in ("catalogName", "schemaName", "functionName"))

    def table_urn(self, t: dict) -> str:
        return self.urn(t.get("catalogName", ""), t.get("schemaName", ""), t.get("tableName", ""))

    def _resident(self, t: dict) -> bool:
        if self.resident is None:
            return True
        try:
            return bool(self.resident(str(t.get("catalogName") or ""), str(t.get("schemaName") or "")))
        except Exception as e:  # noqa: BLE001 - fail closed: residency that cannot be checked refuses
            logger.warning("residency check failed for %s: %s", self._tkey(t), e)
            return False

    def _room_tables(self, user: str) -> Optional[set[str]]:
        if self.cleanroom_tables is None or not user.startswith("cleanroom-"):
            return None
        return self.cleanroom_tables(user.removeprefix("cleanroom-"))

    @staticmethod
    def _tkey(t: dict) -> str:
        return ".".join(str(t.get(k) or "") for k in ("catalogName", "schemaName", "tableName")).lower()

    def _read_only(self, catalog: str) -> bool:
        """An external catalog not registered writable."""
        if self.external_catalog is None or not catalog or catalog in self.catalogs:
            return False
        ext = self.external_catalog(catalog)
        return ext is not None and not ext.get("writable")

    @staticmethod
    def _ambiguous(t: Any) -> bool:
        """A table name holding a dot: its dataset (``<schema>.<table>``, nested namespaces joined with dots) is
        another table's too (``"s"."x.y"`` is ``s.x``'s ``y``), so nothing decided on it would be its own."""
        return isinstance(t, dict) and "." in str(t.get("tableName") or "")

    def allow(self, inp: dict, warehouse: Optional[str] = None) -> bool:
        actor, groups = self.who(inp.get("context"))
        action = inp.get("action") or {}
        op = action.get("operation", "")
        res = action.get("resource") or {}
        if self._ambiguous(res.get("table")) or self._ambiguous((action.get("targetResource") or {}).get("table")):
            logger.info("%s refused: a table name with a dot names another table's dataset too", op)
            return False
        user = actor.removeprefix("urn:li:corpuser:")
        if user == CATALOG_ADMIN or op in ("CreateCatalog", "DropCatalog"):
            if user != CATALOG_ADMIN:
                return False
            if op in CATALOG_ADMIN_OPS:
                return True
            t = res.get("table") or {}            # SHOW CATALOGS reads Trino's system tables
            return op in TABLE_OPS and TABLE_OPS[op] in (VIEW, READ) and t.get("catalogName") == "system"
        room = self._room_tables(user)
        if op == "ExecuteQuery" and warehouse is not None:   # a team's warehouse: its members (and operators)
            if room is not None:                      # a clean room is no team's member: it runs on the shared Trino
                return False
            # An installed app's identity is its installer (who) once installed: it may run where they may.
            return user == CRAWLER or self.allowed(actor, groups, WAREHOUSE, f"urn:li:warehouse:{warehouse}")
        if room is not None:                          # a clean room: read its tables, and nothing else
            if op in ("ExecuteQuery", "AccessCatalog", "FilterCatalogs", "ShowSchemas", "FilterSchemas", "ShowTables"):
                return True
            if op == "ExecuteFunction":
                f = res.get("function") or {}
                return str(f.get("catalogName") or "") not in self.ai_catalogs | self.routine_catalogs \
                    and not self._raw_rows(f)              # no connector table functions: only aggregates leave
            t = res.get("table") or {}
            return op in TABLE_OPS and TABLE_OPS[op] in (VIEW, READ) and self._tkey(t) in room
        app = self._app(self._raw_user(inp))
        if app is not None:                           # an installed app: its bound tables, read-only, nothing else
            if op in ("ExecuteQuery", "AccessCatalog", "FilterCatalogs", "ShowSchemas", "FilterSchemas", "ShowTables"):
                return True
            if op == "ExecuteFunction":
                f = res.get("function") or {}
                return str(f.get("catalogName") or "") not in self.ai_catalogs | self.routine_catalogs \
                    and not self._raw_rows(f)
            t = res.get("table") or {}
            if not (op in TABLE_OPS and TABLE_OPS[op] in (VIEW, READ) and t and "$" not in str(t.get("tableName") or "")
                    and self.table_urn(t) in app["tables"]):
                return False
            # Bound: decided below as the installer (their policies, residency, holds, query policies).
        if op == "SetSystemSessionProperty":
            prop = str((res.get("systemSessionProperty") or {}).get("name") or "")
            if prop in LIMIT_SESSION_PROPERTIES:
                return user != CRAWLER and self.allowed(actor, groups, SYSTEM, None)
            return True
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
            if self._raw_rows(f):          # e.g. table_changes, query: rows that escape masks and row filters
                return user != CRAWLER and self.allowed(actor, groups, SYSTEM, None)
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
        if op in OWNER_OPS and not self._to_self(action, user) and not self.allowed(actor, groups, SYSTEM, None):
            logger.info("%s by %s refused: handing it to another principal needs %s", op, user, SYSTEM)
            return False
        if op in SCHEMA_OPS:
            if op in ("RenameSchema", "SetSchemaAuthorization"):   # a schema renamed into or out of the metrics one
                said = self._metric_location(inp, op, [res.get("schema") or {},
                                                       (action.get("targetResource") or {}).get("schema") or {}], set())
                if said is not None:
                    return said
            if self._in_system_catalog(str((res.get("schema") or {}).get("catalogName") or "")):
                return False                           # the system catalog is Cairn's to change
            try:
                if self._read_only(str((res.get("schema") or {}).get("catalogName") or "")):
                    return False
            except Exception as e:  # noqa: BLE001 - fail closed
                logger.warning("external catalog lookup failed: %s", e)
                return False
            return self.allowed(actor, groups, MANAGE, None)
        if op in SYSTEM_OPS:
            if op == "ExecuteProcedure":               # a connector's procedures change its catalog (unregister_table)
                try:
                    if self._read_only(str((res.get("function") or {}).get("catalogName") or "")):
                        logger.info("ExecuteProcedure refused: the external catalog is read-only")
                        return False
                except Exception as e:  # noqa: BLE001 - fail closed
                    logger.warning("external catalog lookup failed: %s", e)
                    return False
            return self.allowed(actor, groups, SYSTEM, None)
        if op in TABLE_OPS:
            t = res.get("table")
            if not t:
                return False
            # Trino's own metadata tables: what they show is filtered table by table (FilterTables,
            # FilterColumns, FilterViewQueryOwnedBy), so reading them is open; changing them is not.
            if (t.get("schemaName") == "information_schema" or t.get("catalogName") == "system") and TABLE_OPS[op] in (VIEW, READ):
                return True
            if TABLE_OPS[op] not in (VIEW, READ):
                # A read of a view in the metrics schema is decided on its metric's table: only Cairn's own service
                # identity creates (or replaces) and drops views there; nobody makes, renames, swaps or re-owns one.
                rename = op in RENAME_OPS
                places = [t] + ([(action.get("targetResource") or {}).get("table") or {}] if rename else [])
                said = self._metric_location(inp, op, places, set() if rename else METRIC_VIEW_DDL)
                if said is not None:
                    return said
            name = str(t.get("tableName") or "")
            if TABLE_OPS[op] not in (VIEW, READ) and self._in_system_catalog(str(t.get("catalogName") or "")):
                logger.info("%s on %s refused: system tables are read-only", op, self._tkey(t))
                return False
            if TABLE_OPS[op] not in (VIEW, READ):
                try:
                    if self._read_only(str(t.get("catalogName") or "")):
                        logger.info("%s on %s refused: the external catalog is read-only", op, self._tkey(t))
                        return False
                except Exception as e:  # noqa: BLE001 - fail closed
                    logger.warning("external catalog lookup failed for %s: %s", self._tkey(t), e)
                    return False
            if TABLE_OPS[op] != VIEW and not self._resident(t):
                logger.info("%s on %s refused: its data is pinned to another region", op, self._tkey(t))
                return False
            if "$" in name:                            # an Iceberg metadata table: decided on its table
                base, meta = name.split("$", 1)
                if TABLE_OPS[op] not in (VIEW, READ):
                    return False
                urn = self.table_urn({**t, "tableName": base})
                # Snapshots, history and refs hold no row values; files, manifests, partitions and the rest
                # carry column bounds that can reveal masked values: the table's managers only.
                need = TABLE_OPS[op] if self._value_free(t, meta) else MANAGE
                ok = self.allowed(actor, groups, need, urn) and not self._derived_refuses(actor, groups, urn)
                # What they show is the table's (bounds, partition values): copies of it follow its pin too.
                if ok and self.copies is not None and TABLE_OPS[op] == READ:
                    ok = self._copy_allowed(op, inp.get("context"), t)
                return ok
            if op in HOLD_OPS and self.held is not None:
                try:
                    on_hold = self.held(self.table_urn(t))
                except Exception as e:  # fail closed: a hold that cannot be checked holds
                    logger.warning("legal hold check failed for %s: %s", self.table_urn(t), e)
                    on_hold = True
                if on_hold:
                    logger.info("%s on %s refused: a legal hold is on", op, self.table_urn(t))
                    return False
            ok = self.allowed(actor, groups, TABLE_OPS[op], self._decided_on(t, TABLE_OPS[op]))
            if ok and TABLE_OPS[op] == READ and self._policy_refuses(inp.get("context"), actor, groups, t,
                                                                    self._decided_on(t, READ)):
                return False
            if ok and TABLE_OPS[op] == READ and self._derived_refuses(actor, groups, self.table_urn(t)):
                return False
            if ok and op in RENAME_OPS:
                target = (action.get("targetResource") or {}).get("table")
                ok = bool(target) and self.allowed(actor, groups, MANAGE, self.table_urn(target))
            if ok and self.copies is not None and (TABLE_OPS[op] == READ or op in COPY_OPS):
                ok = self._copy_allowed(op, inp.get("context"), t)
            return ok
        logger.info("trino operation %s is not known to Cairn: refused", op)
        return False

    @staticmethod
    def _to_self(action: dict, user: str) -> bool:
        """Does an ownership change (``grantee``: Trino's ``{"principals": [{"name", "type"}]}``) name the caller alone?"""
        g = action.get("grantee") or {}
        ps = g.get("principals") if isinstance(g, dict) else None
        if ps is None and isinstance(g, dict) and g.get("principal"):
            ps = [g["principal"]]
        if not isinstance(ps, list) or not ps:
            return False
        return all(isinstance(p, dict) and str(p.get("type") or "USER").upper() == "USER" and p.get("name") == user
                   for p in ps)

    def _copy_allowed(self, op: str, ctx: Optional[dict], t: dict) -> bool:
        """Data residency follows copies: a statement reading a pinned table writes only where its pin allows."""
        qid = str((ctx or {}).get("queryId") or "") or None
        try:
            why = self.copies(op, qid, str(t.get("catalogName") or ""), str(t.get("schemaName") or ""))
        except Exception as e:  # noqa: BLE001 - fail closed: a copy that cannot be followed is refused
            logger.warning("residency copy check failed for %s on %s: %s", op, self._tkey(t), e)
            return False
        if why:
            logger.info("%s on %s refused (data residency): %s", op, self._tkey(t), why)
            return False
        return True

    @staticmethod
    def _raw_rows(f: dict) -> bool:
        """A connector's own function (``<catalog>.system.<name>``) that is not known to be value-free: such
        table functions (Iceberg's and Delta's ``table_changes``, JDBC connectors' ``query``) return a
        table's or a source's rows directly, where Cairn's masks and row filters do not reach."""
        catalog, schema = str(f.get("catalogName") or ""), str(f.get("schemaName") or "")
        return catalog != "system" and schema == "system" and str(f.get("functionName") or "") not in SAFE_CONNECTOR_FUNCTIONS

    def _value_free(self, t: dict, meta: str) -> bool:
        """Is metadata table ``<table>$<meta>`` free of row values? Delta Lake's ``$history`` is not: its
        ``operation_parameters`` hold each DELETE's, UPDATE's or MERGE's predicate, with the values it named."""
        if meta not in SAFE_METADATA:
            return False
        if meta != "history":
            return True
        try:                                          # the catalog's platform (a registered external catalog too)
            platform = self.dataset_name(str(t.get("catalogName") or ""), "", "")[0]
        except Exception as e:  # noqa: BLE001 - fail closed: a catalog that cannot be told is treated as Delta
            logger.warning("catalog lookup failed for %s: %s", self._tkey(t), e)
            return False
        return not str(platform).lower().startswith("delta")

    def _may_view(self, actor: str, groups: list[str], t: dict) -> bool:
        """VIEW on a table; for an Iceberg metadata table, on its table (MANAGE for those showing values)."""
        name = str(t.get("tableName") or "")
        if "$" in name:
            base, meta = name.split("$", 1)
            return self.allowed(actor, groups, VIEW if self._value_free(t, meta) else MANAGE,
                                self.table_urn({**t, "tableName": base}))
        return self.allowed(actor, groups, VIEW, self._decided_on(t, VIEW))

    def batch(self, inp: dict) -> list[int]:
        """The indexes of ``filterResources`` (of the one table's columns, for FilterColumns) the caller may see; a
        table whose name holds a dot is never among them (``_ambiguous``)."""
        out = self._batch(inp)
        action = inp.get("action") or {}
        items = action.get("filterResources") or []
        if action.get("operation") == "FilterColumns":
            return [] if items and self._ambiguous((items[0] or {}).get("table")) else out
        return [i for i in out if 0 <= i < len(items) and not self._ambiguous((items[i] or {}).get("table"))]

    def _batch(self, inp: dict) -> list[int]:
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
        app = self._app(self._raw_user(inp))
        if app is not None and op in ("FilterTables", "FilterColumns"):
            bound = app["tables"]
            if op == "FilterTables":
                return [i for i, r in enumerate(items) if r.get("table") and self.table_urn(r["table"]) in bound
                        and self._may_view(actor, groups, r["table"])]
            t = (items[0] if items else {}).get("table") or {}
            ok = t and self.table_urn(t) in bound and self._may_view(actor, groups, t)
            return list(range(len(t.get("columns") or []))) if ok else []
        if app is not None and op == "FilterViewQueryOwnedBy":   # its own queries only, never its installer's rights
            me = self._raw_user(inp)
            return [i for i, r in enumerate(items) if (r.get("user") or {}).get("user") == me]
        if op in ("FilterCatalogs", "FilterSchemas", "FilterFunctions"):
            return list(range(len(items)))
        if user == CRAWLER and op in ("FilterTables", "FilterColumns"):
            t = (items[0] if items else {}).get("table") or {}
            return list(range(len(t.get("columns") or []))) if op == "FilterColumns" else list(range(len(items)))
        if op == "FilterTables":
            return [i for i, r in enumerate(items) if r.get("table") and self._may_view(actor, groups, r["table"])]
        if op == "FilterColumns":
            t = (items[0] if items else {}).get("table") or {}
            cols = t.get("columns") or []
            return list(range(len(cols))) if t and self._may_view(actor, groups, t) else []
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
        if self._ambiguous(t):
            return [{"expression": "false"}]          # never read (allow refuses it): nothing passes
        if not t or "$" in str(t.get("tableName") or ""):
            return []                                 # Iceberg metadata tables hold no rows to filter (see allow)
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
        spec = self._system_spec(t.get("catalogName", ""), t.get("schemaName", ""), t.get("tableName", ""))
        if spec is not None and spec.owner_filtered and not self.allowed(actor, groups, SYSTEM, None):
            out.append({"expression": f"user_name = {quote(user)}"})   # a person's own rows (fails closed: theirs)
        consent = getattr(self, "consent", None)  # (actor, urn, dialect, groups) -> filters (cairn.governance.consent)
        if consent is not None:
            try:
                out += [{"expression": e} for e in consent(actor, urn, "trino", groups)]
            except Exception as e:  # noqa: BLE001 - fail closed: a filter nothing passes
                logger.warning("consent filters failed for %s on %s: %s", actor, urn, e)
                return [{"expression": "false"}]
        return out

    def column_mask(self, inp: dict) -> Optional[dict]:
        if self._room_tables(self.who(inp.get("context"))[0].removeprefix("urn:li:corpuser:")) is not None:
            return None
        actor, groups = self.who(inp.get("context"))
        col = ((inp.get("action") or {}).get("resource") or {}).get("column") or {}
        if self._ambiguous(col):                      # never read (allow refuses it): NULL
            return {"expression": f"CAST(NULL AS {col.get('columnType', 'varchar')})"}
        if not col or "$" in str(col.get("tableName") or ""):
            return None                               # metadata tables: no row values (or managers only)
        urn = self.urn(col.get("catalogName", ""), col.get("schemaName", ""), col.get("tableName", ""))
        spec = self._system_spec(col.get("catalogName", ""), col.get("schemaName", ""), col.get("tableName", ""))
        if spec is not None and col.get("columnName", "") in spec.sensitive:
            from cairn.dataaccess.system_tables import TEXT_PRIVILEGE

            if not self.allowed(actor, groups, TEXT_PRIVILEGE, urn):   # fails closed: masked
                return {"expression": mask_expression(col.get("columnName", ""), col.get("columnType", "varchar"),
                                                      "redact")}
        try:
            plan = self.plan_for(actor, urn)
        except Exception as e:  # fail closed
            logger.warning("trino mask plan failed for %s on %s: %s", actor, urn, e)
            return {"expression": f"CAST(NULL AS {col.get('columnType', 'varchar')})"}
        name = col.get("columnName", "")
        hit = masked_path(plan, name)
        if hit is None:
            return None
        return {"expression": mask_expression(name, col.get("columnType", "varchar"), plan.strategy_for(hit))}


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
                try:
                    service = p is not None and access.service_identity(p.urn)
                except Exception as e:  # noqa: BLE001 - fail closed: no code when it cannot be told
                    logger.warning("trino sign-in: cannot tell Cairn's service identities: %s", e)
                    return back(redirect_uri, error="temporarily_unavailable",
                                error_description="Cairn cannot check this sign-in right now", state=state)
                if p is None or p.via not in ("session", "dev") or service:
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
            try:
                service = access.service_identity(p.urn)
            except Exception as e:  # noqa: BLE001 - fail closed: no token when it cannot be told
                logger.warning("trino token: cannot tell Cairn's service identities: %s", e)
                raise HTTPException(503, "Cairn cannot issue Trino tokens right now")
            # Cairn's own Trino identities (the crawler, the catalog administrator, connections' service users) have
            # rights in Trino no person has: nobody signed in under one of those names is issued a token as it.
            if p.is_service or p.via in ("agent", "extension") or service:
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

    @router.post("/{given}/warehouse/{warehouse}/admit")
    async def warehouse_admit(given: str, warehouse: str, request: Request):
        """For the warehouse proxy, before it starts a warehouse or counts a request as use: is ``token`` a Trino
        token Cairn issued, for someone the warehouse would run a query for (as its own ``allow`` decides)?"""
        check(given)
        name = wh_name(warehouse)
        doc = await request.json()
        token = doc.get("token") if isinstance(doc, dict) else None
        if tokens is None or not isinstance(token, str) or not token:
            return {"result": False}
        try:
            claims = tokens.verify(token, "trino")
        except ValueError:
            return {"result": False}
        inp = {"context": {"identity": {"user": str(claims["sub"]), "groups": []}}, "action": {"operation": "ExecuteQuery"}}
        try:
            ok = bool(await run_in_threadpool(access.allow, inp, name))
        except HTTPException:                          # nobody Cairn knows (an expired checked query, an app not installed)
            ok = False
        return {"result": ok, "expires_at": claims.get("exp")}

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
