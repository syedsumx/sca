"""The edge: HTTPS with HSTS when a certificate is mounted, and per-client rate limits."""

from __future__ import annotations

import concurrent.futures
import os

import httpx
import pytest

from test_stack_smoke import EDGE

TLS_URL = os.environ.get("CAIRN_E2E_TLS_URL", "")
TLS_CA = os.environ.get("CAIRN_E2E_TLS_CA", "")


@pytest.mark.skipif(not TLS_URL, reason="edge TLS not enabled for this run")
def test_https_serves_the_product_with_hsts():
    with httpx.Client(verify=TLS_CA, timeout=30) as c:
        r = c.get(f"{TLS_URL}/login")
        assert r.status_code == 200
        assert r.headers["strict-transport-security"].startswith("max-age=31536000")
        assert c.get(f"{TLS_URL}/cairn/api/health").json()["status"] == "ok"
        assert c.get(f"{TLS_URL}/docs/").status_code == 200
        # Framable embed route keeps HSTS even though it sets its own headers.
        assert "strict-transport-security" in c.get(f"{TLS_URL}/cairn-ui/remoteEntry.js").headers
    # Plain HTTP stays available (no redirect configured) and never sends HSTS.
    r = httpx.get(f"{EDGE}/login", timeout=30)
    assert r.status_code == 200 and "strict-transport-security" not in r.headers
    # Old protocol versions are refused.
    import ssl

    ctx = ssl.create_default_context(cafile=TLS_CA)
    ctx.maximum_version = ssl.TLSVersion.TLSv1_1
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    with pytest.raises((httpx.ConnectError, ssl.SSLError)):
        httpx.get(f"{TLS_URL}/login", verify=ctx, timeout=10)


def test_password_sign_in_is_rate_limited():
    statuses = [httpx.post(f"{EDGE}/logIn", json={"username": "admin", "password": "not-the-password"},
                           timeout=30).status_code for _ in range(30)]
    assert 429 in statuses, statuses
    assert statuses[0] != 429                                     # a burst is allowed first


def test_api_is_rate_limited_per_client():
    """50 requests/s with bursts of 400: a client going faster than that gets 429s."""
    limits = httpx.Limits(max_connections=64, max_keepalive_connections=64)
    statuses: list[int] = []
    with httpx.Client(timeout=30, limits=limits) as client, concurrent.futures.ThreadPoolExecutor(64) as pool:
        def hit(_):
            return client.get(f"{EDGE}/cairn/api/__rate-probe").status_code   # 404 from the API: cheap

        while 429 not in statuses and len(statuses) < 8000:
            statuses += list(pool.map(hit, range(1000)))
    assert 429 in statuses, {s: statuses.count(s) for s in set(statuses)}
    assert statuses.count(404) >= 400                              # the burst is served
