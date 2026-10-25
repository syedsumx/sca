"""Metric collectors.

``DataHubSignalCollector`` needs **no warehouse access**: it reads signals ingestion
already emits (``operation``, ``datasetProfile``, ``schemaMetadata``).
``SqlCollector`` queries the warehouse directly through SQLAlchemy using a
*read-only* connection, with identifiers quoted by the target dialect.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from datahub_enterprise.common.gms import MetadataGraph
from datahub_enterprise.quality.models import Monitor


@dataclass
class Sample:
    value: Optional[float]
    payload: dict[str, Any] = field(default_factory=dict)


class CollectorError(RuntimeError):
    pass


class Collector(Protocol):
    def collect(self, monitor: Monitor, now_ms: int) -> Sample: ...


def dataset_name_from_urn(urn: str) -> str:
    # urn:li:dataset:(urn:li:dataPlatform:snowflake,db.schema.table,PROD)
    m = re.match(r"^urn:li:dataset:\(urn:li:dataPlatform:[^,]+,(.+),[A-Z_]+\)$", urn)
    if not m:
        raise CollectorError(f"cannot parse dataset urn {urn!r}")
    return m.group(1)


# --------------------------------------------------------------------------- DataHub signals
class DataHubSignalCollector:
    def __init__(self, graph: MetadataGraph):
        self.graph = graph

    def collect(self, monitor: Monitor, now_ms: int) -> Sample:
        t = monitor.type
        urn = monitor.dataset_urn
        if t == "freshness":
            ops = self._timeseries(urn, "operation", 200)
            updates = sorted({int(o.get("lastUpdatedTimestamp") or o["timestampMillis"]) for o in ops
                              if o.get("operationType", "UPDATE") not in ("DROP",)})
            if not updates:
                return Sample(None, {"update_times_ms": []})
            return Sample(float(updates[-1]), {"update_times_ms": updates})
        if t == "volume":
            profile = self._latest_profile(urn)
            if profile is None or profile.get("rowCount") is None:
                raise CollectorError("no datasetProfile.rowCount available; enable profiling or use source=sql")
            return Sample(float(profile["rowCount"]), {"profile_ts_ms": profile.get("timestampMillis")})
        if t == "field_metric":
            profile = self._latest_profile(urn)
            if profile is None:
                raise CollectorError("no datasetProfile available")
            metric = monitor.config["metric"]
            if metric == "row_count":
                return Sample(_f(profile.get("rowCount")))
            fp = next((f for f in profile.get("fieldProfiles", []) if f.get("fieldPath") == monitor.config["field"]), None)
            if fp is None:
                raise CollectorError(f"field {monitor.config['field']!r} not in datasetProfile")
            key = {"null_pct": "nullProportion", "distinct_pct": "uniqueProportion", "mean": "mean",
                   "min": "min", "max": "max", "stddev": "stdev"}.get(metric)
            if key is None:
                raise CollectorError(f"metric {metric!r} not available from datasetProfile; use source=sql")
            value = _f(fp.get(key))
            if value is not None and metric in ("null_pct", "distinct_pct"):
                value *= 100.0
            return Sample(value)
        if t == "schema":
            schema = self.graph.get_aspect(urn, "schemaMetadata")
            if not schema:
                raise CollectorError("no schemaMetadata for dataset")
            fields = {f["fieldPath"]: f.get("nativeDataType", "") for f in schema.get("fields", [])}
            return Sample(float(len(fields)), {"fields": fields})
        raise CollectorError(f"monitor type {t!r} is not supported with source=datahub")

    def _timeseries(self, urn: str, aspect: str, limit: int) -> list[dict]:
        getter = getattr(self.graph, "get_timeseries", None)
        if getter is None:
            raise CollectorError("graph client does not support timeseries reads")
        return getter(urn, aspect, limit)

    def _latest_profile(self, urn: str) -> Optional[dict]:
        profiles = self._timeseries(urn, "datasetProfile", 1)
        return profiles[0] if profiles else None


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- SQL
_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand_env(value: str) -> str:
    def repl(m: re.Match) -> str:
        if m.group(1) not in os.environ:
            raise CollectorError(f"environment variable {m.group(1)} is not set")
        return os.environ[m.group(1)]

    return _ENV_REF.sub(repl, value)


class ConnectionRegistry:
    """Named warehouse connections. URLs may reference secrets as ``${ENV_VAR}``.

    ``connections.yml``::

        connections:
          snowflake_prod:
            url: snowflake://${SF_USER}:${SF_PASSWORD}@acct/db?warehouse=WH&role=DHE_READONLY
    """

    def __init__(self, connections: Optional[dict[str, dict]] = None):
        self._defs = connections or {}
        self._engines: dict[str, Engine] = {}

    @classmethod
    def from_file(cls, path: Optional[str]) -> "ConnectionRegistry":
        if not path or not os.path.exists(path):
            return cls({})
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
        return cls(data.get("connections", {}))

    def engine(self, name: str) -> Engine:
        if name not in self._engines:
            d = self._defs.get(name)
            if d is None:
                raise CollectorError(f"unknown connection {name!r}")
            self._engines[name] = create_engine(_expand_env(d["url"]), pool_pre_ping=True, future=True)
        return self._engines[name]


class SqlCollector:
    def __init__(self, registry: ConnectionRegistry, sample_limit: int = 10_000, max_categories: int = 200):
        self.registry = registry
        self.sample_limit = sample_limit
        self.max_categories = max_categories

    def collect(self, monitor: Monitor, now_ms: int) -> Sample:
        c = monitor.config
        engine = self.registry.engine(c["connection"])
        q = engine.dialect.identifier_preparer
        table = self._table(monitor, q)
        with engine.connect() as conn:
            if monitor.type == "custom_sql":
                row = conn.execute(text(c["sql"])).first()
                return Sample(_f(row[0]) if row else None)
            if monitor.type == "volume":
                return Sample(_f(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()))
            if monitor.type == "freshness":
                col = q.quote(c["timestamp_column"])
                ts = conn.execute(text(f"SELECT MAX({col}) FROM {table}")).scalar()
                ts_ms = _to_epoch_ms(ts)
                return Sample(float(ts_ms) if ts_ms else None, {"update_times_ms": [ts_ms] if ts_ms else []})
            if monitor.type == "field_metric":
                return Sample(self._field_metric(conn, table, q.quote(c["field"]), c["metric"]))
            if monitor.type == "distribution":
                col = q.quote(c["field"])
                if c.get("kind", "categorical") == "categorical":
                    rows = conn.execute(text(
                        f"SELECT {col}, COUNT(*) FROM {table} GROUP BY {col} ORDER BY COUNT(*) DESC"
                    )).fetchmany(self.max_categories)
                    hist = {str(k): float(v) for k, v in rows}
                    return Sample(float(sum(hist.values())), {"histogram": hist})
                rows = conn.execute(text(
                    f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL"
                )).fetchmany(self.sample_limit)
                samples = [float(r[0]) for r in rows]
                return Sample(float(len(samples)), {"samples": samples})
        raise CollectorError(f"monitor type {monitor.type!r} is not supported with source=sql")

    @staticmethod
    def _field_metric(conn, table: str, col: str, metric: str) -> Optional[float]:
        exprs = {
            "null_pct": f"100.0 * SUM(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0)",
            "distinct_pct": f"100.0 * COUNT(DISTINCT {col}) / NULLIF(COUNT({col}), 0)",
            "mean": f"AVG({col})",
            "min": f"MIN({col})",
            "max": f"MAX({col})",
            "stddev": f"AVG({col} * {col}) - AVG({col}) * AVG({col})",
            "avg_length": f"AVG(LENGTH({col}))",
            "row_count": "COUNT(*)",
        }
        value = _f(conn.execute(text(f"SELECT {exprs[metric]} FROM {table}")).scalar())
        if metric == "stddev" and value is not None:
            value = max(value, 0.0) ** 0.5  # population stddev, portable across dialects
        return value

    @staticmethod
    def _table(monitor: Monitor, quoter) -> str:
        name = monitor.config.get("table") or dataset_name_from_urn(monitor.dataset_urn)
        parts = [p for p in name.split(".") if p]
        if not parts or any(not re.fullmatch(r"[A-Za-z0-9_$\- ]+", p) for p in parts):
            raise CollectorError(f"unsafe table identifier {name!r}")
        return ".".join(quoter.quote(p) for p in parts)


def _to_epoch_ms(value: Any) -> Optional[int]:
    from datetime import date, datetime, timezone

    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value if value > 1e11 else value * 1000)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)
    if isinstance(value, date):
        return int(datetime(value.year, value.month, value.day, tzinfo=timezone.utc).timestamp() * 1000)
    if isinstance(value, str):
        return _to_epoch_ms(datetime.fromisoformat(value.replace("Z", "+00:00")))
    raise CollectorError(f"unsupported timestamp value {value!r}")
