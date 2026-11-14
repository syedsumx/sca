"""Governed volumes from Spark: temporary store credentials for one volume, decided by Cairn for the caller.

``volume_conf("raw-files")`` asks Cairn (``POST /v1/volumes/{name}/credentials``) for credentials narrowed to that
volume's bucket, read-only unless ``access="write"`` and the caller may write, and returns the per-bucket S3A
settings that use them. Pass them to ``lakehouse_session(extra_conf=...)``, or apply them to a running session
with ``use_volume(spark, name)``. Then read and write ``s3a://cairn-vol-<name>/...``. The credentials last 15 to
60 minutes (Cairn's ``CAIRN_VOLUMES_CREDENTIAL_TTL``); call ``use_volume`` again for a longer job. Spark redacts
the keys in its UI and event logs (their names hold "secret" and "token").
"""

from __future__ import annotations

import os
from typing import Optional

import httpx


def volume_credentials(name: str, access: str = "read", *, cairn_url: Optional[str] = None,
                       token: Optional[str] = None) -> dict:
    from cairn_spark import CairnSparkError, _token

    cairn_url = (cairn_url or os.environ.get("CAIRN_URL", "")).rstrip("/")
    if not cairn_url:
        raise CairnSparkError("set CAIRN_URL (Cairn's API)")
    verify = os.environ.get("CAIRN_CA_FILE") or True
    tok = _token(token, cairn_url, verify)
    r = httpx.post(f"{cairn_url}/v1/volumes/{name}/credentials", json={"access": access},
                   headers={"Authorization": f"Bearer {tok}"}, timeout=30, verify=verify)
    if r.status_code != 200:
        raise CairnSparkError(f"Cairn refused credentials for volume {name} ({r.status_code}): {r.text[:300]}")
    return r.json()


def _conf(c: dict) -> dict:
    b = f"spark.hadoop.fs.s3a.bucket.{c['bucket']}"
    return {f"{b}.endpoint": c["endpoint"], f"{b}.path.style.access": "true",
            f"{b}.endpoint.region": c.get("region") or "us-east-1",
            f"{b}.aws.credentials.provider": "org.apache.hadoop.fs.s3a.TemporaryAWSCredentialsProvider",
            f"{b}.access.key": c["access_key_id"], f"{b}.secret.key": c["secret_access_key"],
            f"{b}.session.token": c["session_token"],
            f"{b}.connection.ssl.enabled": "true" if c["endpoint"].startswith("https") else "false"}


def volume_conf(name: str, access: str = "read", *, cairn_url: Optional[str] = None,
                token: Optional[str] = None) -> dict:
    """Spark settings (``spark.hadoop.fs.s3a.bucket.<bucket>.*``) for volume ``name`` as the caller."""
    return _conf(volume_credentials(name, access, cairn_url=cairn_url, token=token))


def use_volume(spark, name: str, access: str = "read", **kw) -> str:
    """Applies a volume's settings to a running session and returns its path (``s3a://cairn-vol-<name>/``).
    Call it again to renew the credentials of a long job."""
    c = volume_credentials(name, access, **kw)
    hc = spark.sparkContext._jsc.hadoopConfiguration()
    for k, v in _conf(c).items():
        hc.set(k.removeprefix("spark.hadoop."), v)
    # S3A keeps one filesystem per bucket: close it so the next use signs with the new credentials.
    jvm = spark.sparkContext._jvm
    jvm.org.apache.hadoop.fs.FileSystem.get(jvm.java.net.URI(c["path"]), hc).close()
    return c["path"]
