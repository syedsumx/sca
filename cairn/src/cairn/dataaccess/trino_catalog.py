"""Trino's tables in Cairn's catalog: Cairn reads each Trino connection's structure (``information_schema``
and table comments) as its own crawler, ``cairn-catalog``, which Cairn's access control lets see every
table and column but read no rows. Every table becomes a dataset (properties, schema, subtype) under the
URN access control, lineage and usage already use, so the SQL editor and Ask-your-data can query it and
policies, masks and lineage refer to the same dataset.

* Tables gone since the last sync are marked removed (``status``), so the catalog follows Trino.
* Runs from **Sync catalog** (``POST /v1/trino/catalog-sync``, needs ``MANAGE_INGESTION``) and on a schedule
  (``CAIRN_TRINO_CATALOG_SYNC_SECONDS``), claimed so that one replica runs each sync.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

from sqlalchemy import BigInteger, Column, PrimaryKeyConstraint, String, Table, delete, select, update

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

trino_catalog_tables = Table(
    "cairn_trino_catalog_tables", metadata,
    Column("connection", String(128), nullable=False),
    Column("urn", String(512), nullable=False),
    PrimaryKeyConstraint("connection", "urn"),
)
trino_catalog_runs = Table(
    "cairn_trino_catalog_runs", metadata,
    Column("connection", String(128), primary_key=True),
    Column("next_ms", BigInteger, nullable=False),
    Column("last_ms", BigInteger, nullable=True),
)

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]*$")
MAX_ROWS = 200_000


def field_type(trino_type: str) -> str:
    """Trino's type → the catalog's field type."""
    t = trino_type.lower()
    if t.startswith(("varchar", "char", "json", "uuid", "ipaddress")):
        return "string"
    if t.startswith(("tinyint", "smallint", "integer", "bigint", "decimal", "real", "double")):
        return "number"
    if t.startswith("boolean"):
        return "boolean"
    if t.startswith("date"):
        return "date"
    if t.startswith(("timestamp", "time")):
        return "time"
    if t.startswith("varbinary"):
        return "bytes"
    if t.startswith("array"):
        return "array"
    if t.startswith("map"):
        return "map"
    if t.startswith("row"):
        return "record"
    return "string"


class CatalogSyncError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class TrinoCatalogSync:
    def __init__(self, engine, graph, data_access, access, audit=None):
        self.engine, self.graph, self.data_access, self.access, self.audit = engine, graph, data_access, access, audit

    def connections(self) -> dict[str, dict]:
        """The trino:// connections that run as the signed-in user (the ones Cairn signs in to)."""
        registry = self.data_access.registry
        defs = registry.definitions() if registry is not None else {}
        return {n: d for n, d in (defs or {}).items()
                if isinstance(d, dict) and str(d.get("url", "")).startswith("trino://") and d.get("run_as_user")}

    def _query(self, definition: dict, sql: str) -> list[list[Any]]:
        from cairn.dataaccess.trino import CRAWLER

        _, rows, truncated = self.data_access.run_on_trino(definition, f"urn:li:corpuser:{CRAWLER}", sql, MAX_ROWS)
        if truncated:
            raise CatalogSyncError(f"more than {MAX_ROWS} columns: sync this catalog in parts", 413)
        return rows

    def sync(self, connection: str, actor: str) -> dict:
        from cairn.common.proposals import make_mcp
        from cairn.sdk.source import Dataset, Field, to_mcps

        definition = self.connections().get(connection)
        if definition is None:
            raise CatalogSyncError(f"no trino:// connection {connection!r} with run_as_user", 404)
        from urllib.parse import urlsplit

        from cairn.quality.collectors import _expand_env

        path = [p for p in urlsplit(_expand_env(definition["url"])).path.split("/") if p]
        if not path or not IDENT.match(path[0]):
            raise CatalogSyncError("the connection's URL names no Trino catalog (trino://host:port/<catalog>)")
        cat = path[0]
        started = time.time()
        cols = self._query(definition, (
            f'SELECT table_schema, table_name, column_name, data_type, is_nullable, comment FROM "{cat}".information_schema.columns '
            "WHERE table_schema <> 'information_schema' ORDER BY table_schema, table_name, ordinal_position"))
        kinds = {(r[0], r[1]): r[2] for r in self._query(definition, (
            f'SELECT table_schema, table_name, table_type FROM "{cat}".information_schema.tables '
            "WHERE table_schema <> 'information_schema'"))}
        comments = {(r[0], r[1]): r[2] for r in self._query(definition, (
            "SELECT schema_name, table_name, comment FROM system.metadata.table_comments "
            f"WHERE catalog_name = '{cat}' AND comment IS NOT NULL"))}
        tables: dict[tuple[str, str], list[Field]] = {}
        for schema, table, column, dtype, nullable, comment in cols:
            tables.setdefault((schema, table), []).append(Field(
                path=column, type=field_type(dtype), native_type=dtype, nullable=str(nullable).upper() != "NO",
                description=comment or None))
        mcps: list[dict] = []
        urns: set[str] = set()
        for (schema, table), fields in tables.items():
            platform, name, env = self.access.dataset_name(cat, schema, table)
            ds = Dataset(name=name, fields=fields, description=comments.get((schema, table)) or None,
                         subtype="View" if "VIEW" in str(kinds.get((schema, table), "")).upper() else "Table",
                         properties={"trino_catalog": cat, "trino_schema": schema, "trino_table": table,
                                     "cairn_connection": connection})
            out = to_mcps(platform, [ds], env)
            urns.add(out[0]["entityUrn"])
            mcps += out
        t = trino_catalog_tables
        with self.engine.connect() as conn:
            before = {r[0] for r in conn.execute(select(t.c.urn).where(t.c.connection == connection))}
        gone = sorted(before - urns)
        mcps += [make_mcp(u, "status", {"removed": True}) for u in gone]
        self.graph.emit(mcps)
        with self.engine.begin() as conn:
            conn.execute(delete(t).where(t.c.connection == connection))
            if urns:
                conn.execute(t.insert(), [{"connection": connection, "urn": u} for u in sorted(urns)])
        result = {"connection": connection, "catalog": cat, "tables": len(urns), "columns": len(cols),
                  "removed": len(gone), "seconds": round(time.time() - started, 2)}
        if self.audit is not None:
            self.audit.record("trino", "trino.catalog_synced", actor, None, result)
        return result

    # ---- the schedule -------------------------------------------------------------------------
    def due(self, every_s: float, now_ms: Optional[int] = None) -> list[str]:
        """Claims the connections whose sync is due (one replica each)."""
        now = now_ms or int(time.time() * 1000)
        r = trino_catalog_runs
        claimed = []
        for name in self.connections():
            with self.engine.begin() as conn:
                row = conn.execute(select(r.c.next_ms).where(r.c.connection == name)).first()
                if row is None:
                    conn.execute(r.insert().values(connection=name, next_ms=now + int(every_s * 1000), last_ms=now))
                    claimed.append(name)
                elif row[0] <= now and conn.execute(update(r).where((r.c.connection == name) & (r.c.next_ms == row[0]))
                                                    .values(next_ms=now + int(every_s * 1000), last_ms=now)).rowcount == 1:
                    claimed.append(name)
        return claimed

    def run_due(self, every_s: float) -> None:
        from cairn.dataaccess.trino import LINEAGE_ACTOR

        for name in self.due(every_s):
            try:
                logger.info("Trino catalog sync %s: %s", name, self.sync(name, LINEAGE_ACTOR))
            except Exception as e:  # noqa: BLE001 - one connection's failure never stops the others
                logger.warning("Trino catalog sync %s failed: %s", name, e)
