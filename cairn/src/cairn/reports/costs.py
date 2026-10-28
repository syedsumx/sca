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
    platform: Literal["snowflake", "bigquery"]
    connection: str
    env: str = "PROD"
    credit_usd: float = 3.0
    storage_usd_per_tb_month: float = 23.0
    region: str = "region-us"
    usd_per_tib: float = 6.25
    storage_usd_per_gib_month: float = 0.02


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
       COUNT(DISTINCT j.job_id) AS reads
FROM `{region}`.INFORMATION_SCHEMA.JOBS j, UNNEST(j.referenced_tables) t
WHERE j.creation_time >= @start AND j.creation_time < @end AND j.job_type = 'QUERY' AND j.state = 'DONE'
GROUP BY name
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
        out.setdefault(u, {})["compute_usd"] = round(float(r["bytes_billed"] or 0) / 2**40 * source.usd_per_tib, 6)
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
