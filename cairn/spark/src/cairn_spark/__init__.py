"""Spark on the Cairn lakehouse, as a Cairn identity.

``lakehouse_session()`` signs in to Cairn with the caller's Cairn token (a person's or a pipeline user's
personal access token), gets a short-lived token for Cairn Catalog for that user, and returns a
SparkSession whose ``lakehouse`` catalog is Cairn Catalog (Iceberg REST):

* Cairn Catalog asks Cairn about every table the session touches, for that user: tables they may not
  see are not listed, and reading a table a mask or row filter applies to is refused (query it through
  Trino, which applies them). Writes need Cairn's write or manage privilege on the table.
* Storage access is per table: the catalog hands Spark temporary credentials for one table's location
  (read-only unless the user may write), never a store key.
* Lineage goes to Cairn through OpenLineage (the Spark listener), as the same user.

Configuration (environment, or keyword arguments)::

    CAIRN_URL             Cairn's API, e.g. https://cairn.example.com/cairn/api
    CAIRN_TOKEN[_FILE]    the caller's Cairn token
    CAIRN_CATALOG_URI     Cairn Catalog, e.g. https://catalog.example.com
    CAIRN_CA_FILE         a CA for Cairn's certificate (default: the system's)
    CAIRN_SPARK_TOKEN_TTL seconds the catalog token lasts (14400; at most 43200): the longest job
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import httpx

__version__ = "0.1.0"
CATALOG = "lakehouse"


class CairnSparkError(Exception):
    pass


def _token(token: Optional[str]) -> str:
    if token:
        return token
    path = os.environ.get("CAIRN_TOKEN_FILE")
    if path:
        return Path(path).read_text().strip()
    t = os.environ.get("CAIRN_TOKEN", "")
    if not t:
        raise CairnSparkError("set CAIRN_TOKEN (or CAIRN_TOKEN_FILE): your Cairn token")
    return t


def catalog_token(cairn_url: str, token: str, ttl: int, verify) -> dict:
    """A Cairn Catalog token for the Cairn user ``token`` belongs to."""
    r = httpx.post(f"{cairn_url.rstrip('/')}/v1/lakehouse/token", json={"ttl_seconds": ttl},
                   headers={"Authorization": f"Bearer {token}"}, timeout=30, verify=verify)
    if r.status_code != 200:
        raise CairnSparkError(f"Cairn refused a lakehouse token ({r.status_code}): {r.text[:300]}")
    return r.json()


def lakehouse_conf(cairn_url: str, catalog_uri: str, catalog_tok: str, cairn_token: str, region: str = "us-east-1",
                   lineage: bool = True, app_name: str = "") -> dict[str, str]:
    """The Spark settings for the lakehouse catalog and lineage (also usable with spark-submit --conf)."""
    c = f"spark.sql.catalog.{CATALOG}"
    conf = {
        "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        c: "org.apache.iceberg.spark.SparkCatalog",
        f"{c}.type": "rest",
        f"{c}.uri": catalog_uri,
        f"{c}.token": catalog_tok,
        # Per-table temporary credentials from the catalog; Spark holds no store key.
        f"{c}.header.X-Iceberg-Access-Delegation": "vended-credentials",
        f"{c}.io-impl": "org.apache.iceberg.aws.s3.S3FileIO",
        f"{c}.client.region": region,
        f"{c}.s3.path-style-access": "true",
        "spark.sql.defaultCatalog": CATALOG,
    }
    if lineage:
        conf.update({
            "spark.extraListeners": "io.openlineage.spark.agent.OpenLineageSparkListener",
            "spark.openlineage.transport.type": "http",
            "spark.openlineage.transport.url": cairn_url.rstrip("/"),
            "spark.openlineage.transport.endpoint": "/openlineage/api/v1/lineage",
            "spark.openlineage.transport.auth.type": "api_key",
            "spark.openlineage.transport.auth.apiKey": cairn_token,
            "spark.openlineage.namespace": os.environ.get("CAIRN_SPARK_NAMESPACE", "spark"),
            **({"spark.openlineage.parentJobName": app_name} if app_name else {}),
        })
    return conf


def lakehouse_session(app_name: str = "cairn", *, cairn_url: Optional[str] = None, token: Optional[str] = None,
                      catalog_uri: Optional[str] = None, ttl: Optional[int] = None, lineage: bool = True,
                      master: Optional[str] = None, extra_conf: Optional[dict] = None):
    """A SparkSession on the lakehouse as the Cairn user of ``token`` (or CAIRN_TOKEN)."""
    from pyspark.sql import SparkSession

    cairn_url = cairn_url or os.environ.get("CAIRN_URL", "")
    catalog_uri = catalog_uri or os.environ.get("CAIRN_CATALOG_URI", "")
    if not cairn_url or not catalog_uri:
        raise CairnSparkError("set CAIRN_URL (Cairn's API) and CAIRN_CATALOG_URI (Cairn Catalog)")
    tok = _token(token)
    verify = os.environ.get("CAIRN_CA_FILE") or True
    ct = catalog_token(cairn_url, tok, int(ttl or os.environ.get("CAIRN_SPARK_TOKEN_TTL", "14400")), verify)
    b = SparkSession.builder.appName(app_name)
    if master:
        b = b.master(master)
    for k, v in {**lakehouse_conf(cairn_url, catalog_uri, ct["token"], tok, lineage=lineage, app_name=app_name),
                 **(extra_conf or {})}.items():
        b = b.config(k, v)
    spark = b.getOrCreate()
    spark.sparkContext.setLocalProperty("cairn.user", ct["user"])
    return spark
