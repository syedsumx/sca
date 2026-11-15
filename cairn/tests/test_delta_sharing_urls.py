"""Delta Sharing pre-signed URLs never outlast the share: each URL's lifetime is counted when it is signed, so the
work before signing (loading the table from the catalog, planning its files) can't push a URL past the share's end.
The URLs are signed by botocore exactly as in a real query (no request is sent)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from cairn.dataaccess.delta_sharing import DeltaSharing


def _ends(url: str) -> float:
    q = parse_qs(urlparse(url).query)
    signed = datetime.strptime(q["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()
    return signed + int(q["X-Amz-Expires"][0])


def test_urls_signed_after_slow_work_still_end_with_the_share():
    ds = object.__new__(DeltaSharing)
    ds.store_url = "http://127.0.0.1:9"
    table = SimpleNamespace(io=SimpleNamespace(properties={
        "s3.access-key-id": "AK", "s3.secret-access-key": "SK", "s3.session-token": "ST", "s3.region": "us-east-1"}))
    deadline = time.time() + 20                         # the share ends in 20 seconds
    presign = ds._signer(table, deadline)
    time.sleep(1.3)                                     # the catalog load and file planning take a while
    for path in ("s3://wh/analytics/products/data/a.parquet", "s3://wh/analytics/products/data/b.parquet"):
        url, expires_ms = presign(path)
        assert _ends(url) <= deadline, (_ends(url), deadline)
        assert _ends(url) * 1000 <= expires_ms <= deadline * 1000      # what the client is told: not before, not after


def test_urls_end_with_the_vended_keys_when_those_end_first():
    ds = object.__new__(DeltaSharing)
    ds.store_url = "http://127.0.0.1:9"
    keys_end = time.time() + 10
    table = SimpleNamespace(io=SimpleNamespace(properties={
        "s3.access-key-id": "AK", "s3.secret-access-key": "SK", "s3.session-token": "ST",
        "s3.session-token-expires-at-ms": str(int(keys_end * 1000))}))
    url, expires_ms = ds._signer(table, time.time() + 900)("s3://wh/t/data/a.parquet")
    assert _ends(url) <= keys_end and expires_ms <= keys_end * 1000
