#!/usr/bin/env python3
"""One-shot bootstrap: mint a DataHub personal access token for the DHE services.

Logs in to datahub-frontend with the bootstrap admin credentials, calls the
``createAccessToken`` GraphQL mutation and writes the token to ``--out`` (mode 0600).
Idempotent: if the file already holds a token that GMS accepts, nothing is minted.

Standard library only so it runs in any python image.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request

MUTATION = """
mutation createAccessToken($input: CreateAccessTokenInput!) {
  createAccessToken(input: $input) { accessToken metadata { id } }
}
"""


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )


def _post(opener, url: str, payload: dict, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with opener.open(req, timeout=30) as resp:
        body = resp.read().decode() or "{}"
        return json.loads(body)


def token_is_valid(gms_url: str, token: str) -> bool:
    req = urllib.request.Request(
        f"{gms_url}/api/graphql",
        data=json.dumps({"query": "{ me { corpUser { urn } } }"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return bool(data.get("data", {}).get("me"))
    except (urllib.error.URLError, ValueError):
        return False


def mint(frontend_url: str, username: str, password: str, actor: str, name: str) -> str:
    opener = _opener()
    _post(opener, f"{frontend_url}/logIn", {"username": username, "password": password})
    result = _post(
        opener,
        f"{frontend_url}/api/v2/graphql",
        {
            "query": MUTATION,
            "variables": {
                "input": {
                    "type": "PERSONAL",
                    "actorUrn": actor,
                    "duration": "NO_EXPIRY",
                    "name": name,
                    "description": "Service token for the DHE control plane (bootstrap)",
                }
            },
        },
    )
    if result.get("errors"):
        raise RuntimeError(f"createAccessToken failed: {result['errors']}")
    return result["data"]["createAccessToken"]["accessToken"]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--frontend-url", default=os.environ.get("DHE_FRONTEND_URL", "http://datahub-frontend-react:9002"))
    p.add_argument("--gms-url", default=os.environ.get("DHE_GMS_URL", "http://datahub-gms:8080"))
    p.add_argument("--username", default=os.environ.get("DATAHUB_BOOTSTRAP_USER", "datahub"))
    p.add_argument("--password", default=os.environ.get("DATAHUB_BOOTSTRAP_PASSWORD", "datahub"))
    p.add_argument("--actor", default="urn:li:corpuser:datahub")
    p.add_argument("--name", default="dhe-service")
    p.add_argument("--out", default="/run/dhe/gms_token")
    p.add_argument("--retries", type=int, default=60)
    args = p.parse_args()

    if os.path.exists(args.out):
        with open(args.out) as f:
            existing = f.read().strip()
        if existing and token_is_valid(args.gms_url, existing):
            print(f"[bootstrap] existing token in {args.out} is valid; nothing to do")
            return 0

    last_err: Exception | None = None
    for attempt in range(1, args.retries + 1):
        try:
            token = mint(args.frontend_url, args.username, args.password, args.actor, args.name)
            os.makedirs(os.path.dirname(args.out), exist_ok=True)
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token)
            os.chmod(args.out, 0o644)  # readable by the non-root DHE uid on the shared volume
            print(f"[bootstrap] minted token '{args.name}' for {args.actor} → {args.out}")
            return 0
        except Exception as e:  # noqa: BLE001 - retry anything while the stack warms up
            last_err = e
            print(f"[bootstrap] attempt {attempt}/{args.retries} failed: {e}", file=sys.stderr)
            time.sleep(5)
    print(f"[bootstrap] giving up: {last_err}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
