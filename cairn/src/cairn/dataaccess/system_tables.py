"""System tables: Cairn's own operational data as SQL tables in Trino (``cairn_system.<schema>.<table>``).

Cairn materialises its audit log, refused access decisions, Trino query history, costs, lineage, warehouse usage
and job runs into Iceberg tables of a catalog of their own (a Cairn Catalog holding only these tables), and Trino
reads them as the catalog ``cairn_system``. They are tables like any other: Trino asks Cairn before every read
(the OPA plugin), so the policy engine decides who may see them, masks apply and row filters apply.

* **Why materialised, not a view over Cairn's database.** Trino never gets credentials to Cairn's database, a
  read needs nothing of Cairn's database at query time, data that is not in Cairn's database (lineage, kept in
  the metadata graph; team costs, computed) is there too, the tables keep history longer than the sources do
  (query history keeps 30 days), and each sync is an Iceberg snapshot: ``synced_at`` on every row and the
  table's snapshots are an incremental change feed.
* **Governance (built in, on top of policies).** Reading a system table needs ``VIEW_DATASET_PROFILE`` on it,
  like any table (no policy, no access: refused). Tables with a ``user_name`` column show a person only their
  own rows unless they hold ``MANAGE_SYSTEM_OPERATIONS``; columns that may hold literals people typed
  (``statement``, ``error``) are redacted unless the reader holds ``VIEW_QUERY_TEXT`` on the table. Through Trino
  the tables are read-only for everyone. A decision that cannot be had filters every row and masks the column.
* **Freshness.** A sync runs every ``CAIRN_SYSTEM_TABLES_INTERVAL_SECONDS`` (60) on the scheduler's leader; a
  change is in its table within that interval plus 60 seconds (two minutes by default).
* **Retention.** ``CAIRN_SYSTEM_TABLES_RETENTION_DAYS`` (90) by default; per table with
  ``PUT /v1/system-tables/<schema>/<table>/retention``, kept on the table (``system-table.retention-days``).

Settings: ``CAIRN_SYSTEM_TABLES_CATALOG`` (pyiceberg REST catalog properties, JSON or a file: uri, token, s3.*),
``CAIRN_SYSTEM_TABLES_TRINO_CATALOG`` (``cairn_system``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

DAY_MS = 86_400_000
CATALOG = "cairn_system"
TEXT_PRIVILEGE = "VIEW_QUERY_TEXT"
SYSTEM_PRIVILEGE = "MANAGE_SYSTEM_OPERATIONS"
OWNER_COLUMN = "user_name"


@dataclass(frozen=True)
class Col:
    name: str
    type: str          # string, long, double, timestamp, date, strings (a list of strings)
    doc: str
    sensitive: bool = False


@dataclass(frozen=True)
class Spec:
    schema: str
    table: str
    description: str
    columns: tuple[Col, ...]
    mode: str                        # append: new rows by key; replace: the retention window recomputed
    key: Optional[str] = None        # append: what makes a row new
    order: Optional[str] = None      # append: the column the source is read by (and retention applies to)
    lookback: int = 0                # append: how far back to look again (late rows), in the order column's units
    time: str = "synced_at"          # the column retention applies to

    @property
    def name(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def owner_filtered(self) -> bool:
        return any(c.name == OWNER_COLUMN for c in self.columns)

    @property
    def sensitive(self) -> set[str]:
        return {c.name for c in self.columns if c.sensitive}


SYNCED = Col("synced_at", "timestamp", "When Cairn wrote this row into the system table (the change feed: read rows "
                                       "with a later synced_at).")
USER = Col(OWNER_COLUMN, "string", "The person, as their Trino user name (their URN without urn:li:corpuser:). People "
                                   "without MANAGE_SYSTEM_OPERATIONS see only rows with their own name.")

SPECS: tuple[Spec, ...] = (
    Spec("audit", "events", "Cairn's audit log, hash-chained: every change, sign-in, export and refused action, in order.", (
        Col("seq", "long", "Position in the chain (ascending)."),
        Col("event_id", "string", "The entry's id."),
        Col("ts", "timestamp", "When it happened (UTC)."),
        Col("category", "string", "Area: authz, governance, trino, flows, ..."),
        Col("action", "string", "What was done."),
        Col("actor", "string", "Who did it (a URN)."),
        USER,
        Col("subject", "string", "What it was done to (a URN or a name)."),
        Col("details", "string", "Details, as JSON."),
        Col("prev_hash", "string", "The previous entry's hash."),
        Col("hash", "string", "sha256(prev_hash || the entry): verify the chain with GET /v1/audit/verify."),
        SYNCED), mode="append", key="seq", order="seq", lookback=1000, time="ts"),
    Spec("access", "decisions", "Access the policy engine refused (an explicit deny policy matched), from the audit log. "
                                "Allowed decisions are not recorded.", (
        Col("seq", "long", "The audit entry's position."),
        Col("ts", "timestamp", "When (UTC)."),
        Col("actor", "string", "Who asked (a URN)."),
        USER,
        Col("privilege", "string", "The privilege asked for, e.g. VIEW_DATASET_PROFILE."),
        Col("resource_urn", "string", "What it was asked for (empty: no particular resource)."),
        Col("decision", "string", "DENY."),
        Col("policies", "strings", "The policies that refused it."),
        SYNCED), mode="append", key="seq", order="seq", lookback=1000, time="ts"),
    Spec("query", "history", "Queries Trino ran (its event listener), with their figures and price.", (
        Col("query_id", "string", "Trino's query id."),
        USER,
        Col("source", "string", "The client (cairn, trino-cli, a BI tool...)."),
        Col("state", "string", "FINISHED or FAILED."),
        Col("error_code", "string", "Trino's error code, when it failed."),
        Col("error", "string", "The error message (may quote the statement: masked without VIEW_QUERY_TEXT).", True),
        Col("statement", "string", "The SQL as typed (masked without VIEW_QUERY_TEXT).", True),
        Col("statement_hash", "string", "sha256 of the SQL: the same statement has the same hash."),
        Col("fingerprint", "string", "The statement's shape, literals left out: the same report with other values shares it."),
        Col("tables", "strings", "Datasets it read (URNs)."),
        Col("created_at", "timestamp", "When it was submitted (UTC)."),
        Col("ended_at", "timestamp", "When it ended (UTC)."),
        Col("wall_ms", "long", "Elapsed time, ms."),
        Col("cpu_ms", "long", "CPU time, ms."),
        Col("queued_ms", "long", "Time queued, ms."),
        Col("input_bytes", "long", "Bytes read."),
        Col("input_rows", "long", "Rows read."),
        Col("output_rows", "long", "Rows returned."),
        Col("peak_memory_bytes", "long", "Peak memory."),
        Col("warehouse", "string", "The Trino that ran it (the shared one or a team warehouse)."),
        Col("cost_usd", "double", "Its price at CAIRN_COST_TRINO_USD_PER_CPU_HOUR and _PER_TB_SCANNED (null: not priced)."),
        SYNCED), mode="append", key="query_id", order="created_ms", lookback=DAY_MS, time="created_at"),
    Spec("billing", "usage", "Costs per day: Trino queries per person and warehouse, tables (storage and compute), "
                             "teams, engine runs and AI usage. The figures of /reports/costs and /reports/costs/teams.", (
        Col("day", "date", "The day (UTC)."),
        Col("kind", "string", "query (a person's Trino queries on a warehouse), table, team, run, or ai."),
        USER,
        Col("team", "string", "kind team: the team (a group URN, or (no team))."),
        Col("warehouse", "string", "kind query: the Trino; kind run: the engine or warehouse."),
        Col("table_urn", "string", "kind table: the dataset."),
        Col("job", "string", "kind run: the job or application."),
        Col("model", "string", "kind ai: the model."),
        Col("queries", "long", "Queries (kinds query and team)."),
        Col("cpu_ms", "long", "CPU time, ms."),
        Col("scanned_bytes", "long", "Bytes scanned."),
        Col("storage_bytes", "long", "Bytes stored (kind table)."),
        Col("input_tokens", "long", "Tokens sent to the model (kind ai)."),
        Col("output_tokens", "long", "Tokens the model returned (kind ai)."),
        Col("cost_usd", "double", "Cost, USD (null: not priced)."),
        SYNCED), mode="replace", time="day"),
    Spec("lineage", "table_lineage", "Table lineage: each table and the tables it is made from, as the catalog has it "
                                     "(declared on the table, or recorded from the jobs and queries that wrote it).", (
        Col("downstream_urn", "string", "The table made."),
        Col("upstream_urn", "string", "A table it is made from."),
        Col("lineage_type", "string", "TRANSFORMED, COPY, VIEW... (JOB: recorded from a job or query)."),
        Col("job_urn", "string", "The job that wrote it (a Trino query's job is named after what it writes)."),
        SYNCED), mode="replace"),
    Spec("lineage", "column_lineage", "Column lineage: each column and the columns it is computed from.", (
        Col("downstream_urn", "string", "The table made."),
        Col("downstream_column", "string", "The column made."),
        Col("upstream_urn", "string", "The table it comes from."),
        Col("upstream_column", "string", "The column it comes from."),
        Col("transform", "string", "How (IDENTITY, TRANSFORMATION, an expression...), when known."),
        Col("job_urn", "string", "The job that computed it, when recorded from one."),
        SYNCED), mode="replace"),
    Spec("compute", "warehouse_events", "Team warehouses: started by hand, and their node-seconds per reporting period "
                                        "(the proxy scales workers with the load; nodes is the period's average).", (
        Col("event_time", "timestamp", "When (the end of the period for usage)."),
        Col("warehouse", "string", "The warehouse."),
        Col("event", "string", "usage or started."),
        Col("period_start", "timestamp", "usage: the period's start."),
        Col("period_end", "timestamp", "usage: the period's end."),
        Col("nodes", "double", "usage: average nodes running (coordinator and workers)."),
        Col("node_seconds", "double", "usage: node-seconds."),
        Col("cost_usd", "double", "usage: at CAIRN_COST_WAREHOUSE_USD_PER_NODE_HOUR."),
        Col("actor", "string", "started: who started it."),
        SYNCED), mode="replace", time="event_time"),
    Spec("jobs", "runs", "Runs of Cairn Flows, scheduled jobs (notebooks, Python) and SQL workflows.", (
        Col("run_id", "string", "The run's id."),
        Col("kind", "string", "flow, job or sql_workflow."),
        Col("name", "string", "The flow, job or workflow."),
        USER,
        Col("run_as", "string", "Whose rights it ran with."),
        Col("trigger", "string", "schedule, manual, dataset, backfill..."),
        Col("status", "string", "Its state: queued, running, succeeded, failed, cancelled..."),
        Col("created_at", "timestamp", "When it was created (UTC)."),
        Col("started_at", "timestamp", "When it started."),
        Col("finished_at", "timestamp", "When it finished."),
        Col("duration_ms", "long", "Finished minus started, ms."),
        Col("error", "string", "Why it failed."),
        SYNCED), mode="replace", time="created_at"),
)
BY_NAME = {s.name: s for s in SPECS}


def _settings(name: str) -> dict:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    return json.loads(open(raw).read() if not raw.startswith("{") else raw)


def user_name(actor: Optional[str]) -> str:
    a = str(actor or "")
    return a.removeprefix("urn:li:corpuser:") if a.startswith("urn:li:corpuser:") else ""


def _ts(ms: Optional[int]) -> Optional[datetime]:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms is not None else None


def _iso(v: Optional[str]) -> Optional[datetime]:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class SystemTablesError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Status:
    synced_ms: Optional[int] = None      # the last successful sync in this process
    written_ms: Optional[int] = None     # the last snapshot written (any process)
    rows: Optional[int] = None
    error: Optional[str] = None


class SystemTables:
    """Materialises Cairn's operational data into the system catalog, and answers what governance needs."""

    def __init__(self, engine, catalog_props: dict, graph=None, groups_of: Optional[Callable[[str], list[str]]] = None,
                 trino_catalog: str = CATALOG, interval_s: Optional[float] = None, retention_days: Optional[int] = None,
                 audit=None):
        self.engine, self.catalog_props, self.graph, self.groups_of = engine, dict(catalog_props), graph, groups_of
        self.trino_catalog = trino_catalog
        self.interval_s = interval_s if interval_s is not None else float(
            os.environ.get("CAIRN_SYSTEM_TABLES_INTERVAL_SECONDS", "60"))
        self.default_retention = retention_days or int(os.environ.get("CAIRN_SYSTEM_TABLES_RETENTION_DAYS", "90"))
        self.audit = audit
        self.status: dict[str, Status] = {s.name: Status() for s in SPECS}
        self._lock = threading.Lock()
        self._last_run = 0.0
        self._last_maintenance = 0.0
        self._published = False

    @classmethod
    def from_env(cls, engine, graph=None, groups_of=None, audit=None) -> Optional["SystemTables"]:
        props = _settings("CAIRN_SYSTEM_TABLES_CATALOG")
        if not props:
            return None
        if not props.get("uri"):
            raise ValueError("CAIRN_SYSTEM_TABLES_CATALOG: give the catalog's uri (a Cairn Catalog of its own)")
        return cls(engine, props, graph, groups_of, os.environ.get("CAIRN_SYSTEM_TABLES_TRINO_CATALOG", CATALOG) or CATALOG,
                   audit=audit)

    # ---- governance -------------------------------------------------------------------------------
    def urn(self, name: str) -> str:
        return f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{self.trino_catalog}.{name},PROD)"

    def spec_of(self, catalog: str, schema: str, table: str) -> Optional[Spec]:
        """The system table a Trino table is, or None."""
        if str(catalog or "") != self.trino_catalog:
            return None
        return BY_NAME.get(f"{schema}.{table}".lower())

    # ---- the catalog -------------------------------------------------------------------------------
    def _catalog(self):
        from pyiceberg.catalog.rest import RestCatalog

        return RestCatalog("cairn-system-tables", **self.catalog_props)

    @staticmethod
    def iceberg_schema(spec: Spec):
        from pyiceberg.schema import Schema
        from pyiceberg.types import (DateType, DoubleType, ListType, LongType, NestedField, StringType,
                                     TimestamptzType)

        types = {"string": StringType, "long": LongType, "double": DoubleType, "timestamp": TimestamptzType,
                 "date": DateType}
        fields, fid = [], 1
        for c in spec.columns:
            if c.type == "strings":
                fields.append(NestedField(fid, c.name, ListType(fid + 1, StringType(), element_required=False),
                                          required=False, doc=c.doc))
                fid += 2
            else:
                fields.append(NestedField(fid, c.name, types[c.type](), required=False, doc=c.doc))
                fid += 1
        return Schema(*fields)

    @staticmethod
    def arrow_schema(spec: Spec):
        import pyarrow as pa

        types = {"string": pa.string(), "long": pa.int64(), "double": pa.float64(),
                 "timestamp": pa.timestamp("us", tz="UTC"), "date": pa.date32(), "strings": pa.list_(pa.string())}
        return pa.schema([pa.field(c.name, types[c.type], nullable=True) for c in spec.columns])

    def _table(self, cat, spec: Spec):
        """The table, created (with its descriptions) when missing."""
        from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchTableError, TableAlreadyExistsError

        ident = (spec.schema, spec.table)
        try:
            return cat.load_table(ident)
        except NoSuchTableError:
            pass
        try:
            cat.create_namespace((spec.schema,), {"comment": "Cairn system tables"})
        except NamespaceAlreadyExistsError:
            pass
        try:
            return cat.create_table(ident, schema=self.iceberg_schema(spec), properties={
                "comment": spec.description, "system-table": "true",
                "system-table.retention-days": str(self.default_retention)})
        except TableAlreadyExistsError:
            return cat.load_table(ident)

    def retention_days(self, tbl) -> int:
        try:
            return max(1, int(tbl.properties.get("system-table.retention-days") or self.default_retention))
        except ValueError:
            return self.default_retention

    # ---- sources -----------------------------------------------------------------------------------
    def _rows(self, spec: Spec, since_ms: int, after: Optional[int]) -> list[dict]:
        return getattr(self, "_src_" + spec.name.replace(".", "_"))(since_ms, after)

    def _select(self, stmt) -> list:
        with self.engine.connect() as conn:
            return list(conn.execute(stmt))

    def _src_audit_events(self, since_ms: int, after: Optional[int]) -> list[dict]:
        from sqlalchemy import select

        from cairn.common.audit import audit_log as a

        stmt = select(a).order_by(a.c.seq).limit(50_000)
        if after is not None:
            stmt = stmt.where(a.c.seq > after)
        out = []
        for r in self._select(stmt):
            ts = _iso(r.ts)
            if ts is not None and ts.timestamp() * 1000 < since_ms:
                continue
            out.append({"seq": r.seq, "event_id": r.id, "ts": ts, "category": r.category, "action": r.action,
                        "actor": r.actor, "user_name": user_name(r.actor), "subject": r.subject,
                        "details": json.dumps(r.details or {}, sort_keys=True, default=str),
                        "prev_hash": r.prev_hash, "hash": r.hash})
        return out

    def _src_access_decisions(self, since_ms: int, after: Optional[int]) -> list[dict]:
        from sqlalchemy import select

        from cairn.common.audit import audit_log as a

        stmt = select(a).where(a.c.category == "authz", a.c.action == "deny").order_by(a.c.seq).limit(50_000)
        if after is not None:
            stmt = stmt.where(a.c.seq > after)
        out = []
        for r in self._select(stmt):
            ts = _iso(r.ts)
            if ts is not None and ts.timestamp() * 1000 < since_ms:
                continue
            d = r.details or {}
            out.append({"seq": r.seq, "ts": ts, "actor": r.actor, "user_name": user_name(r.actor),
                        "privilege": str(d.get("privilege") or ""), "resource_urn": r.subject or "", "decision": "DENY",
                        "policies": [str(p) for p in d.get("policies") or []]})
        return out

    def _src_query_history(self, since_ms: int, after: Optional[int]) -> list[dict]:
        from sqlalchemy import select

        from cairn.dataaccess.query_history import price, query_history as q, trino_rates

        rates = trino_rates()
        stmt = select(q).where(q.c.create_ms >= max(since_ms, after or 0)).order_by(q.c.create_ms).limit(100_000)
        out = []
        for r in self._select(stmt):
            sql = r.sql or ""
            try:
                tables = [str(t) for t in json.loads(r.tables or "[]")]
            except ValueError:
                tables = []
            out.append({"query_id": r.query_id, "user_name": r.user, "source": r.source, "state": r.state,
                        "error_code": r.error_code, "error": r.error, "statement": sql,
                        "statement_hash": hashlib.sha256(sql.encode()).hexdigest(), "fingerprint": r.fingerprint,
                        "tables": tables, "created_at": _ts(r.create_ms), "ended_at": _ts(r.end_ms),
                        "wall_ms": r.wall_ms, "cpu_ms": r.cpu_ms, "queued_ms": r.queued_ms, "input_bytes": r.input_bytes,
                        "input_rows": r.input_rows, "output_rows": r.output_rows,
                        "peak_memory_bytes": r.peak_memory_bytes, "warehouse": r.server,
                        "cost_usd": price(r.cpu_ms, r.input_bytes, rates), "created_ms": r.create_ms})
        return out

    def _src_billing_usage(self, since_ms: int, after: Optional[int]) -> list[dict]:
        from sqlalchemy import func, select

        from cairn.ai.usage import usage_table as ai
        from cairn.dataaccess.query_history import price, query_history as q, trino_rates
        from cairn.reports.costs import CostService, asset_costs, run_costs

        first = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        rates = trino_rates()
        out: list[dict] = []

        def row(day: str, kind: str, **v: Any) -> dict:
            base = {"day": date.fromisoformat(day), "kind": kind, "user_name": None, "team": None, "warehouse": None,
                    "table_urn": None, "job": None, "model": None, "queries": None, "cpu_ms": None,
                    "scanned_bytes": None, "storage_bytes": None, "input_tokens": None, "output_tokens": None,
                    "cost_usd": None}
            base.update(v)
            return base
        per: dict[tuple, dict] = {}
        for r in self._select(select(q.c.user, q.c.server, q.c.create_ms, q.c.cpu_ms, q.c.input_bytes)
                              .where(q.c.create_ms >= since_ms)):
            day = datetime.fromtimestamp(r.create_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            a = per.setdefault((day, r.user, r.server or ""), {"queries": 0, "cpu_ms": 0, "scanned_bytes": 0})
            a["queries"] += 1
            a["cpu_ms"] += r.cpu_ms or 0
            a["scanned_bytes"] += r.input_bytes or 0
        for (day, user, server), a in sorted(per.items()):
            out.append(row(day, "query", user_name=user, warehouse=server, **a,
                           cost_usd=price(a["cpu_ms"], a["scanned_bytes"], rates)))
        for r in self._select(select(asset_costs).where(asset_costs.c.day >= first).order_by(asset_costs.c.day,
                                                                                            asset_costs.c.urn)):
            out.append(row(r.day, "table", table_urn=r.urn, warehouse=r.platform, storage_bytes=r.storage_bytes,
                           cost_usd=round((r.storage_usd or 0) + (r.compute_usd or 0), 6)))
        for r in self._select(select(run_costs).where(run_costs.c.day >= first, run_costs.c.parent.is_(None))
                              .order_by(run_costs.c.day, run_costs.c.run)):
            out.append(row(r.day, "run", job=r.job, warehouse=r.engine if r.engine != "warehouse"
                           else r.job.removeprefix("urn:li:warehouse:"), cost_usd=r.usd))
        if self.groups_of is not None:
            days = max(1, int((time.time() * 1000 - since_ms) // DAY_MS) + 1)
            teams = CostService(self.engine, self.graph, lambda u: False, lambda *a: None).teams_daily(
                days, groups_of=self.groups_of)
            for t in teams["rows"]:
                if t["day"] < first:
                    continue
                out.append(row(t["day"], "team", team=t["team"], queries=t["queries"],
                               cpu_ms=int(t["cpu_hours"] * 3_600_000), scanned_bytes=t["scanned_bytes"],
                               storage_bytes=t["storage_bytes"], cost_usd=t["total_usd"]))
        day_ms = (ai.c.ts_ms - ai.c.ts_ms % DAY_MS)
        for r in self._select(select(day_ms.label("d"), ai.c.actor, ai.c.model, func.sum(ai.c.input_tokens),
                                     func.sum(ai.c.output_tokens), func.sum(ai.c.cost_usd))
                              .where(ai.c.ts_ms >= since_ms).group_by(day_ms, ai.c.actor, ai.c.model)
                              .order_by(day_ms, ai.c.actor, ai.c.model)):
            day = datetime.fromtimestamp(int(r[0]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            out.append(row(day, "ai", user_name=user_name(r[1]) or r[1], model=r[2], input_tokens=int(r[3] or 0),
                           output_tokens=int(r[4] or 0), cost_usd=round(float(r[5] or 0), 6)))
        return out

    def _lineage(self) -> list[tuple[str, Optional[str], list[str], list[str], list[dict]]]:
        """(downstream, job, upstreams, types, fine-grained) from tables' upstreamLineage and jobs' inputs and outputs."""
        if self.graph is None:
            return []
        out = []
        for e in self.graph.scroll_entities("dataset", ["upstreamLineage"]):
            ul = e.get("upstreamLineage") or {}
            ups = [u for u in ul.get("upstreams") or [] if u.get("dataset")]
            out.append((e["urn"], None, [u["dataset"] for u in ups], [str(u.get("type") or "TRANSFORMED") for u in ups],
                        ul.get("fineGrainedLineages") or []))
        for e in self.graph.scroll_entities("dataJob", ["dataJobInputOutput"]):
            io = e.get("dataJobInputOutput") or {}
            ins = [str(u) for u in io.get("inputDatasets") or []]
            for o in io.get("outputDatasets") or []:
                fg = [f for f in io.get("fineGrainedLineages") or []
                      if any(str(d).startswith(f"urn:li:schemaField:({o},") for d in f.get("downstreams") or [])]
                out.append((str(o), e["urn"], ins, ["JOB"] * len(ins), fg))
        return out

    def _src_lineage_table_lineage(self, since_ms: int, after: Optional[int]) -> list[dict]:
        out = {}
        for down, job, ups, types, _ in self._lineage():
            for up, kind in zip(ups, types):
                out[(down, up, job or "")] = {"downstream_urn": down, "upstream_urn": up, "lineage_type": kind,
                                              "job_urn": job}
        return [out[k] for k in sorted(out)]

    def _src_lineage_column_lineage(self, since_ms: int, after: Optional[int]) -> list[dict]:
        def split(field_urn: str) -> tuple[str, str]:
            inner = str(field_urn).removeprefix("urn:li:schemaField:(").removesuffix(")")
            urn, _, col = inner.rpartition(",")
            return urn, col

        out = {}
        for down, job, _, _, fgs in self._lineage():
            for fg in fgs:
                for d in fg.get("downstreams") or []:
                    durn, dcol = split(d)
                    for u in fg.get("upstreams") or []:
                        uurn, ucol = split(u)
                        out[(durn or down, dcol, uurn, ucol, job or "")] = {
                            "downstream_urn": durn or down, "downstream_column": dcol, "upstream_urn": uurn,
                            "upstream_column": ucol, "transform": fg.get("transformOperation"), "job_urn": job}
        return [out[k] for k in sorted(out)]

    def _src_compute_warehouse_events(self, since_ms: int, after: Optional[int]) -> list[dict]:
        from sqlalchemy import select

        from cairn.common.audit import audit_log as a
        from cairn.reports.costs import run_costs

        first = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        out = []
        for r in self._select(select(run_costs).where(run_costs.c.engine == "warehouse", run_costs.c.day >= first)
                              .order_by(run_costs.c.run)):
            seconds = float(r.seconds or 0)
            out.append({"event": "usage", "warehouse": r.job.removeprefix("urn:li:warehouse:"), "nodes": r.cores,
                        "node_seconds": round(r.cores * seconds, 3), "cost_usd": r.usd, "actor": None,
                        "_end": r.day, "_seconds": seconds, "_id": r.run})
        # The proxy reports periods by id; the period's end is the report's day at the latest. Cairn keeps only the
        # day and length, so usage events carry the day's end and the length as their period.
        for o in out:
            end = datetime.fromisoformat(o.pop("_end")).replace(tzinfo=timezone.utc)
            o.pop("_id")
            seconds = o.pop("_seconds")
            o["event_time"] = o["period_end"] = end
            o["period_start"] = datetime.fromtimestamp(end.timestamp() - seconds, tz=timezone.utc)
        for r in self._select(select(a).where(a.c.category == "trino", a.c.action == "warehouse.started")
                              .order_by(a.c.seq)):
            ts = _iso(r.ts)
            if ts is None or ts.timestamp() * 1000 < since_ms:
                continue
            out.append({"event_time": ts, "warehouse": str(r.subject or "").removeprefix("urn:li:warehouse:"),
                        "event": "started", "period_start": None, "period_end": None, "nodes": None,
                        "node_seconds": None, "cost_usd": None, "actor": r.actor})
        return sorted(out, key=lambda r: (r["event_time"], r["warehouse"], r["event"]))

    def _src_jobs_runs(self, since_ms: int, after: Optional[int]) -> list[dict]:
        from sqlalchemy import select

        out = []

        def add(run_id, kind, name, by, run_as, trigger, status, created, started, finished, error):
            out.append({"run_id": run_id, "kind": kind, "name": name, "user_name": user_name(by) or user_name(run_as),
                        "run_as": run_as, "trigger": trigger, "status": status, "created_at": _ts(created),
                        "started_at": _ts(started), "finished_at": _ts(finished),
                        "duration_ms": (finished - started) if started is not None and finished is not None else None,
                        "error": (error or None) and str(error)[:4000]})
        try:
            from cairn.flows.engine import flow_runs as fr, flows as fl

            for r in self._select(select(fr.c.id, fl.c.name, fr.c.triggered_by, fr.c.run_as, fr.c.trigger, fr.c.status,
                                         fr.c.created_ms, fr.c.started_ms, fr.c.finished_ms, fr.c.error)
                                  .select_from(fr.join(fl, fl.c.id == fr.c.flow_id))
                                  .where(fr.c.created_ms >= since_ms)):
                add(r.id, "flow", r.name, r.triggered_by, r.run_as, r.trigger, r.status, r.created_ms, r.started_ms,
                    r.finished_ms, r.error)
        except ImportError:   # pragma: no cover - flows are part of the server
            pass
        from cairn.pipelines.jobs import job_runs as jr, jobs as jb

        for r in self._select(select(jr.c.id, jb.c.name, jr.c.as_user, jr.c.trigger, jr.c.status, jr.c.started_ms,
                                     jr.c.finished_ms, jr.c.error).select_from(jr.join(jb, jb.c.id == jr.c.job_id))
                              .where(jr.c.started_ms >= since_ms)):
            add(r.id, "job", r.name, r.as_user, r.as_user, r.trigger, r.status, r.started_ms, r.started_ms,
                r.finished_ms, r.error)
        from cairn.sqlworkspace.workflows import sql_workflow_runs as wr, sql_workflows as wf

        for r in self._select(select(wr.c.id, wf.c.name, wf.c.owner, wr.c.trigger, wr.c.status, wr.c.started_ms,
                                     wr.c.finished_ms).select_from(wr.join(wf, wf.c.id == wr.c.workflow_id))
                              .where(wr.c.started_ms >= since_ms)):
            add(r.id, "sql_workflow", r.name, None, r.owner, r.trigger, r.status, r.started_ms, r.started_ms,
                r.finished_ms, None)
        return sorted(out, key=lambda r: (r["created_at"] or datetime.min.replace(tzinfo=timezone.utc), r["run_id"]))

    # ---- sync --------------------------------------------------------------------------------------
    def run_due(self, now: Optional[float] = None) -> int:
        """Syncs every table when the interval has passed (the scheduler's leader calls this each tick)."""
        now = now if now is not None else time.time()
        if now - self._last_run < self.interval_s:
            return 0
        self._last_run = now
        if not self._published:
            try:                    # each table in the catalog with its descriptions, once Cairn Core answers
                self.publish_catalog()
                self._published = True
            except Exception as e:  # noqa: BLE001 - tried again next time
                logger.info("system tables not catalogued yet: %s", e)
        return sum(1 for r in self.sync().values() if r.get("ok"))

    def sync(self, only: Optional[Iterable[str]] = None) -> dict[str, dict]:
        """One pass over the tables (or ``only`` these): new rows appended, recomputed tables replaced when they
        changed, retention applied. A table that fails is reported and retried next time; the others go on."""
        with self._lock:
            cat = self._catalog()
            out: dict[str, dict] = {}
            maintain = time.time() - self._last_maintenance > 3600
            for spec in SPECS:
                if only is not None and spec.name not in set(only):
                    continue
                st = self.status[spec.name]
                try:
                    out[spec.name] = {"ok": True, **self._sync_one(cat, spec, maintain)}
                    st.synced_ms, st.error = int(time.time() * 1000), None
                except Exception as e:  # noqa: BLE001 - one table's failure must not stop the others
                    logger.warning("system table %s could not be synced: %s", spec.name, e)
                    st.error = str(e)[:500]
                    out[spec.name] = {"ok": False, "error": st.error}
            if maintain and only is None:
                self._last_maintenance = time.time()
            return out

    def _sync_one(self, cat, spec: Spec, maintain: bool) -> dict:
        import pyarrow as pa

        tbl = self._table(cat, spec)
        days = self.retention_days(tbl)
        now_ms = int(time.time() * 1000)
        since_ms = now_ms - days * DAY_MS
        synced = datetime.now(timezone.utc)
        snap = tbl.current_snapshot()
        summary = dict(snap.summary.additional_properties) if snap is not None and snap.summary else {}
        written = 0
        if spec.mode == "append":
            wm = self._watermark(tbl, spec)
            after = int(wm) - spec.lookback if wm not in (None, "") else None
            rows = self._rows(spec, since_ms, after)
            if after is not None and rows:
                rows = self._new_only(tbl, spec, rows, after)
            if rows:
                mark = max(int(r[spec.order] if spec.order in r else r[spec.key]) for r in rows)
                if wm not in (None, ""):
                    mark = max(mark, int(wm))
                for r in rows:
                    r["synced_at"] = synced
                data = pa.Table.from_pylist([{c.name: r.get(c.name) for c in spec.columns} for r in rows],
                                            schema=self.arrow_schema(spec))
                tbl.append(data, snapshot_properties={"cairn.watermark": str(mark)})
                written = len(rows)
            if maintain:
                self._retain(tbl, spec, since_ms)
        else:
            rows = self._rows(spec, since_ms, None)
            digest = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
            if digest != summary.get("cairn.content-hash") or snap is None:
                for r in rows:
                    r["synced_at"] = synced
                data = pa.Table.from_pylist([{c.name: r.get(c.name) for c in spec.columns} for r in rows],
                                            schema=self.arrow_schema(spec))
                with warnings.catch_warnings():      # an empty table replaced: nothing to delete is fine
                    warnings.filterwarnings("ignore", message="Delete operation did not match any records")
                    tbl.overwrite(data, snapshot_properties={"cairn.content-hash": digest})
                written = len(rows)
        if maintain:
            self._expire(tbl)
        st = self.status[spec.name]
        cur = tbl.current_snapshot()
        st.written_ms = cur.timestamp_ms if cur is not None else None
        return {"written": written, "retention_days": days}

    @staticmethod
    def _new_only(tbl, spec: Spec, rows: list[dict], after: int) -> list[dict]:
        """Rows not in the table yet (the lookback reads some again)."""
        from pyiceberg.expressions import GreaterThanOrEqual

        key_col = spec.key
        if spec.order == spec.key:
            flt = GreaterThanOrEqual(spec.key, after)
        else:  # the order column is in milliseconds: the table's timestamp column
            col = spec.time
            flt = GreaterThanOrEqual(col, datetime.fromtimestamp(max(0, after) / 1000, tz=timezone.utc).isoformat())
        have = set(tbl.scan(row_filter=flt, selected_fields=(key_col,)).to_arrow().column(key_col).to_pylist())
        return [r for r in rows if r[key_col] not in have]

    @staticmethod
    def _watermark(tbl, spec: Spec) -> Optional[str]:
        """Where the last append got to: carried on every snapshot Cairn writes; for a table without one (its
        snapshots expired), read from its rows."""
        snap = tbl.current_snapshot()
        if snap is None:
            return None
        wm = (snap.summary.additional_properties if snap.summary else {}).get("cairn.watermark")
        if wm not in (None, ""):
            return str(wm)
        col = spec.order if spec.order == spec.key else spec.time
        values = [v for v in tbl.scan(selected_fields=(col,)).to_arrow().column(col).to_pylist() if v is not None]
        if not values:
            return None
        top = max(values)
        return str(int(top.timestamp() * 1000) if isinstance(top, datetime) else int(top))

    @classmethod
    def _retain(cls, tbl, spec: Spec, since_ms: int) -> None:
        from pyiceberg.expressions import LessThan

        cutoff = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc)
        value = cutoff.date().isoformat() if any(c.name == spec.time and c.type == "date" for c in spec.columns) \
            else cutoff.isoformat()
        if tbl.current_snapshot() is not None:
            with warnings.catch_warnings():          # nothing old enough: not worth a warning
                warnings.filterwarnings("ignore", message="Delete operation did not match any records")
                wm = cls._watermark(tbl, spec) if spec.mode == "append" else None
                tbl.delete(LessThan(spec.time, value), snapshot_properties={"cairn.watermark": wm} if wm else {})

    @staticmethod
    def _expire(tbl) -> None:
        """Snapshots older than a day go (the change feed reaches back a day); files they alone held stay until the
        catalog's orphan-file clean-up."""
        try:
            older = datetime.fromtimestamp(time.time() - 86_400, tz=timezone.utc)
            current = tbl.current_snapshot()
            old = [s.snapshot_id for s in tbl.snapshots() if s.timestamp_ms < older.timestamp() * 1000
                   and (current is None or s.snapshot_id != current.snapshot_id)]
            if old:
                tbl.maintenance.expire_snapshots().by_ids(old).commit()
        except Exception as e:  # noqa: BLE001 - housekeeping: the next pass tries again
            logger.info("system table snapshots not expired: %s", e)

    # ---- settings ----------------------------------------------------------------------------------
    def describe(self) -> list[dict]:
        out = []
        try:
            cat = self._catalog()
        except Exception as e:  # noqa: BLE001 - the catalog is down: describe without it
            logger.warning("system tables catalog: %s", e)
            cat = None
        for spec in SPECS:
            st = self.status[spec.name]
            retention, written = self.default_retention, st.written_ms
            if cat is not None:
                try:
                    tbl = cat.load_table((spec.schema, spec.table))
                    retention = self.retention_days(tbl)
                    cur = tbl.current_snapshot()
                    written = cur.timestamp_ms if cur is not None else None
                except Exception:  # noqa: BLE001 - not created yet
                    pass
            out.append({"name": f"{self.trino_catalog}.{spec.name}", "schema": spec.schema, "table": spec.table,
                        "urn": self.urn(spec.name), "description": spec.description, "mode": spec.mode,
                        "row_filtered": spec.owner_filtered, "masked_columns": sorted(spec.sensitive),
                        "columns": [{"name": c.name, "type": c.type, "description": c.doc} for c in spec.columns],
                        "retention_days": retention, "synced_ms": st.synced_ms, "written_ms": written,
                        "error": st.error})
        return out

    def set_retention(self, name: str, days: int, actor: str) -> dict:
        spec = BY_NAME.get(name.lower())
        if spec is None:
            raise SystemTablesError("no such system table", 404)
        if not 1 <= int(days) <= 3650:
            raise SystemTablesError("retention is 1 to 3650 days", 422)
        with self._lock:
            cat = self._catalog()
            tbl = self._table(cat, spec)
            with tbl.transaction() as tx:
                tx.set_properties({"system-table.retention-days": str(int(days))})
            if spec.mode == "append":
                self._retain(cat.load_table((spec.schema, spec.table)), spec, int(time.time() * 1000) - int(days) * DAY_MS)
        if self.audit is not None:
            self.audit.record("governance", "system_tables.retention", actor, self.urn(spec.name), {"days": int(days)})
        return {"name": spec.name, "retention_days": int(days)}

    def catalog_mcps(self) -> list[dict]:
        """Each system table in the catalog: its description, columns and their descriptions."""
        from cairn.common.proposals import make_mcp

        kinds = {"string": "StringType", "long": "NumberType", "double": "NumberType", "timestamp": "TimeType",
                 "date": "DateType", "strings": "ArrayType"}
        out = []
        for spec in SPECS:
            urn = self.urn(spec.name)
            out.append(make_mcp(urn, "datasetProperties", {
                "name": spec.table, "qualifiedName": f"{self.trino_catalog}.{spec.name}", "description": spec.description,
                "customProperties": {"cairn.system-table": "true", "mode": spec.mode,
                                     "row_filtered": str(spec.owner_filtered).lower(),
                                     "masked_columns": ",".join(sorted(spec.sensitive))}}))
            out.append(make_mcp(urn, "schemaMetadata", {
                "schemaName": spec.name, "platform": "urn:li:dataPlatform:iceberg", "version": 0, "hash": "",
                "platformSchema": {"com.linkedin.schema.OtherSchema": {"rawSchema": ""}},
                "fields": [{"fieldPath": c.name, "nativeDataType": c.type, "nullable": True, "description": c.doc,
                            "type": {"type": {f"com.linkedin.schema.{kinds[c.type]}": {}}}} for c in spec.columns]}))
            out.append(make_mcp(urn, "subTypes", {"typeNames": ["System table"]}))
            out.append(make_mcp(urn, "status", {"removed": False}))
        return out

    def publish_catalog(self) -> int:
        if self.graph is None:
            return 0
        mcps = self.catalog_mcps()
        self.graph.emit(mcps)
        return len(SPECS)


def build_router(st: SystemTables, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/system-tables", tags=["system tables"])

    def operator(p) -> None:
        authorize(p, SYSTEM_PRIVILEGE, None)

    @router.get("")
    async def list_tables(p=Depends(current_principal)):
        """The system tables: names, descriptions, columns, governance, retention and when each was last synced."""
        return {"catalog": st.trino_catalog, "interval_seconds": st.interval_s,
                "freshness_seconds": st.interval_s + 60, "tables": await run_in_threadpool(st.describe)}

    @router.post("/sync")
    async def sync_now(p=Depends(current_principal)):
        """Sync every table now (operators)."""
        operator(p)
        return {"tables": await run_in_threadpool(st.sync)}

    @router.put("/{schema}/{table}/retention")
    async def retention(schema: str, table: str, days: int = Body(..., embed=True), p=Depends(current_principal)):
        """How many days a table keeps (operators); older rows are deleted now."""
        operator(p)
        try:
            return await run_in_threadpool(st.set_retention, f"{schema}.{table}", days, p.urn)
        except SystemTablesError as e:
            raise HTTPException(e.status, str(e))

    return router
