"""Metric collectors.

``CoreSignalCollector`` needs **no warehouse access**: it reads signals ingestion
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

from cairn.common.core import MetadataGraph
from cairn.quality.models import Monitor


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


# --------------------------------------------------------------------------- Cairn Core signals
class CoreSignalCollector:
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
        raise CollectorError(f"monitor type {t!r} is not supported with source=catalog")

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
            url: snowflake://${SF_USER}:${SF_PASSWORD}@acct/db?warehouse=WH&role=CAIRN_READONLY
    """

    def __init__(self, connections: Optional[dict[str, dict]] = None, path: Optional[str] = None):
        self._defs = connections or {}
        self._engines: dict[str, Engine] = {}
        self.trino_tokens = None        # cairn.dataaccess.trino_tokens.TrinoTokens: Cairn's own sign-in to Trino
        self._path = path
        self._stamp: Optional[tuple] = None

    @classmethod
    def from_file(cls, path: Optional[str]) -> "ConnectionRegistry":
        """A YAML file, or a directory whose ``*.yml``/``*.yaml`` files are merged (``*.example.*``
        skipped). Files are re-read when they change, so connections can be added without a restart."""
        reg = cls({}, path)
        reg._reload()
        return reg

    def _files(self) -> list[str]:
        p = self._path
        if not p or not os.path.exists(p):
            return []
        if os.path.isfile(p):
            return [p]
        return sorted(os.path.join(p, f) for f in os.listdir(p)
                      if f.endswith((".yml", ".yaml")) and ".example." not in f)

    def _reload(self) -> None:
        if self._path is None:
            return
        files = self._files()
        stamp = tuple((f, os.path.getmtime(f)) for f in files)
        if stamp == self._stamp:
            return
        defs: dict[str, dict] = {}
        for f in files:
            with open(f) as fh:
                defs.update((yaml.safe_load(fh) or {}).get("connections") or {})
        for name in [n for n in self._engines if self._defs.get(n) != defs.get(n)]:
            self._engines.pop(name).dispose()
        self._defs, self._stamp = defs, stamp

    def definitions(self) -> dict[str, dict]:
        self._reload()
        return dict(self._defs)

    def engine(self, name: str) -> Engine:
        self._reload()
        if name not in self._engines:
            d = self._defs.get(name)
            if d is None:
                raise CollectorError(f"unknown connection {name!r}")
            url = _expand_env(d["url"])
            self._engines[name] = (self._trino(name, d, url) if url.startswith("trino://")
                                   else create_engine(url, pool_pre_ping=True, future=True))
        return self._engines[name]

    def _trino(self, name: str, d: dict, raw: str) -> Engine:
        """A Trino connection for Cairn's own reads (monitors, profiling): over the connection's TLS with its own CA,
        signed in with the URL's password or, on a governed (``run_as_user``) connection, as its ``service_user``
        (default ``cairn-quality``) with a short Cairn token made for each connection, so Trino asks Cairn's policies
        about that user like anyone else."""
        from urllib.parse import parse_qs, unquote, urlsplit

        from sqlalchemy.pool import NullPool

        try:
            import trino
        except ImportError:
            raise CollectorError(f"connection {name!r} needs the trino client (pip install 'cairn-platform[trino]')")
        u = urlsplit(raw)
        opts = {k: v[-1] for k, v in parse_qs(u.query).items()}
        scheme = opts.get("protocol", "http")
        if scheme not in ("http", "https"):
            raise CollectorError(f"connection {name!r}: protocol must be http or https")
        verify: Any = opts.get("verify", "true")
        verify = False if verify == "false" else (True if verify == "true" else verify)
        path = [p for p in u.path.split("/") if p]
        service = str(d.get("service_user") or "urn:li:corpuser:cairn-quality")
        if not service.startswith("urn:li:corpuser:"):
            service = f"urn:li:corpuser:{service}"

        def connect():
            auth, headers, user = None, {}, unquote(u.username or "") or service.removeprefix("urn:li:corpuser:")
            if u.password:
                auth = trino.auth.BasicAuthentication(unquote(u.username or ""), unquote(u.password))
            elif d.get("run_as_user"):
                if self.trino_tokens is None:            # fail closed: a governed connection is never used unsigned
                    raise CollectorError(f"connection {name!r} needs Cairn's Trino sign-in (CAIRN_TRINO_ACCESS_KEY)")
                user = service.removeprefix("urn:li:corpuser:")
                headers["Authorization"] = "Bearer " + self.trino_tokens.mint(service, 900)["token"]
            session = None
            if isinstance(verify, str):                  # the connection's own CA, not one from the environment
                import requests

                session = requests.Session()
                session.trust_env = False
                session.verify = verify
            return trino.dbapi.connect(host=u.hostname, port=u.port or (443 if scheme == "https" else 8080), user=user,
                                       catalog=path[0] if path else None, schema=path[1] if len(path) > 1 else None,
                                       http_scheme=scheme, auth=auth, http_headers=headers or None, verify=verify,
                                       http_session=session, source="cairn-quality")

        return create_engine("trino://", creator=connect, poolclass=NullPool, future=True)


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
            if monitor.type == "volume" and c.get("partition_column") and c.get("partition_source") == "metadata":
                return self._metadata_partitions(conn, self._table(monitor, q, "$partitions"),
                                                 q.quote(c["partition_column"]), c, now_ms)
            if monitor.type == "volume" and c.get("partition_column"):
                return self._partitions(conn, engine.dialect.name, table, q.quote(c["partition_column"]), c, now_ms)
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
    def _partitions(conn, dialect: str, table: str, col: str, c: dict, now_ms: int) -> Sample:
        """Rows per day (or hour) over the monitor's window, read in one grouped query bounded by time; the value is
        the newest complete partition's rows."""
        from datetime import datetime, timezone

        from cairn.quality.detectors import GRAIN_MS

        grain = c.get("grain", "day")
        g = GRAIN_MS[grain]
        span = int(c.get("partitions", 28)) + int(c.get("check", 3)) + int(c.get("lag", 1))
        since = datetime.fromtimestamp(((now_ms // g) - span) * g / 1000, tz=timezone.utc)
        if c.get("truncate", True):
            key = {"sqlite": {"day": f"date({col})", "hour": f"strftime('%Y-%m-%d %H:00:00', {col})"},
                   "mysql": {"day": f"DATE({col})", "hour": f"DATE_FORMAT({col}, '%Y-%m-%d %H:00:00')"},
                   }.get(dialect, {"day": f"date_trunc('day', {col})", "hour": f"date_trunc('hour', {col})"})[grain]
            bound = f"'{since:%Y-%m-%d %H:%M:%S}'" if dialect in ("sqlite", "mysql") else f"TIMESTAMP '{since:%Y-%m-%d %H:%M:%S}'"
        else:                       # already a partition value: a date, or text like 2026-11-05 (day) / 2026-11-05 13 (hour)
            key = col
            bound = f"'{since:%Y-%m-%d}'" if grain == "day" else f"'{since:%Y-%m-%d %H}'"
        rows = conn.execute(text(f"SELECT {key} AS p, COUNT(*) AS n FROM {table} WHERE {col} >= {bound} GROUP BY {key}")).all()
        counts: dict[int, float] = {}
        for p, n in rows:
            if p is None:
                continue
            if isinstance(p, str) and grain == "hour" and len(p) == 13:      # 2026-11-05 13
                p = p + ":00:00"
            ms = _to_epoch_ms(p)
            if ms is not None:
                start = (ms // g) * g
                counts[start] = counts.get(start, 0.0) + float(n)
        newest = counts.get(((now_ms // g) - int(c.get("lag", 1))) * g, 0.0)
        return Sample(newest, {"partitions": {str(k): v for k, v in counts.items()}})

    @staticmethod
    def _metadata_partitions(conn, meta: str, field: str, c: dict, now_ms: int) -> Sample:
        """Rows per partition from the table format's own partition metadata (Trino's ``"<table>$partitions"`` for
        Iceberg, and for Delta Lake where Trino keeps row counts), not from a scan: each data file records how many
        rows it holds, so this reads metadata only, however large the table. ``partition_column`` is the partition
        field (``ts_day`` for ``day(ts)``, ``ts_hour`` for ``hour(ts)``, or a date column partitioned as it is).
        The value is the newest complete partition's rows."""
        from cairn.quality.detectors import GRAIN_MS

        grain = c.get("grain", "day")
        g = GRAIN_MS[grain]
        span = int(c.get("partitions", 28)) + int(c.get("check", 3)) + int(c.get("lag", 1))
        since = ((now_ms // g) - span) * g
        try:
            rows = conn.execute(text(f"SELECT partition.{field} AS p, record_count AS n FROM {meta}")).all()
        except Exception as e:  # noqa: BLE001 - the table has no such partition field, or no row counts
            raise CollectorError(f"the table's partition metadata has no row counts for {field}: {e}"[:500]) from e
        counts: dict[int, float] = {}
        for p, n in rows:
            if p is None or n is None:
                continue
            if isinstance(p, int) and not isinstance(p, bool):     # a transform's ordinal: days or hours since 1970
                ms = p * g
            elif isinstance(p, str) and grain == "hour" and len(p) == 13:
                ms = _to_epoch_ms(p + ":00:00")
            else:
                ms = _to_epoch_ms(p)
            if ms is None or ms < since:
                continue
            start = (ms // g) * g
            counts[start] = counts.get(start, 0.0) + float(n)
        newest = counts.get(((now_ms // g) - int(c.get("lag", 1))) * g, 0.0)
        return Sample(newest, {"partitions": {str(k): v for k, v in counts.items()}, "source": "metadata"})

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
    def _table(monitor: Monitor, quoter, suffix: str = "") -> str:
        """The monitored table, quoted; ``suffix`` names one of its metadata tables (``$partitions``)."""
        name = monitor.config.get("table") or dataset_name_from_urn(monitor.dataset_urn)
        parts = [p for p in name.split(".") if p]
        if not parts or any(not re.fullmatch(r"[A-Za-z0-9_$\- ]+", p) for p in parts):
            raise CollectorError(f"unsafe table identifier {name!r}")
        if suffix:                                   # always quoted: "orders$partitions"
            return ".".join([*(quoter.quote(p) for p in parts[:-1]), quoter.quote_identifier(parts[-1] + suffix)])
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
