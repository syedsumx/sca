"""SQL profiling: column statistics for datasets whose ingestion does not profile them.

The result is written to the catalog as a ``datasetProfile`` (the Stats tab, volume and field
monitors with ``source: catalog``, and masking all read it there). One aggregate query per table:
row count, and per column the null and distinct counts, plus min, max, mean and standard deviation
for numeric columns. Sample values are off by default: they are real data.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import NoSuchTableError

from cairn.common.proposals import make_mcp, now_ms
from cairn.quality.collectors import CollectorError, dataset_name_from_urn

logger = logging.getLogger(__name__)

_NUMERIC = re.compile(r"INT|NUMERIC|DECIMAL|REAL|FLOAT|DOUBLE|MONEY|NUMBER", re.I)
_SAFE = re.compile(r"[A-Za-z0-9_$\- ]+")


def table_parts(dataset_urn: str, drop_database: bool = True) -> tuple[Optional[str], str]:
    """``db.schema.table`` → (``schema``, ``table``): the connection already selects the database."""
    parts = [p for p in dataset_name_from_urn(dataset_urn).split(".") if p]
    if drop_database and len(parts) == 3:
        parts = parts[1:]
    if not parts or len(parts) > 2 or any(not _SAFE.fullmatch(p) for p in parts):
        raise CollectorError(f"cannot profile {dataset_urn!r}: unsupported table name")
    return (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])


def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def profile_table(engine: Engine, dataset_urn: str, drop_database: bool = True, sample_values: int = 0,
                  max_columns: int = 200) -> dict:
    """A ``datasetProfile`` aspect for the table behind ``dataset_urn``."""
    schema, table = table_parts(dataset_urn, drop_database)
    q = engine.dialect.identifier_preparer
    try:
        cols = inspect(engine).get_columns(table, schema=schema)[:max_columns]
    except NoSuchTableError:
        cols = []
    if not cols:
        raise CollectorError(f"{dataset_urn}: table not found or has no columns")
    target = f"{q.quote(schema)}.{q.quote(table)}" if schema else q.quote(table)
    selects, plan = ["COUNT(*)"], []
    for c in cols:
        col = q.quote(c["name"])
        numeric = bool(_NUMERIC.search(str(c["type"])))
        exprs = [f"COUNT({col})", f"COUNT(DISTINCT {col})"]
        if numeric:
            exprs += [f"MIN({col})", f"MAX({col})", f"AVG({col})", f"AVG(1.0 * {col} * {col})"]
        plan.append((c["name"], numeric, len(selects), len(exprs)))
        selects += exprs
    with engine.connect() as conn:
        row = conn.execute(text(f"SELECT {', '.join(selects)} FROM {target}")).one()
        samples: dict[str, list] = {}
        if sample_values:
            for name, _numeric, _i, _n in plan:
                col = q.quote(name)
                vals = conn.execute(text(f"SELECT DISTINCT {col} FROM {target} WHERE {col} IS NOT NULL"))
                samples[name] = [str(v[0]) for v in vals.fetchmany(sample_values)]
    rows = int(row[0] or 0)
    fields = []
    for name, numeric, i, _n in plan:
        non_null, distinct = int(row[i] or 0), int(row[i + 1] or 0)
        f: dict[str, Any] = {"fieldPath": name, "nullCount": rows - non_null,
                             "nullProportion": (rows - non_null) / rows if rows else 0.0,
                             "uniqueCount": distinct, "uniqueProportion": distinct / non_null if non_null else 0.0}
        if numeric:
            lo, hi, mean, sq = (_num(row[i + 2]), _num(row[i + 3]), _num(row[i + 4]), _num(row[i + 5]))
            if lo is not None:
                f.update(min=str(lo), max=str(hi), mean=str(mean))
            if mean is not None and sq is not None:
                f["stdev"] = str(max(sq - mean * mean, 0.0) ** 0.5)
        if name in samples:
            f["sampleValues"] = samples[name]
        fields.append(f)
    return {"timestampMillis": now_ms(), "rowCount": rows, "columnCount": len(cols), "fieldProfiles": fields}


def profile_mcp(dataset_urn: str, profile: dict) -> dict:
    return make_mcp(dataset_urn, "datasetProfile", profile, system_properties={"cairnProfiler": "1"})
