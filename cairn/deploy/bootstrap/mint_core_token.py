#!/usr/bin/env python3
"""Service account for the Cairn control plane: bootstrap, and token rotation.

The control plane never acts as root. It runs as ``urn:li:corpuser:cairn-service``, which holds only
the privileges in :data:`PLATFORM_PRIVILEGES` and :data:`METADATA_PRIVILEGES` (two non-editable
policies managed here). Policy, user, credential, secret and token administration, entity deletion
and system operations are deliberately absent.

Bootstrap (one-shot, idempotent)::

    mint_core_token.py --out /run/cairn/core_token

  Signs in to cairn-web as ``admin`` (``CAIRN_ADMIN_PASSWORD``) and mints a one-hour admin token,
  upserts the service account and its policies, mints the service token, waits until the policies
  are in effect, writes the token (mode 0644, atomic rename) and revokes the admin token. Skipped
  when the file already holds a valid, non-expiring-soon token of the service account.

Keeper (long-running)::

    mint_core_token.py --out /run/cairn/core_token --keep --rotate-days 7

  Every ``--check-minutes`` it checks the token's age; once it reaches ``--rotate-days`` the service
  account mints its own successor (``GENERATE_PERSONAL_ACCESS_TOKENS``), the file is replaced, and the
  old token is revoked after ``--grace-minutes``. Needs no admin credentials.

Token metadata (id, issued/expiry time) is kept next to the token in ``<out>.json``.
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

SERVICE_USER = "urn:li:corpuser:cairn-service"
ADMIN_USER = "admin"

PLATFORM_PRIVILEGES = [
    "GENERATE_PERSONAL_ACCESS_TOKENS",   # rotation: the account mints its own successor
    "MANAGE_INGESTION",                  # remote-agent dispatcher: execution requests
    "MANAGE_GLOSSARIES",                 # PII classification terms, context-agent terms
    "MANAGE_TAGS",
    "MANAGE_DOMAINS",
    "MANAGE_STRUCTURED_PROPERTIES",      # certification / quality properties
    "VIEW_STRUCTURED_PROPERTIES_PAGE",
    "MANAGE_DOCUMENTATION_FORMS",
    "VIEW_ANALYTICS",
    "VIEW_ALL_QUERIES",                  # context miner reads query history
]
METADATA_PRIVILEGES = [
    "VIEW_ENTITY_PAGE", "GET_ENTITY_PRIVILEGE", "SEARCH_PRIVILEGE", "GET_COUNTS_PRIVILEGE",
    "GET_TIMELINE_PRIVILEGE", "GET_TIMESERIES_ASPECT_PRIVILEGE", "VIEW_ENTITY_QUERIES",
    "VIEW_DATASET_USAGE", "VIEW_DATASET_PROFILE", "VIEW_DATASET_OPERATIONS",
    "EDIT_ENTITY", "EDIT_LINEAGE", "EDIT_ENTITY_ASSERTIONS", "EXECUTE_ENTITY",
    "MANAGE_DATA_PRODUCTS", "PRODUCE_PLATFORM_EVENT_PRIVILEGE",
]
POLICIES = {
    "urn:li:dataHubPolicy:cairn-service-platform": ("PLATFORM", "Cairn service: platform"),
    "urn:li:dataHubPolicy:cairn-service-metadata": ("METADATA", "Cairn service: metadata"),
}

DURATIONS_DAYS = {"ONE_DAY": 1, "ONE_WEEK": 7, "ONE_MONTH": 30, "THREE_MONTHS": 90, "SIX_MONTHS": 180}


def log(msg: str) -> None:
    print(f"[cairn-token] {msg}", flush=True)


# ---------------------------------------------------------------------------------------- HTTP
def _request(url: str, payload: dict | None, headers: dict | None = None, opener=None, method="POST") -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with (opener.open if opener else urllib.request.urlopen)(req, timeout=30) as resp:
        body = resp.read().decode() or "{}"
        return json.loads(body)


def gql(core_url: str, token: str, query: str, variables: dict | None = None) -> dict:
    out = _request(f"{core_url}/api/graphql", {"query": query, "variables": variables or {}},
                   {"Authorization": f"Bearer {token}"})
    if out.get("errors"):
        raise RuntimeError(f"graphql: {out['errors']}")
    return out["data"]


def ingest(core_url: str, token: str, entity_type: str, urn: str, aspect_name: str, aspect: dict) -> None:
    proposal = {"entityType": entity_type, "entityUrn": urn, "changeType": "UPSERT", "aspectName": aspect_name,
                "aspect": {"value": json.dumps(aspect), "contentType": "application/json"}}
    _request(f"{core_url}/aspects?action=ingestProposal", {"proposal": proposal, "async": "false"},
             {"Authorization": f"Bearer {token}", "X-RestLi-Protocol-Version": "2.0.0"})


MINT = """mutation($input: CreateAccessTokenInput!) {
  createAccessToken(input: $input) { accessToken metadata { id } } }"""
REVOKE = "mutation($id: String!) { revokeAccessToken(tokenId: $id) }"
ME = "{ me { corpUser { urn } platformPrivileges { manageIngestion manageGlossaries manageTags } } }"


def mint(core_url: str, token: str, actor: str, duration: str, name: str, description: str) -> tuple[str, str]:
    data = gql(core_url, token, MINT, {"input": {
        "type": "PERSONAL", "actorUrn": actor, "duration": duration, "name": name, "description": description}})
    return data["createAccessToken"]["accessToken"], data["createAccessToken"]["metadata"]["id"]


def revoke(core_url: str, token: str, token_id: str) -> None:
    gql(core_url, token, REVOKE, {"id": token_id})


def whoami(core_url: str, token: str) -> dict | None:
    try:
        return gql(core_url, token, ME)["me"]
    except (urllib.error.URLError, RuntimeError, ValueError, KeyError):
        return None


# ---------------------------------------------------------------------------------------- state
def read_state(out: str) -> tuple[str | None, dict]:
    token, meta = None, {}
    if os.path.exists(out):
        with open(out) as f:
            token = f.read().strip() or None
    if os.path.exists(out + ".json"):
        with open(out + ".json") as f:
            meta = json.load(f)
    return token, meta


def write_state(out: str, token: str, meta: dict) -> None:
    os.makedirs(os.path.dirname(out), exist_ok=True)
    for path, body in ((out + ".json", json.dumps(meta)), (out, token)):
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            f.write(body)
        os.chmod(tmp, 0o644)  # readable by the non-root Cairn uid on the shared volume
        os.replace(tmp, path)


def token_meta(token_id: str, duration: str, previous: list | None = None) -> dict:
    now = time.time()
    return {"id": token_id, "actor": SERVICE_USER, "issued_at": now,
            "expires_at": now + DURATIONS_DAYS[duration] * 86400, "retired": previous or []}


# ---------------------------------------------------------------------------------------- bootstrap
def admin_token(frontend_url: str, password: str) -> tuple[str, str]:
    """A one-hour admin token, minted through a web sign-in (the only place the password is used)."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    _request(f"{frontend_url}/logIn", {"username": ADMIN_USER, "password": password}, opener=opener)
    out = _request(f"{frontend_url}/api/v2/graphql", {"query": MINT, "variables": {"input": {
        "type": "PERSONAL", "actorUrn": f"urn:li:corpuser:{ADMIN_USER}", "duration": "ONE_HOUR",
        "name": "cairn-bootstrap", "description": "Short-lived token used by the Cairn bootstrap job"}}},
        opener=opener)
    if out.get("errors"):
        raise RuntimeError(f"admin token: {out['errors']}")
    return out["data"]["createAccessToken"]["accessToken"], out["data"]["createAccessToken"]["metadata"]["id"]


def ensure_service_account(core_url: str, token: str) -> None:
    ingest(core_url, token, "corpuser", SERVICE_USER, "corpUserInfo", {
        "active": True, "displayName": "Cairn service", "title": "Service account",
        "email": "cairn-service@localhost", "system": True})
    now = int(time.time() * 1000)
    for urn, (kind, name) in POLICIES.items():
        privileges = PLATFORM_PRIVILEGES if kind == "PLATFORM" else METADATA_PRIVILEGES
        policy = {
            "displayName": name, "type": kind, "state": "ACTIVE", "editable": False,
            "description": "Least-privilege grant for the Cairn control plane. Managed by Cairn; "
                           "changes are overwritten at the next bootstrap.",
            "privileges": privileges, "lastUpdatedTimestamp": now,
            "actors": {"users": [SERVICE_USER], "allUsers": False, "allGroups": False, "resourceOwners": False},
        }
        if kind == "METADATA":
            policy["resources"] = {"allResources": True}
        ingest(core_url, token, "dataHubPolicy", urn, "dataHubPolicyInfo", policy)


def wait_effective(core_url: str, token: str, timeout: float = 300) -> None:
    """Policies reach the authorizer through a periodic cache refresh; wait until they apply."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        me = whoami(core_url, token)
        if me and me["corpUser"]["urn"] == SERVICE_USER and me["platformPrivileges"]["manageIngestion"]:
            return
        time.sleep(5)
    raise RuntimeError("service account policies did not take effect in time")


def bootstrap(args) -> None:
    token, meta = read_state(args.out)
    if token and meta.get("actor") == SERVICE_USER and meta.get("expires_at", 0) - time.time() > 2 * 86400:
        me = whoami(args.core_url, token)
        if me and me["corpUser"]["urn"] == SERVICE_USER:
            log(f"existing service token in {args.out} is valid; nothing to do")
            return
    if not args.password:
        raise SystemExit("CAIRN_ADMIN_PASSWORD (or --password) is required to bootstrap the service account")
    admin, admin_id = admin_token(args.frontend_url, args.password)
    try:
        ensure_service_account(args.core_url, admin)
        new, new_id = mint(args.core_url, admin, SERVICE_USER, args.duration, f"cairn-service {time.strftime('%Y-%m-%d')}",
                           "Cairn control plane (rotated automatically)")
        wait_effective(args.core_url, new)
        write_state(args.out, new, token_meta(new_id, args.duration))
        log(f"service token for {SERVICE_USER} written to {args.out}")
    finally:
        try:
            revoke(args.core_url, admin, admin_id)
        except Exception as e:  # noqa: BLE001 - the admin token also expires within the hour
            log(f"could not revoke bootstrap admin token: {e}")


# ---------------------------------------------------------------------------------------- keeper
def rotate_once(args, now: float | None = None) -> bool:
    """Rotate when due; revoke retired tokens past their grace period. Returns True if rotated."""
    now = now or time.time()
    token, meta = read_state(args.out)
    if not token or meta.get("actor") != SERVICE_USER:
        log("no service token yet; waiting for bootstrap")
        return False
    retired = meta.get("retired", [])
    keep = []
    for r in retired:
        if now - r["retired_at"] >= args.grace_minutes * 60:
            try:
                revoke(args.core_url, token, r["id"])
                log(f"revoked retired token {r['id']}")
            except Exception as e:  # noqa: BLE001
                log(f"revoke {r['id']} failed ({e}); retrying next cycle")
                keep.append(r)
        else:
            keep.append(r)
    rotated = False
    if now - meta.get("issued_at", 0) >= args.rotate_days * 86400:
        new, new_id = mint(args.core_url, token, SERVICE_USER, args.duration,
                           f"cairn-service {time.strftime('%Y-%m-%d')}", "Cairn control plane (rotated automatically)")
        keep.append({"id": meta["id"], "retired_at": now})
        meta = token_meta(new_id, args.duration, keep)
        write_state(args.out, new, meta)
        log(f"rotated service token; previous one is revoked in {args.grace_minutes} min")
        rotated = True
    elif keep != retired:
        meta["retired"] = keep
        write_state(args.out, token, meta)
    return rotated


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--frontend-url", default=os.environ.get("CAIRN_FRONTEND_URL", "http://cairn-web:9002"))
    p.add_argument("--core-url", default=os.environ.get("CAIRN_CORE_URL", "http://cairn-core:8080"))
    p.add_argument("--password", default=os.environ.get("CAIRN_ADMIN_PASSWORD"))
    p.add_argument("--out", default="/run/cairn/core_token")
    p.add_argument("--duration", default=os.environ.get("CAIRN_CORE_TOKEN_DURATION", "ONE_MONTH"),
                   choices=sorted(DURATIONS_DAYS))
    p.add_argument("--keep", action="store_true", help="run as the rotation keeper")
    p.add_argument("--once", action="store_true", help="with --keep: one rotation check, then exit")
    p.add_argument("--print-admin-token", action="store_true",
                   help="print a one-hour admin token (operators, tests) and exit")
    p.add_argument("--rotate-days", type=float, default=float(os.environ.get("CAIRN_TOKEN_ROTATE_DAYS", "7")))
    p.add_argument("--grace-minutes", type=float, default=float(os.environ.get("CAIRN_TOKEN_GRACE_MINUTES", "15")))
    p.add_argument("--check-minutes", type=float, default=float(os.environ.get("CAIRN_TOKEN_CHECK_MINUTES", "30")))
    p.add_argument("--retries", type=int, default=60)
    args = p.parse_args()
    if args.rotate_days >= DURATIONS_DAYS[args.duration]:
        p.error("--rotate-days must be shorter than the token lifetime (--duration)")

    if args.print_admin_token:
        print(admin_token(args.frontend_url, args.password)[0])
        return 0
    if args.keep and args.once:
        rotate_once(args)
        return 0
    if args.keep:
        while True:
            try:
                rotate_once(args)
            except Exception as e:  # noqa: BLE001 - keep running; next cycle retries
                log(f"rotation check failed: {e}")
            time.sleep(args.check_minutes * 60)

    last_err: Exception | None = None
    for attempt in range(1, args.retries + 1):
        try:
            bootstrap(args)
            return 0
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001 - retry anything while the stack warms up
            last_err = e
            log(f"attempt {attempt}/{args.retries} failed: {e}")
            time.sleep(5)
    log(f"giving up: {last_err}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
