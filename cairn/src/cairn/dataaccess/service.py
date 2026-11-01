"""Looking at the data from the catalog: a table preview and a read-only SQL editor.

Both run through a named warehouse connection with a read-only role. A connection says which
catalog datasets it serves::

    connections:
      warehouse:
        url: postgresql+psycopg://${WH_USER}:${WH_PASSWORD}@warehouse:5432/analytics
        catalog: {platform: postgres, database: analytics}     # datasets analytics.<schema>.<table>

A Trino connection can run each query as the signed-in user, so Trino's own access control (Cairn's
policies through Trino's OPA plugin: masks and row filters included) applies on top::

    connections:
      lakehouse:
        url: trino://cairn@cairn-trino:8080/lakehouse      # trino://[principal[:password]@]host:port/catalog
        run_as_user: true                                  # the query's Trino user is the caller
        catalog: {platform: trino, database: lakehouse}

With a password the connection uses HTTPS and the principal impersonates the caller (Trino must allow it).

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


def capped(tree: exp.Expression, n: int, dialect: str) -> str:
    """The query with at most ``n`` rows. A plain SELECT gets the cap as its own LIMIT (a smaller one it
    has is kept), so its ORDER BY still holds: engines such as Trino drop the order of a subquery."""
    if isinstance(tree, exp.Select):
        current = tree.args.get("limit")
        try:
            have = int(current.expression.name) if current is not None and current.expression is not None else None
        except (TypeError, ValueError):
            have = None
        if current is None or (have is not None and have > n):
            tree = tree.copy().limit(n)
        elif have is None:
            return f"SELECT * FROM ({tree.sql(dialect=dialect)}) AS cairn_q LIMIT {n}"
        return tree.sql(dialect=dialect)
    return f"SELECT * FROM ({tree.sql(dialect=dialect)}) AS cairn_q LIMIT {n}"


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
        self.trino_tokens = None   # TrinoTokens: run_as_user queries sign in as the caller
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
    def _execute(self, source: Source, sql: str, limit: int, actor: str = "") -> tuple[list[str], list[list[Any]], bool]:
        definition = (self.registry.definitions() or {}).get(source.connection) or {}
        if definition.get("run_as_user"):
            return self._execute_as(definition, sql, limit, actor)
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

    def _execute_as(self, definition: dict, sql: str, limit: int, actor: str) -> tuple[list[str], list[list[Any]], bool]:
        """Runs ``sql`` on Trino with the caller as the query's user (``run_as_user``)."""
        return self.run_on_trino(definition, actor, sql, limit)

    def run_on_trino(self, definition: dict, actor: str, sql: str, limit: int) -> tuple[list[str], list[list[Any]], bool]:
        """Runs ``sql`` on a trino:// connection as ``actor``: signed in with a short Cairn token (or, with a
        password in the URL, as that user impersonating ``actor``)."""
        from urllib.parse import parse_qs, unquote, urlsplit

        try:
            import trino
        except ImportError:
            raise DataAccessError("this connection needs the trino client (pip install 'cairn-platform[trino]')", 503)
        from cairn.quality.collectors import _expand_env

        url = urlsplit(_expand_env(definition["url"]))
        if url.scheme != "trino":
            raise DataAccessError("run_as_user is for trino:// connections", 400)
        user = actor.removeprefix("urn:li:corpuser:")
        if not user or user == actor:
            raise DataAccessError("queries on this connection run as a Cairn user", 403)
        opts = {k: v[-1] for k, v in parse_qs(url.query).items()}
        scheme = opts.get("protocol", "http")
        if scheme not in ("http", "https"):
            raise DataAccessError("protocol must be http or https", 400)
        auth, headers = None, {}
        if url.password:
            auth, scheme = trino.auth.BasicAuthentication(unquote(url.username or ""), unquote(url.password)), "https"
        elif self.trino_tokens is not None:
            # Signed in as the caller with a token from Cairn (Trino checks it against Cairn's key), so
            # Trino knows the user from the token, not from a name Cairn claims.
            headers["Authorization"] = "Bearer " + self.trino_tokens.mint(actor, 300)["token"]
        verify = opts.get("verify", "true")
        verify = False if verify == "false" else (True if verify == "true" else verify)
        session = None
        if isinstance(verify, str):
            # The connection's own CA: requests would let REQUESTS_CA_BUNDLE in the environment win over it.
            import requests

            session = requests.Session()
            session.trust_env = False
            session.verify = verify
        path = [p for p in url.path.split("/") if p]
        conn = trino.dbapi.connect(host=url.hostname, port=url.port or (443 if scheme == "https" else 8080), user=user,
                                   catalog=path[0] if path else None, schema=path[1] if len(path) > 1 else None,
                                   http_scheme=scheme, auth=auth, http_headers=headers or None, verify=verify,
                                   http_session=session, source="cairn", request_timeout=max(5.0, self.timeout_ms / 1000))
        try:
            cur = conn.cursor()
            cur.execute(sql)
            rows = [list(r) for r in cur.fetchmany(limit + 1)]
            columns = [d[0] for d in cur.description or []]
        except trino.exceptions.TrinoUserError as e:
            if e.error_name == "PERMISSION_DENIED":
                raise DataAccessError(f"Trino refused the query: {e.message[:300]}", 403)
            raise DataAccessError(f"the warehouse refused the query: {e.message[:300]}")
        except trino.exceptions.HttpError as e:
            if "401" in str(e):
                raise DataAccessError("Trino did not accept Cairn's sign-in (check http-server.authentication.jwt "
                                      "points at Cairn's /v1/trino/jwks.json)", 502)
            raise DataAccessError(f"Trino could not be reached: {str(e)[:300]}", 502)
        finally:
            conn.close()
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
        columns, rows, truncated = self._execute(source, sql, min(limit, self.max_rows), actor)
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
        wrapped = capped(tree, limit + 1, source.dialect)
        started = time.monotonic()
        try:
            columns, rows, truncated = self._execute(source, wrapped, limit, actor)
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
