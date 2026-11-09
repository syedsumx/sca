"""API/domain models for monitors and evaluations."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, model_validator

MonitorType = Literal["freshness", "volume", "field_metric", "distribution", "schema", "custom_sql"]
FieldMetric = Literal["null_pct", "distinct_pct", "mean", "min", "max", "stddev", "avg_length", "row_count"]


class Schedule(BaseModel):
    cron: Optional[str] = None
    interval_seconds: Optional[int] = Field(default=None, ge=60)

    @model_validator(mode="after")
    def _one_of(self) -> "Schedule":
        if bool(self.cron) == bool(self.interval_seconds):
            raise ValueError("exactly one of cron or interval_seconds is required")
        if self.cron:
            from croniter import croniter

            if not croniter.is_valid(self.cron):
                raise ValueError(f"invalid cron expression {self.cron!r}")
        return self


class MonitorSpec(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    dataset_urn: str = Field(pattern=r"^urn:li:dataset:\(.+\)$")
    type: MonitorType
    schedule: Schedule = Field(default_factory=lambda: Schedule(interval_seconds=3600))
    config: dict[str, Any] = Field(default_factory=dict)
    sensitivity: Literal["low", "medium", "high"] = "medium"
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "MEDIUM"
    enabled: bool = True
    incident_after_failures: int = Field(default=1, ge=1, le=100)
    notify: list[str] = Field(default_factory=list, description="webhook URLs (Slack/Teams/PagerDuty bridge)")

    @model_validator(mode="after")
    def _validate_config(self) -> "MonitorSpec":
        c = self.config
        source = c.get("source", "catalog" if self.type in ("freshness", "volume", "field_metric", "schema") else "sql")
        c["source"] = source
        if source not in ("catalog", "sql"):
            raise ValueError("config.source must be 'catalog' or 'sql'")
        if source == "sql" and not c.get("connection"):
            raise ValueError("config.connection is required when source='sql'")
        if self.type == "field_metric":
            if not c.get("field") or c.get("metric") not in FieldMetric.__args__:  # type: ignore[attr-defined]
                raise ValueError(f"field_metric requires config.field and config.metric in {FieldMetric.__args__}")  # type: ignore[attr-defined]
        if self.type == "distribution":
            if source != "sql" or not c.get("field"):
                raise ValueError("distribution monitors require source='sql' and config.field")
            c.setdefault("kind", "categorical")
        if self.type == "custom_sql":
            sql = (c.get("sql") or "").strip()
            if not sql:
                raise ValueError("custom_sql requires config.sql")
            validate_readonly_sql(sql)
        if c.get("model") not in (None, "learned"):
            raise ValueError("config.model must be 'learned' or omitted")
        if c.get("model") == "learned":
            if self.type not in ("volume", "field_metric", "custom_sql"):
                raise ValueError("learned thresholds apply to volume, field_metric and custom_sql monitors")
            if c.get("band") is not None and not 1 <= float(c["band"]) <= 10:
                raise ValueError("config.band must be between 1 and 10 standard deviations")
            if not 0 <= float(c.get("learn_days", 21)) <= 90:
                raise ValueError("config.learn_days must be between 0 and 90")
            if c.get("seasonality", "auto") not in ("auto", None, "hour_of_week", "day_of_week", "hour_of_day"):
                raise ValueError("config.seasonality must be auto, hour_of_week, day_of_week, hour_of_day or null")
        if c.get("partition_column") is not None:
            if self.type != "volume" or source != "sql":
                raise ValueError("config.partition_column applies to volume monitors with source='sql'")
            if not isinstance(c["partition_column"], str) or not c["partition_column"].strip():
                raise ValueError("config.partition_column is a column name")
            if c.get("model") == "learned":
                raise ValueError("a partitioned volume monitor learns each partition's usual size itself; omit config.model")
            if c.setdefault("grain", "day") not in ("day", "hour"):
                raise ValueError("config.grain must be 'day' or 'hour'")
            for k, lo, hi, default in (("partitions", 3, 400, 28), ("check", 1, 48, 3), ("lag", 0, 48, 1),
                                       ("min_history", 1, 400, 3)):
                v = c.setdefault(k, default)
                if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                    raise ValueError(f"config.{k} must be a whole number from {lo} to {hi}")
            if c.get("min_ratio") is not None and not 0 < float(c["min_ratio"]) <= 1:
                raise ValueError("config.min_ratio must be above 0 and at most 1")
            if c.get("min_rows_per_partition") is not None and float(c["min_rows_per_partition"]) < 0:
                raise ValueError("config.min_rows_per_partition must not be negative")
            c.setdefault("truncate", True)
            if c.setdefault("partition_source", "data") not in ("data", "metadata"):
                raise ValueError("config.partition_source must be 'data' (count rows by partition_column) or "
                                 "'metadata' (row counts of the table's partitions, Iceberg and Delta Lake on Trino)")
        if self.type == "freshness" and source == "sql" and not c.get("timestamp_column"):
            raise ValueError("SQL freshness requires config.timestamp_column")
        return self


_FORBIDDEN_SQL = (
    "insert", "update", "delete", "merge", "drop", "alter", "create", "truncate", "grant",
    "revoke", "call", "exec", "execute", "copy", "unload", "put", "remove", "attach", "pragma",
)


def validate_readonly_sql(sql: str) -> None:
    """Defence in depth for custom SQL: one SELECT/WITH statement, no DML/DDL keywords.

    The primary control is still the read-only database role configured for the
    connection; this check only rejects obviously unsafe statements early.
    """
    stripped = sql.strip().rstrip(";").strip()
    if ";" in stripped:
        raise ValueError("custom SQL must be a single statement")
    head = stripped.split(None, 1)[0].lower() if stripped else ""
    if head not in ("select", "with"):
        raise ValueError("custom SQL must start with SELECT or WITH")
    tokens = {t.strip("(),").lower() for t in stripped.replace("\n", " ").split()}
    bad = tokens.intersection(_FORBIDDEN_SQL)
    if bad:
        raise ValueError(f"custom SQL contains forbidden keywords: {sorted(bad)}")


class Monitor(MonitorSpec):
    id: str
    status: Literal["TRAINING", "ACTIVE", "PAUSED"] = "TRAINING"
    created_at: str
    updated_at: str
    created_by: Optional[str] = None
    next_run_at_ms: Optional[int] = None
    last_evaluation: Optional["Evaluation"] = None


class Evaluation(BaseModel):
    id: str
    monitor_id: str
    ts: str
    ts_ms: int
    run_id: str
    metric_value: Optional[float]
    expected_low: Optional[float]
    expected_high: Optional[float]
    status: Literal["PASS", "WARN", "FAIL", "TRAINING", "ERROR"]
    score: Optional[float]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    feedback: Optional[Literal["EXPECTED", "REAL", "NEW_NORMAL"]] = None
    emitted: bool = False


class FeedbackRequest(BaseModel):
    # EXPECTED: a legitimate value, kept in the baseline. REAL: an incident, kept out of it.
    # NEW_NORMAL: an intended step change; the baseline restarts from this point.
    feedback: Literal["EXPECTED", "REAL", "NEW_NORMAL"]


Monitor.model_rebuild()
