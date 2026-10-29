"""Looking at the data from the catalog: a table preview and a read-only SQL editor.

Both run through a named warehouse connection with a read-only role. A connection says which
catalog datasets it serves::

    connections:
      warehouse:
        url: postgresql+psycopg://${WH_USER}:${WH_PASSWORD}@warehouse:5432/analytics
        catalog: {platform: postgres, database: analytics}     # datasets analytics.<schema>.<table>

Every table a query reads must be in the catalog and the caller must be allowed to see its data
(``VIEW_DATASET_PROFILE``, the privilege that guards sample values). Results are masked with the
caller's masking plan, the same one the catalog applies to profiles, and every query is audited.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import sqlglot
from sqlglot import exp
from sqlalchemy import text

from cairn.coreproxy.masking import DatasetPlan, mask_scalar, mask_sql
from cairn.quality.models import validate_readonly_sql

logger = logging.getLogger(__name__)

DIALECTS = {"postgres": "postgres", "postgresql": "postgres", "snowflake": "snowflake", "bigquery": "bigquery",
            "mysql": "mysql", "redshift": "redshift", "databricks": "databricks", "sqlite": "sqlite",
            "trino": "trino", "duckdb": "duckdb"}
MAX_ROWS = 1000
PREVIEW_ROWS = 100
TIMEOUT_MS = 15_000


class DataAccessError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Source:
    connection: str
    platform: str
    database: Optional[str]
    dialect: str
    env: str = "PROD"

    def urn(self, schema: str, table: str, database: Optional[str] = None) -> str:
        name = ".".join(p for p in (database or self.database, schema, table) if p).lower()
        return f"urn:li:dataset:(urn:li:dataPlatform:{self.platform},{name},{self.env})"


def parse_dataset_urn(urn: str) -> tuple[str, list[str]]:
    import re

    m = re.match(r"^urn:li:dataset:\(urn:li:dataPlatform:([^,]+),(.+),[^,]+\)$", urn)
    if not m:
        raise DataAccessError("not a dataset urn")
    return m.group(1), m.group(2).split(".")


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    masked_columns: list[str] = field(default_factory=list)
    truncated: bool = False
    elapsed_ms: int = 0
    tables: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"columns": self.columns, "rows": self.rows, "masked_columns": self.masked_columns,
                "truncated": self.truncated, "elapsed_ms": self.elapsed_ms, "tables": self.tables}


def tables_in(sql: str, dialect: str) -> tuple[exp.Expression, list[exp.Table]]:
    """The parsed statement and every real table it reads (CTE names excluded)."""
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.ParseError as e:
        raise DataAccessError(f"could not parse the query: {str(e).splitlines()[0][:300]}")
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise DataAccessError("run one statement at a time")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise DataAccessError("only SELECT queries can run here")
    for bad in (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Merge, exp.Command):
        if tree.find(bad):
            raise DataAccessError("only SELECT queries can run here")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    tables = [t for t in tree.find_all(exp.Table) if t.name and t.name.lower() not in ctes]
    return tree, tables


def output_sources(tree: exp.Expression) -> list[Optional[set[str]]]:
    """Per output column of the outermost SELECT, the simple names of the columns it is computed
    from; ``None`` for a ``*`` (resolved against the result's column names instead)."""
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if select is None:
        return []
    out: list[Optional[set[str]]] = []
    for proj in select.expressions:
        if isinstance(proj, exp.Star) or (isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star)):
            out.append(None)
            continue
        out.append({c.name.lower() for c in proj.find_all(exp.Column)})
    return out


class DataAccessService:
    def __init__(self, registry, plan_for: Callable[[str, str], DatasetPlan],
                 can_read: Callable[[str, str], bool], audit=None, max_rows: int = MAX_ROWS,
                 timeout_ms: int = TIMEOUT_MS, known: Callable[[str], bool] = lambda urn: True,
                 on_read: Optional[Callable[[str, list[str]], None]] = None):
        """``plan_for(actor, urn)``: the caller's masking plan. ``can_read(actor, urn)``: may they see
        data. ``known(urn)``: is the dataset in the catalog (uncatalogued tables are never served).
        ``on_read(actor, urns)``: told after every served read (AI usage lineage)."""
        self.known = known
        self.on_read = on_read
        self.registry = registry
        self.plan_for = plan_for
        self.can_read = can_read
        self.audit = audit
        self.max_rows = max_rows
        self.timeout_ms = timeout_ms

    # ---- where datasets live ----------------------------------------------------------
    def sources(self) -> list[Source]:
        out = []
        for name, d in (self.registry.definitions() if self.registry else {}).items():
            cat = d.get("catalog") or {}
            if not cat.get("platform"):
                continue
            out.append(Source(name, cat["platform"], cat.get("database"),
                              DIALECTS.get(cat.get("dialect") or cat["platform"], cat["platform"]),
                              cat.get("env", "PROD")))
        return out

    def source_for(self, urn: str) -> tuple[Source, list[str]]:
        platform, parts = parse_dataset_urn(urn)
        for s in self.sources():
            if s.platform != platform:
                continue
            if s.database and len(parts) == 3 and parts[0].lower() != s.database.lower():
                continue
            return s, parts
        raise DataAccessError("no connection serves this dataset; previews need a warehouse connection", 404)

    def _source(self, connection: Optional[str], urn: Optional[str]) -> Source:
        if urn:
            return self.source_for(urn)[0]
        for s in self.sources():
            if s.connection == connection:
                return s
        raise DataAccessError(f"unknown connection {connection!r}", 404)

    # ---- running ----------------------------------------------------------------------
    def _execute(self, source: Source, sql: str, limit: int) -> tuple[list[str], list[list[Any]], bool]:
        engine = self.registry.engine(source.connection)
        with engine.connect() as conn:
            with conn.begin():
                if engine.dialect.name == "postgresql":
                    conn.execute(text(f"SET LOCAL statement_timeout = {int(self.timeout_ms)}"))
                    conn.execute(text("SET LOCAL transaction_read_only = on"))
                result = conn.execute(text(sql))
                columns = list(result.keys())
                rows = [list(r) for r in result.fetchmany(limit + 1)]
        return columns, rows[:limit], len(rows) > limit

    def _mask(self, actor: str, urns: list[str], columns: list[str], rows: list[list[Any]],
              sources: list[Optional[set[str]]]) -> list[str]:
        plans = [self.plan_for(actor, u) for u in urns]
        active = [p for p in plans if p.active]
        if not active:
            return []
        strategy = max((p.strategy for p in active), key=lambda s: {"partial": 0, "hash": 1, "redact": 2, "null": 3}[s])
        masked = []
        for i, col in enumerate(columns):
            names = sources[i] if i < len(sources) and sources[i] is not None else {col.lower()}
            if not names:      # a literal or an expression over no column
                continue
            if any(p.all_fields or any(p.masks_field(n) for n in names) for p in active):
                masked.append(col)
                for r in rows:
                    r[i] = mask_scalar(r[i], strategy)
        return masked

    def _jsonable(self, rows: list[list[Any]]) -> list[list[Any]]:
        def v(x):
            if x is None or isinstance(x, (bool, int, float, str)):
                return x
            return str(x)
        return [[v(x) for x in r] for r in rows]

    def preview(self, actor: str, urn: str, limit: int = PREVIEW_ROWS) -> dict:
        if not self.known(urn) or not self.can_read(actor, urn):
            raise DataAccessError("you may not see this dataset's data", 403)
        source, parts = self.source_for(urn)
        schema, table = parts[-2], parts[-1]
        database = parts[0] if len(parts) == 3 and not source.database else None
        sql = exp.select("*").from_(exp.table_(table, db=schema, catalog=database, quoted=True)) \
            .limit(min(limit, self.max_rows) + 1).sql(dialect=source.dialect)    # one more: was it cut?
        started = time.monotonic()
        columns, rows, truncated = self._execute(source, sql, min(limit, self.max_rows))
        masked = self._mask(actor, [urn], columns, rows, [None] * len(columns))
        out = QueryResult(columns, self._jsonable(rows), masked, truncated, int((time.monotonic() - started) * 1000),
                          [urn])
        self._audit(actor, "data.previewed", urn, {"rows": len(rows), "masked": masked})
        self._read(actor, [urn])
        return out.as_dict()

    def query(self, actor: str, sql: str, connection: Optional[str] = None, urn: Optional[str] = None,
              limit: int = 200) -> dict:
        source = self._source(connection, urn)
        try:
            validate_readonly_sql(sql)
        except ValueError as e:
            raise DataAccessError(str(e))
        tree, tables = tables_in(sql, source.dialect)
        if not tables:
            raise DataAccessError("the query reads no table")
        urns = []
        for t in tables:
            u = source.urn(t.db or "", t.name, t.catalog or None)
            if not t.db:
                raise DataAccessError(f"qualify {t.name} with its schema (schema.table)")
            if not self.known(u):
                raise DataAccessError(f"{t.db}.{t.name} is not in the catalog", 403)
            if not self.can_read(actor, u):
                self._audit(actor, "data.query_denied", source.connection, {"sql": mask_sql(sql), "table": u})
                raise DataAccessError(f"you may not query {t.db}.{t.name}", 403)
            urns.append(u)
        limit = max(1, min(limit, self.max_rows))
        wrapped = f"SELECT * FROM ({tree.sql(dialect=source.dialect)}) AS cairn_q LIMIT {limit + 1}"
        started = time.monotonic()
        try:
            columns, rows, truncated = self._execute(source, wrapped, limit)
        except DataAccessError:
            raise
        except Exception as e:  # noqa: BLE001 - warehouse errors go back to the editor
            raise DataAccessError(f"the warehouse refused the query: {str(e).splitlines()[0][:300]}")
        masked = self._mask(actor, sorted(set(urns)), columns, rows, output_sources(tree))
        out = QueryResult(columns, self._jsonable(rows), masked, truncated, int((time.monotonic() - started) * 1000),
                          sorted(set(urns)))
        self._audit(actor, "data.queried", source.connection,
                    {"sql": mask_sql(sql), "tables": out.tables, "rows": len(rows), "masked": masked})
        self._read(actor, out.tables)
        return out.as_dict()

    def _read(self, actor: str, urns: list[str]) -> None:
        if self.on_read is not None:
            try:
                self.on_read(actor, urns)
            except Exception:  # noqa: BLE001 - usage records never fail a read
                logger.exception("could not record a data read")

    def _audit(self, actor: str, action: str, subject: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("data", action, actor, subject, details)
