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
* Long jobs keep going: the session's catalog tokens (an hour each) are renewed by Iceberg itself at
  Cairn, with the session credential, on the driver and every executor; storage keys are renewed
  through the catalog. Both are decided again each time. A session lasts at most what Cairn allows
  (CAIRN_LAKEHOUSE_MAX_SESSION, 7 days by default), and ends early when the user is suspended or
  their sessions are ended (POST /v1/lakehouse/sessions/revoke).

Configuration (environment, or keyword arguments)::

    CAIRN_URL             Cairn's API, e.g. https://cairn.example.com/cairn/api
    CAIRN_TOKEN[_FILE]    the caller's Cairn token
    CAIRN_WORKLOAD_TOKEN_FILE  instead: the workload's platform token (Kubernetes, CI), exchanged at Cairn
    CAIRN_CATALOG_URI     Cairn Catalog, e.g. https://catalog.example.com
    CAIRN_CA_FILE         a CA for Cairn's certificate (default: the system's)
    CAIRN_SPARK_TOKEN_TTL seconds the first catalog token lasts (default: Cairn's, an hour); tokens are renewed
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


def _token(token: Optional[str], cairn_url: str = "", verify=True) -> str:
    if token:
        return token
    workload = os.environ.get("CAIRN_WORKLOAD_TOKEN_FILE")
    if workload:  # the workload's platform token (Kubernetes, CI), exchanged at Cairn
        r = httpx.post(f"{cairn_url.rstrip('/')}/v1/auth/workload-token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": Path(workload).read_text().strip(),
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt"}, timeout=30, verify=verify)
        if r.status_code != 200:
            raise CairnSparkError(f"Cairn refused the workload token ({r.status_code}): {r.text[:300]}")
        return r.json()["access_token"]
    path = os.environ.get("CAIRN_TOKEN_FILE")
    if path:
        return Path(path).read_text().strip()
    t = os.environ.get("CAIRN_TOKEN", "")
    if not t:
        raise CairnSparkError("set CAIRN_TOKEN (or CAIRN_TOKEN_FILE): your Cairn token")
    return t


def catalog_token(cairn_url: str, token: str, ttl: Optional[int], verify) -> dict:
    """A Cairn Catalog token for the Cairn user ``token`` belongs to."""
    r = httpx.post(f"{cairn_url.rstrip('/')}/v1/lakehouse/token", json={"ttl_seconds": ttl} if ttl else {},
                   headers={"Authorization": f"Bearer {token}"}, timeout=30, verify=verify)
    if r.status_code != 200:
        raise CairnSparkError(f"Cairn refused a lakehouse token ({r.status_code}): {r.text[:300]}")
    return r.json()


def lakehouse_conf(cairn_url: str, catalog_uri: str, session: str, cairn_token: str, region: str = "us-east-1",
                   lineage: bool = True, app_name: str = "") -> dict[str, str]:
    """The Spark settings for the lakehouse catalog and lineage (also usable with spark-submit --conf).

    ``session`` is the lakehouse session credential (``<session>:<secret>``, from /v1/lakehouse/token):
    Iceberg gets its token with it at Cairn and renews it before it expires, on the driver and on each
    executor; Cairn checks the session every time."""
    c = f"spark.sql.catalog.{CATALOG}"
    conf = {
        "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        c: "org.apache.iceberg.spark.SparkCatalog",
        f"{c}.type": "rest",
        f"{c}.uri": catalog_uri,
        f"{c}.rest.auth.type": "oauth2",
        f"{c}.credential": session,
        f"{c}.scope": "catalog",
        f"{c}.oauth2-server-uri": f"{cairn_url.rstrip('/')}/v1/lakehouse/oauth/tokens",
        f"{c}.token-refresh-enabled": "true",
        # Keep the session credential (and Cairn's token, for lineage) out of the Spark UI and event logs.
        "spark.redaction.regex": r"(?i)secret|password|token|access[.]key|credential|apikey|api[.]key",
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
            # The session credential, not a token: lineage then lasts as long as the session (a token
            # given here would never be renewed, and a job's lineage would stop when it expired).
            "spark.openlineage.transport.auth.apiKey": session,
            "spark.openlineage.namespace": os.environ.get("CAIRN_SPARK_NAMESPACE", "spark"),
            # What Cairn prices a run by (cores reserved x wall-clock time).
            "spark.openlineage.capturedProperties": ",".join(CAPTURED),
            **({"spark.openlineage.parentJobName": app_name} if app_name else {}),
        })
    return conf


CAPTURED = ("spark.master", "spark.app.name", "spark.driver.cores", "spark.executor.instances",
            "spark.executor.cores", "spark.dynamicAllocation.enabled", "spark.dynamicAllocation.maxExecutors")


def session_conf(app_name: str = "cairn", *, cairn_url: Optional[str] = None, token: Optional[str] = None,
                 catalog_uri: Optional[str] = None, ttl: Optional[int] = None, lineage: bool = True) -> tuple[dict, str]:
    """Opens a lakehouse session at Cairn and returns its Spark settings and the Cairn user, for any
    Spark program (Python, Scala, Java, SQL): ``lakehouse_session`` and ``cairn-spark`` use it."""
    cairn_url = cairn_url or os.environ.get("CAIRN_URL", "")
    catalog_uri = catalog_uri or os.environ.get("CAIRN_CATALOG_URI", "")
    if not cairn_url or not catalog_uri:
        raise CairnSparkError("set CAIRN_URL (Cairn's API) and CAIRN_CATALOG_URI (Cairn Catalog)")
    verify = os.environ.get("CAIRN_CA_FILE") or True
    tok = _token(token, cairn_url, verify)
    ttl = ttl or (int(os.environ["CAIRN_SPARK_TOKEN_TTL"]) if os.environ.get("CAIRN_SPARK_TOKEN_TTL") else None)
    ct = catalog_token(cairn_url, tok, ttl, verify)
    conf = lakehouse_conf(cairn_url, catalog_uri, ct["credential"], tok, lineage=lineage, app_name=app_name)
    from cairn_spark.eventlog import event_log_conf

    # For the history server (secrets are redacted in event logs): a directory, or Cairn Store with
    # temporary credentials for the event-log role.
    conf.update(event_log_conf(ct["token"], ct["user"], verify))
    return conf, ct["user"]


def lakehouse_session(app_name: str = "cairn", *, cairn_url: Optional[str] = None, token: Optional[str] = None,
                      catalog_uri: Optional[str] = None, ttl: Optional[int] = None, lineage: bool = True,
                      master: Optional[str] = None, extra_conf: Optional[dict] = None):
    """A SparkSession on the lakehouse as the Cairn user of ``token`` (or CAIRN_TOKEN)."""
    from pyspark.sql import SparkSession

    conf, user = session_conf(app_name, cairn_url=cairn_url, token=token, catalog_uri=catalog_uri, ttl=ttl, lineage=lineage)
    b = SparkSession.builder.appName(app_name)
    if master:
        b = b.master(master)
    for k, v in {**conf, **(extra_conf or {})}.items():
        b = b.config(k, v)
    spark = b.getOrCreate()
    spark.sparkContext.setLocalProperty("cairn.user", user)
    return spark
