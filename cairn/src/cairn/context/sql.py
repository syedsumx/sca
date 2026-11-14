"""SQL fact extraction for Context Agents (``sqlglot``, the parser Cairn Core itself uses).

From one statement we extract:

* **metrics**: ``SUM(o.amount_cents) / 100 AS revenue`` → name ``revenue``, canonical expression
  ``sum(orders.amount_cents) / 100``, referenced columns;
* **dimensions**: renamed columns ``c.nm AS customer_name`` → business-name candidates;
* **joins**: equi-join keys ``o.customer_id = c.id`` → (orders.customer_id, customers.id).

Column qualifiers are resolved through the query's table aliases so facts reference real tables.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

PLATFORM_DIALECTS = {
    "snowflake": "snowflake", "bigquery": "bigquery", "redshift": "redshift", "postgres": "postgres",
    "mysql": "mysql", "databricks": "databricks", "hive": "hive", "trino": "trino", "presto": "presto",
    "mssql": "tsql", "oracle": "oracle", "clickhouse": "clickhouse", "duckdb": "duckdb", "spark": "spark",
    "athena": "athena", "teradata": "teradata", "dbt": None,
}


@dataclass(frozen=True)
class ColumnRef:
    table: Optional[str]
    column: str


@dataclass
class Metric:
    name: str
    expression: str
    canonical: str
    columns: list[ColumnRef]
    aggregates: list[str]


@dataclass
class Dimension:
    name: str
    column: ColumnRef


@dataclass
class Join:
    left: ColumnRef
    right: ColumnRef


@dataclass
class SqlFacts:
    tables: list[str] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    dimensions: list[Dimension] = field(default_factory=list)
    joins: list[Join] = field(default_factory=list)
    error: Optional[str] = None


def normalize_name(name: str) -> str:
    """``Total_Revenue`` / ``totalRevenue`` / ``total revenue`` → ``total revenue``."""
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _table_name(t) -> str:
    parts = [p for p in (t.catalog, t.db, t.name) if p]
    return ".".join(parts).lower()


def analyze_sql(sql: str, platform: Optional[str] = None) -> SqlFacts:
    import sqlglot
    from sqlglot import exp

    dialect = PLATFORM_DIALECTS.get((platform or "").lower())
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect, error_level=sqlglot.ErrorLevel.IGNORE) if s]
    except Exception as e:  # sqlglot raises various errors on exotic SQL
        return SqlFacts(error=f"{type(e).__name__}: {e}")
    facts = SqlFacts()
    for tree in statements:
        aliases: dict[str, str] = {}
        tables: list[str] = []
        for t in tree.find_all(exp.Table):
            if not t.name:
                continue
            fq = _table_name(t)
            tables.append(fq)
            aliases[(t.alias_or_name or t.name).lower()] = fq
            aliases[t.name.lower()] = fq
        # CTE names are not physical tables.
        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        physical = [t for t in dict.fromkeys(tables) if t.split(".")[-1] not in cte_names]
        facts.tables.extend(t for t in physical if t not in facts.tables)

        # Called within this iteration only; defaults bind this statement's aliases and tables.
        def resolve(col, aliases=aliases, physical=physical) -> ColumnRef:
            qual = (col.table or "").lower()
            if qual and qual in aliases:
                return ColumnRef(aliases[qual], col.name.lower())
            if not qual and len(physical) == 1:
                return ColumnRef(physical[0], col.name.lower())
            return ColumnRef(qual or None, col.name.lower())

        for select in tree.find_all(exp.Select):
            for proj in select.expressions:
                if not isinstance(proj, exp.Alias):
                    continue
                name = proj.alias
                inner = proj.this
                if isinstance(inner, exp.Column):
                    ref = resolve(inner)
                    if normalize_name(name) != normalize_name(ref.column):
                        facts.dimensions.append(Dimension(name, ref))
                    continue
                aggs = sorted({type(a).__name__.lower() for a in inner.find_all(exp.AggFunc)})
                if not aggs:
                    continue
                cols = [resolve(c) for c in inner.find_all(exp.Column)]
                canon = inner.copy()
                for c in canon.find_all(exp.Column):
                    ref = resolve(c)
                    c.set("table", exp.to_identifier(ref.table.split(".")[-1]) if ref.table else None)
                    c.set("db", None)
                    c.set("catalog", None)
                canonical = re.sub(r"\s+", " ", canon.sql(normalize=True)).strip().lower()
                facts.metrics.append(Metric(name, inner.sql(dialect=dialect), canonical, cols, aggs))
        for j in tree.find_all(exp.Join):
            on = j.args.get("on")
            if on is None:
                continue
            for eq in on.find_all(exp.EQ):
                left, right = eq.this, eq.expression
                if isinstance(left, exp.Column) and isinstance(right, exp.Column):
                    lref, rref = resolve(left), resolve(right)
                    if lref.table and rref.table and lref.table != rref.table:
                        facts.joins.append(Join(lref, rref))
    return facts


def same_definition(a: str, b: str) -> bool:
    """Canonical expressions compared ignoring table qualifiers and whitespace."""
    strip = lambda s: re.sub(r"\b[a-z0-9_]+\.(?=[a-z0-9_]+)", "", re.sub(r"\s+", "", s.lower()))  # noqa: E731
    return strip(a) == strip(b)
