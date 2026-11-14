"""Spark event logs in Cairn Store, with no store key in the job.

When CAIRN_SPARK_EVENT_LOG_DIR is ``s3a://<bucket>/<prefix>``, the job exchanges its lakehouse token
at the store's STS (``AssumeRoleWithWebIdentity``) for temporary credentials of the event-log role
(CAIRN_SPARK_EVENT_LOG_ROLE), valid CAIRN_SPARK_EVENT_LOG_SECONDS (default 12 hours, the store's
maximum), and writes its event log there. The role should allow writing under that prefix only. The
history server reads the prefix with its own read-only key.

CAIRN_STORE_URL is the store's S3 endpoint as Spark reaches it. The credentials are in Spark's
settings, which cairn_spark redacts from the UI and the event log itself.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import os
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import quote, urlparse

import httpx

REGION = "us-east-1"


def assume_role(store_url: str, role_arn: str, token: str, seconds: int, verify=True) -> dict:
    """The role's temporary credentials for ``seconds`` (or as long as the role allows, if less)."""
    import re

    r = _assume(store_url, role_arn, token, seconds, verify)
    m = re.search(r"DurationSeconds must be between \d+ and (\d+)", r.text) if r.status_code == 400 else None
    if m and int(m.group(1)) < seconds:
        r = _assume(store_url, role_arn, token, int(m.group(1)), verify)
    if r.status_code != 200:
        raise RuntimeError(f"the store refused event-log credentials ({r.status_code}): {r.text[:300]}")
    root = ET.fromstring(r.text)
    found = {el.tag.rsplit("}", 1)[-1]: (el.text or "") for el in root.iter()}
    return {"access_key": found["AccessKeyId"], "secret_key": found["SecretAccessKey"],
            "session_token": found["SessionToken"], "expiration": found.get("Expiration", "")}


def _assume(store_url: str, role_arn: str, token: str, seconds: int, verify) -> httpx.Response:
    return httpx.post(store_url.rstrip("/") + "/", data={
        "Action": "AssumeRoleWithWebIdentity", "Version": "2011-06-15", "RoleArn": role_arn,
        "RoleSessionName": "spark-event-log", "WebIdentityToken": token, "DurationSeconds": str(seconds)},
        timeout=30, verify=verify)


def _sign(method: str, url: str, creds: dict, body: bytes = b"") -> dict:
    """AWS Signature Version 4 headers for one S3 request (path-style)."""
    u = urlparse(url)
    now = _dt.datetime.now(_dt.timezone.utc)
    amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    payload = hashlib.sha256(body).hexdigest()
    headers = {"host": u.netloc, "x-amz-content-sha256": payload, "x-amz-date": amz,
               "x-amz-security-token": creds["session_token"]}
    signed = ";".join(sorted(headers))
    canonical = "\n".join([method, quote(u.path or "/", safe="/-_.~"), u.query,
                           "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed, payload])
    scope = f"{day}/{REGION}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + creds["secret_key"]).encode()
    for part in (day, REGION, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={creds['access_key']}/{scope}, "
                                f"SignedHeaders={signed}, Signature={sig}")
    headers.pop("host")
    return headers


def ensure_directory(store_url: str, bucket: str, prefix: str, creds: dict, verify=True) -> None:
    """Spark wants the event-log directory to exist; in S3 that is a marker object."""
    url = f"{store_url.rstrip('/')}/{bucket}/{prefix.strip('/')}/"
    r = httpx.put(url, content=b"", headers=_sign("PUT", url, creds), timeout=30, verify=verify)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"cannot create the event-log directory {url} ({r.status_code}): {r.text[:300]}")


def event_log_conf(lakehouse_token: str, user: str, verify=True) -> dict:
    """Spark settings for event logs (CAIRN_SPARK_EVENT_LOG_DIR), or {} when they are off."""
    where = os.environ.get("CAIRN_SPARK_EVENT_LOG_DIR", "")
    if not where:
        return {}
    conf = {"spark.eventLog.enabled": "true", "spark.eventLog.dir": where,
            # Who ran it, for the history server's sign-in proxy (which shows people their own runs).
            "spark.cairn.user": user}
    if not where.startswith("s3a://"):
        return conf
    store, role = os.environ.get("CAIRN_STORE_URL", ""), os.environ.get("CAIRN_SPARK_EVENT_LOG_ROLE", "")
    if not (store and role):
        raise RuntimeError("event logs in the store need CAIRN_STORE_URL and CAIRN_SPARK_EVENT_LOG_ROLE")
    seconds = int(os.environ.get("CAIRN_SPARK_EVENT_LOG_SECONDS", "43200"))
    creds = assume_role(store, role, lakehouse_token, seconds, verify)
    bucket, _, prefix = where[len("s3a://"):].partition("/")
    ensure_directory(store, bucket, prefix, creds, verify)
    h = "spark.hadoop.fs.s3a"
    return {**conf, f"{h}.endpoint": store, f"{h}.path.style.access": "true", f"{h}.endpoint.region": REGION,
            f"{h}.aws.credentials.provider": "org.apache.hadoop.fs.s3a.TemporaryAWSCredentialsProvider",
            f"{h}.access.key": creds["access_key"], f"{h}.secret.key": creds["secret_key"],
            f"{h}.session.token": creds["session_token"]}


def history_server_opts(where: str, store_url: Optional[str] = None) -> str:
    """SPARK_HISTORY_OPTS for a history server reading ``where`` (its store key comes from the
    environment: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY, never on the command line)."""
    opts = [f"-Dspark.history.fs.logDirectory={where}"]
    if where.startswith("s3a://") and store_url:
        opts += [f"-Dspark.hadoop.fs.s3a.endpoint={store_url}", "-Dspark.hadoop.fs.s3a.path.style.access=true",
                 f"-Dspark.hadoop.fs.s3a.endpoint.region={REGION}",
                 "-Dspark.hadoop.fs.s3a.aws.credentials.provider="
                 "software.amazon.awssdk.auth.credentials.EnvironmentVariableCredentialsProvider"]
    return " ".join(opts)
