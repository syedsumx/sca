#!/usr/bin/env python3
"""Container health check without curl: ``aegis-healthcheck http://localhost:8800/health``."""

import sys
import urllib.request

url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8800/health"
try:
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 - fixed local URL
        sys.exit(0 if resp.status == 200 else 1)
except Exception as exc:  # any failure = unhealthy
    print(f"unhealthy: {exc}", file=sys.stderr)
    sys.exit(1)
