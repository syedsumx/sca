"""pyiceberg and DuckDB on the Cairn lakehouse, as a Cairn identity.

``catalog()`` opens a lakehouse session at Cairn (the same as Spark's: CAIRN_URL, CAIRN_TOKEN[_FILE] or
CAIRN_WORKLOAD_TOKEN_FILE, CAIRN_CATALOG_URI) and returns a pyiceberg catalog on Cairn Catalog. Every
table is decided by Cairn for that user, storage keys are per table (read-only unless the user may
write), and tables a mask or row filter applies to are refused (query them through Trino)::

    from cairn_lakehouse import catalog, duckdb_connection

    orders = catalog().load_table("sales.orders").scan(row_filter="region = 'EU'").to_arrow()

    con = duckdb_connection(["analytics.products"])   # each table as a DuckDB view, read through pyiceberg
    con.sql("SELECT count(*) FROM products").fetchall()

DuckDB reads the tables through pyiceberg (with Cairn's per-table keys), so no DuckDB extension or
store key is needed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Sequence

import httpx

__version__ = "0.1.0"


class CairnLakehouseError(Exception):
    pass


def _cairn_token(cairn_url: str, verify) -> str:
    workload = os.environ.get("CAIRN_WORKLOAD_TOKEN_FILE")
    if workload:
        r = httpx.post(f"{cairn_url}/v1/auth/workload-token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": Path(workload).read_text().strip(),
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt"}, timeout=30, verify=verify)
        if r.status_code != 200:
            raise CairnLakehouseError(f"Cairn refused the workload token ({r.status_code}): {r.text[:300]}")
        return r.json()["access_token"]
    if os.environ.get("CAIRN_TOKEN_FILE"):
        return Path(os.environ["CAIRN_TOKEN_FILE"]).read_text().strip()
    if os.environ.get("CAIRN_TOKEN"):
        return os.environ["CAIRN_TOKEN"]
    raise CairnLakehouseError("set CAIRN_TOKEN (or CAIRN_TOKEN_FILE, or CAIRN_WORKLOAD_TOKEN_FILE)")


def catalog(name: str = "lakehouse", *, cairn_url: Optional[str] = None, catalog_uri: Optional[str] = None,
            token: Optional[str] = None):
    """A pyiceberg catalog on Cairn Catalog, signed in as the Cairn user of the token."""
    from pyiceberg.catalog.rest import RestCatalog

    cairn_url = (cairn_url or os.environ.get("CAIRN_URL", "")).rstrip("/")
    catalog_uri = catalog_uri or os.environ.get("CAIRN_CATALOG_URI", "")
    if not cairn_url or not catalog_uri:
        raise CairnLakehouseError("set CAIRN_URL (Cairn's API) and CAIRN_CATALOG_URI (Cairn Catalog)")
    verify = os.environ.get("CAIRN_CA_FILE") or True
    tok = token or _cairn_token(cairn_url, verify)
    r = httpx.post(f"{cairn_url}/v1/lakehouse/token", json={}, headers={"Authorization": f"Bearer {tok}"},
                   timeout=30, verify=verify)
    if r.status_code != 200:
        raise CairnLakehouseError(f"Cairn refused a lakehouse session ({r.status_code}): {r.text[:300]}")
    props = {"uri": catalog_uri, "credential": r.json()["credential"], "scope": "catalog",
             "oauth2-server-uri": f"{cairn_url}/v1/lakehouse/oauth/tokens",
             "header.X-Iceberg-Access-Delegation": "vended-credentials", "s3.path-style-access": "true"}
    if isinstance(verify, str):
        props["ssl"] = {"cabundle": verify}
    return RestCatalog(name, **props)


def duckdb_connection(tables: Sequence[str], connection=None, cat=None, **scan):
    """A DuckDB connection with each table (``namespace.table``) as a view named after it, read through
    pyiceberg as the catalog's user. ``scan`` arguments (``row_filter``, ``selected_fields``) apply to each."""
    import duckdb

    con = connection or duckdb.connect()
    cat = cat or catalog()
    for t in tables:
        view = t.rsplit(".", 1)[-1]
        con.register(view, cat.load_table(t).scan(**scan).to_arrow())
    return con
