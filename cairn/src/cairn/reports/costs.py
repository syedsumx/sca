"""Cost and usage per table: what each one costs to store and to query, and which ones nobody reads.

Daily figures per dataset live in ``cairn_asset_costs``. They come from warehouse collectors
(Snowflake ``ACCOUNT_USAGE``, BigQuery ``INFORMATION_SCHEMA``) run through named connections, or
from ``POST /reports/costs`` for anything else. Compute is attributed to tables by splitting each
query's cost evenly across the tables it read.

Collectors are configured in ``CAIRN_COST_SOURCES`` (YAML)::

    sources:
      - {platform: snowflake, connection: snowflake_usage, credit_usd: 3.0, storage_usd_per_tb_month: 23}
      - {platform: bigquery, connection: bq_usage, region: region-us, usd_per_tib: 6.25,
         storage_usd_per_gib_month: 0.02}
      - {platform: bigquery, connection: bq_usage, region: region-eu, pricing: editions,
         usd_per_slot_hour: 0.06}                  # BigQuery editions: slot-hours, not bytes
      - {platform: databricks, connection: dbx_system}   # system.billing + system.query + lineage
      - {platform: redshift, connection: rs_admin, daily_compute_usd: 260, storage_usd_per_gb_month: 0.024}
      - {platform: redshift, connection: rs_serverless, serverless: true, usd_per_rpu_hour: 0.375}

* **Databricks** compute is the day's list-price dollars from ``system.billing.usage``: a SQL
  warehouse's cost is split across its statements by duration, a job's across its runs; each statement
  or job splits its share evenly across the tables it read (``system.access.table_lineage``). There is
  no per-table storage figure in the system tables, so Databricks rows carry compute only.
* **Redshift** compute is the cluster's daily cost (``daily_compute_usd``), or Serverless RPU-seconds
  (``SYS_SERVERLESS_USAGE``) at ``usd_per_rpu_hour``, split across the day's queries by elapsed time and
  then across the tables each one scanned; storage is ``SVV_TABLE_INFO`` size at ``storage_usd_per_gb_month``.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Literal, Optional

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Column, Float, String, Table, and_, func, select
from sqlalchemy.engine import Engine

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

asset_costs = Table(
    "cairn_asset_costs", metadata,
    Column("urn", String(512), primary_key=True),
    Column("day", String(10), primary_key=True),
    Column("platform", String(64), nullable=False, index=True),
    Column("storage_bytes", BigInteger, nullable=True),
    Column("storage_usd", Float, nullable=False, default=0.0),
    Column("compute_usd", Float, nullable=False, default=0.0),
    Column("reads", BigInteger, nullable=False, default=0),
    Column("source", String(32), nullable=False),
)

DAY_MS = 86_400_000


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def dataset_urn(platform: str, name: str, env: str = "PROD") -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name.lower()},{env})"


class CostRow(BaseModel):
    urn: str = Field(pattern=r"^urn:li:dataset:\(.+\)$")
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    storage_bytes: Optional[int] = Field(default=None, ge=0)
    storage_usd: float = Field(default=0.0, ge=0)
    compute_usd: float = Field(default=0.0, ge=0)
    reads: int = Field(default=0, ge=0)


class CostSource(BaseModel):
    platform: Literal["snowflake", "bigquery", "databricks", "redshift"]
    connection: str
    env: str = "PROD"
    credit_usd: float = 3.0
    storage_usd_per_tb_month: float = 23.0
    region: str = "region-us"
    usd_per_tib: float = 6.25
    storage_usd_per_gib_month: float = 0.02
    pricing: Literal["on_demand", "editions"] = "on_demand"     # BigQuery
    usd_per_slot_hour: float = 0.06                              # BigQuery editions (Enterprise list price)
    daily_compute_usd: float = 0.0                               # Redshift provisioned: the cluster per day
    serverless: bool = False                                     # Redshift Serverless
    usd_per_rpu_hour: float = 0.375
    storage_usd_per_gb_month: float = 0.024                      # Redshift managed storage


def load_sources(path: Optional[str] = None) -> list[CostSource]:
    import yaml

    path = path or os.environ.get("CAIRN_COST_SOURCES", "")
    if not path or not os.path.exists(path):
        return []
    with open(path) as fh:
        return [CostSource.model_validate(s) for s in ((yaml.safe_load(fh) or {}).get("sources") or [])]


# --------------------------------------------------------------------------- collectors
SNOWFLAKE_STORAGE = """
SELECT table_catalog || '.' || table_schema || '.' || table_name AS name,
       active_bytes + time_travel_bytes + failsafe_bytes + retained_for_clone_bytes AS bytes
FROM snowflake.account_usage.table_storage_metrics
WHERE deleted = FALSE AND table_dropped IS NULL
"""

# Credits per query (QUERY_ATTRIBUTION_HISTORY), split across the base tables it read (ACCESS_HISTORY).
SNOWFLAKE_COMPUTE = """
WITH reads AS (
  SELECT ah.query_id, LOWER(obj.value:"objectName"::string) AS name
  FROM snowflake.account_usage.access_history ah,
       LATERAL FLATTEN(input => ah.base_objects_accessed) obj
  WHERE ah.query_start_time >= :start AND ah.query_start_time < :end
    AND obj.value:"objectDomain"::string IN ('Table', 'View', 'Materialized view')
), per_query AS (
  SELECT r.query_id, r.name, COALESCE(q.credits_attributed_compute, 0) AS credits,
         COUNT(*) OVER (PARTITION BY r.query_id) AS tables_read
  FROM reads r LEFT JOIN snowflake.account_usage.query_attribution_history q ON q.query_id = r.query_id
)
SELECT name, SUM(credits / tables_read) AS credits, COUNT(DISTINCT query_id) AS reads
FROM per_query GROUP BY name
"""

BIGQUERY_STORAGE = """
SELECT CONCAT(project_id, '.', table_schema, '.', table_name) AS name, total_logical_bytes AS bytes
FROM `{region}`.INFORMATION_SCHEMA.TABLE_STORAGE WHERE deleted = FALSE
"""

BIGQUERY_COMPUTE = """
SELECT CONCAT(t.project_id, '.', t.dataset_id, '.', t.table_id) AS name,
       SUM(j.total_bytes_billed / ARRAY_LENGTH(j.referenced_tables)) AS bytes_billed,
       SUM(j.total_slot_ms / ARRAY_LENGTH(j.referenced_tables)) AS slot_ms,
       COUNT(DISTINCT j.job_id) AS reads
FROM `{region}`.INFORMATION_SCHEMA.JOBS j, UNNEST(j.referenced_tables) t
WHERE j.creation_time >= @start AND j.creation_time < @end AND j.job_type = 'QUERY' AND j.state = 'DONE'
GROUP BY name
"""


# Databricks: list-price dollars per SQL warehouse and per job for the day, split by statement duration
# (warehouses) or evenly per run (jobs), then evenly across the tables each statement or job read.
DATABRICKS_COMPUTE = """
WITH usage AS (
  SELECT u.usage_metadata.warehouse_id AS warehouse_id, u.usage_metadata.job_id AS job_id,
         SUM(u.usage_quantity * p.pricing.default) AS usd
  FROM system.billing.usage u
  JOIN system.billing.list_prices p ON u.sku_name = p.sku_name AND u.cloud = p.cloud
       AND u.usage_start_time >= p.price_start_time AND (p.price_end_time IS NULL OR u.usage_end_time <= p.price_end_time)
  WHERE u.usage_date = :day AND (u.usage_metadata.warehouse_id IS NOT NULL OR u.usage_metadata.job_id IS NOT NULL)
  GROUP BY 1, 2
), stmts AS (
  SELECT q.statement_id, q.compute.warehouse_id AS warehouse_id, q.total_duration_ms AS ms,
         SUM(q.total_duration_ms) OVER (PARTITION BY q.compute.warehouse_id) AS warehouse_ms
  FROM system.query.history q
  WHERE q.start_time >= :start AND q.start_time < :end AND q.compute.warehouse_id IS NOT NULL
), reads AS (
  SELECT DISTINCT l.statement_id, l.entity_type, l.entity_id, LOWER(l.source_table_full_name) AS name
  FROM system.access.table_lineage l
  WHERE l.event_date = :day AND l.source_table_full_name IS NOT NULL
), per_read AS (
  SELECT r.name, r.statement_id AS unit,
         w.usd * s.ms / NULLIF(s.warehouse_ms, 0) / COUNT(*) OVER (PARTITION BY r.statement_id) AS usd
  FROM reads r JOIN stmts s ON s.statement_id = r.statement_id
  JOIN usage w ON w.warehouse_id = s.warehouse_id
  UNION ALL
  SELECT r.name, r.entity_id AS unit, j.usd / COUNT(*) OVER (PARTITION BY r.entity_id) AS usd
  FROM (SELECT DISTINCT entity_id, name FROM reads WHERE entity_type = 'JOB') r
  JOIN usage j ON j.job_id = r.entity_id
)
SELECT name, SUM(usd) AS usd, COUNT(DISTINCT unit) AS reads FROM per_read GROUP BY name
"""

REDSHIFT_STORAGE = """
SELECT "database" || '.' || "schema" || '.' || "table" AS name, size AS mb FROM svv_table_info
"""

# Each query's share of the day (elapsed time), split evenly across the tables it scanned.
REDSHIFT_COMPUTE = """
WITH q AS (
  SELECT query_id, elapsed_time FROM sys_query_history
  WHERE start_time >= :start AND start_time < :end AND status = 'success' AND query_type = 'SELECT'
), scans AS (
  SELECT DISTINCT d.query_id, d.table_id FROM sys_query_detail d JOIN q ON q.query_id = d.query_id
  WHERE d.step_name = 'scan' AND d.table_id > 0
), shares AS (
  SELECT s.query_id, s.table_id,
         q.elapsed_time::float / NULLIF(SUM(q.elapsed_time) OVER (), 0)
           / COUNT(*) OVER (PARTITION BY s.query_id) AS share
  FROM scans s JOIN q ON q.query_id = s.query_id
)
SELECT t."database" || '.' || t."schema" || '.' || t."table" AS name, SUM(sh.share) AS share,
       COUNT(DISTINCT sh.query_id) AS reads
FROM shares sh JOIN svv_table_info t ON t.table_id = sh.table_id
GROUP BY 1
"""

REDSHIFT_SERVERLESS_USAGE = """
SELECT COALESCE(SUM(charged_seconds), 0) AS rpu_seconds FROM sys_serverless_usage
WHERE start_time >= :start AND start_time < :end
"""


def snowflake_rows(source: CostSource, day: str, storage: Iterable[dict], compute: Iterable[dict]) -> list[CostRow]:
    """Daily cost rows from the two query results: storage priced per TB-month (pro rata per
    day), compute from attributed credits."""
    out: dict[str, dict[str, Any]] = {}
    per_day = source.storage_usd_per_tb_month / 30 / 1e12
    for r in storage:
        u = dataset_urn("snowflake", r["name"], source.env)
        out.setdefault(u, {})["storage_bytes"] = int(r["bytes"] or 0)
        out[u]["storage_usd"] = round(int(r["bytes"] or 0) * per_day, 6)
    for r in compute:
        u = dataset_urn("snowflake", r["name"], source.env)
        out.setdefault(u, {})["compute_usd"] = round(float(r["credits"] or 0) * source.credit_usd, 6)
        out[u]["reads"] = int(r["reads"] or 0)
    return [CostRow(urn=u, day=day, **v) for u, v in out.items()]


def bigquery_rows(source: CostSource, day: str, storage: Iterable[dict], compute: Iterable[dict]) -> list[CostRow]:
    out: dict[str, dict[str, Any]] = {}
    per_day = source.storage_usd_per_gib_month / 30 / 2**30
    for r in storage:
        u = dataset_urn("bigquery", r["name"], source.env)
        out.setdefault(u, {})["storage_bytes"] = int(r["bytes"] or 0)
        out[u]["storage_usd"] = round(int(r["bytes"] or 0) * per_day, 6)
    for r in compute:
        u = dataset_urn("bigquery", r["name"], source.env)
        if source.pricing == "editions":
            usd = float(r.get("slot_ms") or 0) / 3_600_000 * source.usd_per_slot_hour
        else:
            usd = float(r["bytes_billed"] or 0) / 2**40 * source.usd_per_tib
        out.setdefault(u, {})["compute_usd"] = round(usd, 6)
        out[u]["reads"] = int(r["reads"] or 0)
    return [CostRow(urn=u, day=day, **v) for u, v in out.items()]


def databricks_rows(source: CostSource, day: str, compute: Iterable[dict]) -> list[CostRow]:
    return [CostRow(urn=dataset_urn("databricks", r["name"], source.env), day=day,
                    compute_usd=round(float(r["usd"] or 0), 6), reads=int(r["reads"] or 0)) for r in compute]


def redshift_rows(source: CostSource, day: str, storage: Iterable[dict], compute: Iterable[dict],
                  rpu_seconds: float = 0.0) -> list[CostRow]:
    """Storage at ``storage_usd_per_gb_month`` pro rata; the day's compute (the cluster's daily cost, or
    Serverless RPU-seconds) split by each table's share of query time."""
    day_usd = rpu_seconds / 3600 * source.usd_per_rpu_hour if source.serverless else source.daily_compute_usd
    per_day = source.storage_usd_per_gb_month / 30 / 1024              # SVV_TABLE_INFO.size is in MB
    out: dict[str, dict[str, Any]] = {}
    for r in storage:
        u = dataset_urn("redshift", r["name"], source.env)
        mb = int(r["mb"] or 0)
        out.setdefault(u, {})["storage_bytes"] = mb * 2**20
        out[u]["storage_usd"] = round(mb * per_day, 6)
    for r in compute:
        u = dataset_urn("redshift", r["name"], source.env)
        out.setdefault(u, {})["compute_usd"] = round(float(r["share"] or 0) * day_usd, 6)
        out[u]["reads"] = int(r["reads"] or 0)
    return [CostRow(urn=u, day=day, **v) for u, v in out.items()]


def collect(source: CostSource, registry, day: str) -> list[CostRow]:
    """Run one source's queries for ``day`` (UTC)."""
    from sqlalchemy import text

    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    params = {"start": start, "end": start + timedelta(days=1)}
    with registry.engine(source.connection).connect() as conn:
        if source.platform == "snowflake":
            storage = [dict(r._mapping) for r in conn.execute(text(SNOWFLAKE_STORAGE))]
            compute = [dict(r._mapping) for r in conn.execute(text(SNOWFLAKE_COMPUTE), params)]
            return snowflake_rows(source, day, storage, compute)
        if source.platform == "databricks":
            compute = [dict(r._mapping) for r in conn.execute(text(DATABRICKS_COMPUTE), {**params, "day": day})]
            return databricks_rows(source, day, compute)
        if source.platform == "redshift":
            storage = [dict(r._mapping) for r in conn.execute(text(REDSHIFT_STORAGE))]
            compute = [dict(r._mapping) for r in conn.execute(text(REDSHIFT_COMPUTE), params)]
            rpu = float(conn.execute(text(REDSHIFT_SERVERLESS_USAGE), params).scalar() or 0) if source.serverless else 0.0
            return redshift_rows(source, day, storage, compute, rpu)
        storage = [dict(r._mapping) for r in conn.execute(text(BIGQUERY_STORAGE.format(region=source.region)))]
        compute = [dict(r._mapping) for r in conn.execute(text(BIGQUERY_COMPUTE.format(region=source.region)
                                                               .replace("@start", ":start").replace("@end", ":end")),
                                                          params)]
        return bigquery_rows(source, day, storage, compute)


# --------------------------------------------------------------------------- service
class CostService:
    def __init__(self, engine: Engine, graph, has_downstream: Callable[[str], bool],
                 notify: Optional[Callable[[str, str, str, dict], None]] = None,
                 sources: Callable[[], list[CostSource]] = load_sources, registry=None, audit=None):
        """``has_downstream(urn)``: does anything read from this dataset in lineage. ``notify(urn,
        actor, reason, figures)`` tells the owners and followers about a proposed deprecation."""
        self.engine = engine
        self.graph = graph
        self.has_downstream = has_downstream
        self.notify = notify
        self.sources = sources
        self.registry = registry
        self.audit = audit

    def ingest(self, rows: list[CostRow], source: str = "api") -> int:
        with self.engine.begin() as conn:
            for r in rows:
                conn.execute(asset_costs.delete().where(and_(asset_costs.c.urn == r.urn, asset_costs.c.day == r.day)))
                conn.execute(asset_costs.insert().values(
                    urn=r.urn, day=r.day, platform=r.urn.split("dataPlatform:", 1)[1].split(",", 1)[0],
                    storage_bytes=r.storage_bytes, storage_usd=r.storage_usd, compute_usd=r.compute_usd,
                    reads=r.reads, source=source))
        return len(rows)

    def collect_day(self, day: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for s in self.sources():
            try:
                out[s.connection] = self.ingest(collect(s, self.registry, day), s.platform)
            except Exception as e:  # noqa: BLE001 - one warehouse failing must not stop the others
                logger.warning("cost collection from %s failed: %s", s.connection, e)
                out[s.connection] = {"error": str(e)[:300]}
        return out

    def asset(self, urn: str, days: int = 90) -> dict:
        with self.engine.connect() as conn:
            rows = conn.execute(select(asset_costs).where(asset_costs.c.urn == urn)
                                .order_by(asset_costs.c.day.desc()).limit(days)).all()
        series = [{"day": r.day, "storage_bytes": r.storage_bytes, "storage_usd": r.storage_usd,
                   "compute_usd": r.compute_usd, "reads": r.reads} for r in reversed(rows)]
        last30 = series[-30:]
        return {"urn": urn, "series": series,
                "last_30d": {"storage_usd": round(sum(r["storage_usd"] for r in last30), 2),
                             "compute_usd": round(sum(r["compute_usd"] for r in last30), 2),
                             "reads": sum(r["reads"] for r in last30)}}

    def _totals(self, days: int, now_ms: int) -> list[dict]:
        since = _day(now_ms - days * DAY_MS)
        with self.engine.connect() as conn:
            rows = conn.execute(select(asset_costs.c.urn, asset_costs.c.platform,
                                       func.sum(asset_costs.c.storage_usd), func.sum(asset_costs.c.compute_usd),
                                       func.sum(asset_costs.c.reads), func.max(asset_costs.c.storage_bytes),
                                       func.max(asset_costs.c.day))
                                .where(asset_costs.c.day >= since).group_by(asset_costs.c.urn, asset_costs.c.platform)).all()
        return [{"urn": u, "platform": p, "storage_usd": round(s or 0, 2), "compute_usd": round(c or 0, 2),
                 "cost_usd": round((s or 0) + (c or 0), 2), "reads": int(n or 0), "storage_bytes": b, "last_day": d}
                for u, p, s, c, n, b, d in rows]

    def idle(self, days: int = 30, now_ms: Optional[int] = None, limit: int = 200) -> list[dict]:
        """No reads in ``days`` and nothing downstream in lineage, most expensive to keep first."""
        now_ms = now_ms or int(time.time() * 1000)
        out = []
        for t in sorted(self._totals(days, now_ms), key=lambda t: -t["storage_usd"]):
            if t["reads"] or self.has_downstream(t["urn"]):
                continue
            out.append({**t, "monthly_storage_usd": round(t["storage_usd"] * 30 / days, 2)})
            if len(out) >= limit:
                break
        return out

    def expensive(self, days: int = 30, now_ms: Optional[int] = None, min_cost_usd: float = 1.0,
                  limit: int = 200) -> list[dict]:
        """Highest cost per read: tables that cost a lot for how rarely they are used."""
        now_ms = now_ms or int(time.time() * 1000)
        rows = [{**t, "cost_per_read_usd": round(t["cost_usd"] / max(t["reads"], 1), 4)}
                for t in self._totals(days, now_ms) if t["cost_usd"] >= min_cost_usd]
        return sorted(rows, key=lambda t: -t["cost_per_read_usd"])[:limit]

    def propose_deprecation(self, urn: str, actor: str, reason: str = "") -> dict:
        figures = self.asset(urn)["last_30d"]
        if self.notify is not None:
            self.notify(urn, actor, reason, figures)
        if self.audit is not None:
            self.audit.record("governance", "deprecation.proposed", actor, urn, {"reason": reason, **figures})
        return {"urn": urn, "proposed": True, "last_30d": figures}
