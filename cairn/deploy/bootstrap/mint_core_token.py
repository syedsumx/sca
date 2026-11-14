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

Token metadata (id, issued/expiry time) is kept next to the token in ``<out>.json``. On Kubernetes,
``--k8s-secret NAME`` stores both in that Secret instead (keys ``core_token`` and ``core_token.json``)
using the pod's service account; pods mount the Secret, so a rotated token reaches them as a file.
Standard library only so it runs in any python image.
"""

from __future__ import annotations

import argparse
import hashlib
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
POLICY_PREFIX = "urn:li:dataHubPolicy:cairn-service-"

# User provisioning (SCIM) only: manages people and groups, nothing else. Kept apart from the
# control-plane account so that account can never change group membership (and so its own access).
PROVISIONER_USER = "urn:li:corpuser:cairn-provisioner"
PROVISIONER_PLATFORM_PRIVILEGES = ["MANAGE_USERS_AND_GROUPS", "GENERATE_PERSONAL_ACCESS_TOKENS"]
PROVISIONER_METADATA_PRIVILEGES = ["VIEW_ENTITY_PAGE", "GET_ENTITY_PRIVILEGE", "SEARCH_PRIVILEGE", "EDIT_ENTITY"]


class Account:
    def __init__(self, key: str, user: str, display: str, platform: list[str], metadata: list[str],
                 resource_types: list[str] | None, ready_privilege: str, token_name: str):
        self.key, self.user, self.display = key, user, display
        self.platform, self.metadata, self.resource_types = platform, metadata, resource_types
        self.ready_privilege = ready_privilege    # platformPrivileges field that shows the grant applies
        self.token_name = token_name              # file name / Secret key
        self.policy_prefix = f"urn:li:dataHubPolicy:{user.rsplit(':', 1)[-1]}-"


SERVICE = Account("service", SERVICE_USER, "Cairn service", PLATFORM_PRIVILEGES, METADATA_PRIVILEGES, None,
                  "manageIngestion", "core_token")
PROVISIONER = Account("provisioner", PROVISIONER_USER, "Cairn provisioning", PROVISIONER_PLATFORM_PRIVILEGES,
                      PROVISIONER_METADATA_PRIVILEGES, ["corpuser", "corpGroup"], "manageIdentities",
                      "provisioner_token")
ACCOUNTS = {a.key: a for a in (SERVICE, PROVISIONER)}

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
ME = "{ me { corpUser { urn } platformPrivileges { manageIngestion manageIdentities manageGlossaries manageTags } } }"


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


class FileStore:
    def __init__(self, out: str):
        self.out = out

    def read(self) -> tuple[str | None, dict]:
        return read_state(self.out)

    def write(self, token: str, meta: dict) -> None:
        write_state(self.out, token, meta)

    def __str__(self) -> str:
        return self.out


class SecretStore:
    """A Kubernetes Secret, through the API with the pod's service account (needs get + patch on it)."""

    SA = "/var/run/secrets/kubernetes.io/serviceaccount"

    def __init__(self, name: str, namespace: str | None = None, key: str = "core_token"):
        self.name = name
        self.key = key
        self.api = os.environ.get("CAIRN_K8S_API") or (
            f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}")
        token_file = os.environ.get("CAIRN_K8S_TOKEN_FILE", f"{self.SA}/token")
        self.token_file = token_file
        if namespace is None:
            with open(os.environ.get("CAIRN_K8S_NAMESPACE_FILE", f"{self.SA}/namespace")) as f:
                namespace = f.read().strip()
        self.namespace = namespace
        ctx = None
        if self.api.startswith("https://"):
            import ssl

            ctx = ssl.create_default_context(cafile=os.environ.get("CAIRN_K8S_CA_FILE", f"{self.SA}/ca.crt"))
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx)) if ctx else None

    def _call(self, method: str, payload: dict | None = None, content_type: str = "application/json") -> dict | None:
        with open(self.token_file) as f:
            bearer = f.read().strip()
        url = f"{self.api}/api/v1/namespaces/{self.namespace}/secrets/{self.name}"
        try:
            return _request(url, payload, {"Authorization": f"Bearer {bearer}", "Content-Type": content_type},
                            opener=self.opener, method=method)
        except urllib.error.HTTPError as e:
            if e.code == 404 and method == "GET":
                return None
            raise RuntimeError(f"kubernetes {method} secret {self.name}: HTTP {e.code} {e.read()[:300]!r}")

    def read(self) -> tuple[str | None, dict]:
        import base64

        data = ((self._call("GET") or {}).get("data")) or {}
        k = self.key
        token = base64.b64decode(data[k]).decode().strip() if data.get(k) else None
        meta = json.loads(base64.b64decode(data[f"{k}.json"])) if data.get(f"{k}.json") else {}
        return token or None, meta

    def write(self, token: str, meta: dict) -> None:
        import base64

        b64 = lambda s: base64.b64encode(s.encode()).decode()  # noqa: E731
        self._call("PATCH", {"data": {self.key: b64(token), f"{self.key}.json": b64(json.dumps(meta))}},
                   content_type="application/merge-patch+json")

    def __str__(self) -> str:
        return f"secret/{self.namespace}/{self.name}"


def make_store(args, account: Account = SERVICE) -> "FileStore | SecretStore":
    if account is PROVISIONER and getattr(args, "k8s_provisioner_secret", None):
        return SecretStore(args.k8s_provisioner_secret, key=account.token_name)
    if getattr(args, "k8s_secret", None):
        return SecretStore(args.k8s_secret, key=account.token_name)
    if account is SERVICE:
        return FileStore(args.out)
    return FileStore(getattr(args, "provisioner_out", None)
                     or os.path.join(os.path.dirname(args.out) or ".", account.token_name))


def token_meta(token_id: str, duration: str, previous: list | None = None, account: Account | None = None) -> dict:
    now = time.time()
    return {"id": token_id, "actor": (account or SERVICE).user, "issued_at": now,
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


def desired_policies(account: Account | None = None) -> dict[str, dict]:
    """The account's policies, keyed by a URN that hashes their content.

    The engine refuses edits to non-editable policies, so a changed privilege set is a new policy
    (new URN) and the old one is deleted; an unchanged one is left alone.
    """
    account = account or SERVICE
    out = {}
    for kind, privileges in (("PLATFORM", account.platform), ("METADATA", account.metadata)):
        policy = {
            "displayName": f"{account.display}: {kind.lower()}", "type": kind, "state": "ACTIVE", "editable": False,
            "description": f"Least-privilege grant for {account.user.rsplit(':', 1)[-1]}. Managed by Cairn.",
            "privileges": sorted(privileges),
            "actors": {"users": [account.user], "allUsers": False, "allGroups": False, "resourceOwners": False},
        }
        if account is SERVICE:   # unchanged wording: existing installs keep their policy URNs
            policy["displayName"] = f"Cairn service: {kind.lower()}"
            policy["description"] = "Least-privilege grant for the Cairn control plane. Managed by Cairn."
        if kind == "METADATA":
            policy["resources"] = {"allResources": True} if not account.resource_types else {
                "allResources": False, "filter": {"criteria": [
                    {"field": "TYPE", "values": account.resource_types, "condition": "EQUALS"}]}}
        digest = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()[:10]
        out[f"{account.policy_prefix}{kind.lower()}-{digest}"] = policy
    return out


LIST_POLICIES = """query($start: Int!) { listPolicies(input: { start: $start, count: 100, query: "*" }) {
  total policies { urn } } }"""


def existing_policies(core_url: str, token: str, prefix: str = POLICY_PREFIX) -> set[str]:
    found, start = set(), 0
    while True:
        page = gql(core_url, token, LIST_POLICIES, {"start": start})["listPolicies"]
        found |= {p["urn"] for p in page["policies"] if p["urn"].startswith(prefix)}
        start += 100
        if start >= page["total"]:
            return found


def ensure_service_account(core_url: str, token: str, account: Account | None = None) -> None:
    account = account or SERVICE
    name = account.user.rsplit(":", 1)[-1]
    ingest(core_url, token, "corpuser", account.user, "corpUserInfo", {
        "active": True, "displayName": account.display, "title": "Service account",
        "email": f"{name}@localhost", "system": True})
    desired = desired_policies(account)
    present = existing_policies(core_url, token, account.policy_prefix)
    now = int(time.time() * 1000)
    for urn, policy in desired.items():          # create first: never a moment without the grant
        if urn not in present:
            ingest(core_url, token, "dataHubPolicy", urn, "dataHubPolicyInfo", {**policy, "lastUpdatedTimestamp": now})
            log(f"created policy {urn}")
    for urn in present - set(desired):
        gql(core_url, token, "mutation($urn: String!) { deletePolicy(urn: $urn) }", {"urn": urn})
        log(f"deleted superseded policy {urn}")


def wait_effective(core_url: str, token: str, timeout: float = 300, account: Account | None = None) -> None:
    """Policies reach the authorizer through a periodic cache refresh; wait until they apply."""
    account = account or SERVICE
    deadline = time.time() + timeout
    while time.time() < deadline:
        me = whoami(core_url, token)
        if me and me["corpUser"]["urn"] == account.user and me["platformPrivileges"][account.ready_privilege]:
            return
        time.sleep(5)
    raise RuntimeError("service account policies did not take effect in time")


def bootstrap(args, account: Account | None = None) -> None:
    account = account or SERVICE
    name = account.user.rsplit(":", 1)[-1]
    store = make_store(args, account)
    token, meta = store.read()
    if token and meta.get("actor") == account.user and meta.get("expires_at", 0) - time.time() > 2 * 86400:
        me = whoami(args.core_url, token)
        if me and me["corpUser"]["urn"] == account.user:
            log(f"existing {name} token in {store} is valid; nothing to do")
            return
    if not args.password:
        raise SystemExit("CAIRN_ADMIN_PASSWORD (or --password) is required to bootstrap the service account")
    admin, admin_id = admin_token(args.frontend_url, args.password)
    seed_id = None
    try:
        ensure_service_account(args.core_url, admin, account)
        # A short-lived seed token lets the account mint its own long-lived token: tokens are owned by
        # whoever mints them, and only owned tokens can later be revoked by the keeper.
        seed, seed_id = mint(args.core_url, admin, account.user, "ONE_DAY", f"{name} seed",
                             "Bootstrap seed (revoked immediately)")
        wait_effective(args.core_url, seed, account=account)
        new, new_id = mint(args.core_url, seed, account.user, args.duration, f"{name} {time.strftime('%Y-%m-%d')}",
                           f"{account.display} (rotated automatically)")
        store.write(new, token_meta(new_id, args.duration, account=account))
        log(f"service token for {account.user} written to {store}")
    finally:
        for tid in filter(None, (seed_id, admin_id)):
            try:
                revoke(args.core_url, admin, tid)
            except Exception as e:  # noqa: BLE001 - both expire on their own (one day / one hour)
                log(f"could not revoke bootstrap token {tid}: {e}")


# ---------------------------------------------------------------------------------------- keeper
def rotate_once(args, now: float | None = None, account: Account | None = None) -> bool:
    """Rotate when due; revoke retired tokens past their grace period. Returns True if rotated."""
    account = account or SERVICE
    name = account.user.rsplit(":", 1)[-1]
    now = now or time.time()
    store = make_store(args, account)
    token, meta = store.read()
    if not token or meta.get("actor") != account.user:
        log(f"no {name} token yet; waiting for bootstrap")
        return False
    retired = meta.get("retired", [])
    keep = []
    lifetime = DURATIONS_DAYS[args.duration] * 86400
    for r in retired:
        if now - r["retired_at"] >= lifetime:
            log(f"retired token {r['id']} has expired; dropping it")
            continue
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
        new, new_id = mint(args.core_url, token, account.user, args.duration,
                           f"{name} {time.strftime('%Y-%m-%d')}", f"{account.display} (rotated automatically)")
        keep.append({"id": meta["id"], "retired_at": now})
        meta = token_meta(new_id, args.duration, keep, account)
        store.write(new, meta)
        log(f"rotated {name} token; previous one is revoked in {args.grace_minutes} min")
        rotated = True
    elif keep != retired:
        meta["retired"] = keep
        store.write(token, meta)
    return rotated


def _secret(name: str) -> str | None:
    """``NAME`` or the content of the file named by ``NAME_FILE`` (Docker/Kubernetes secrets)."""
    path = os.environ.get(f"{name}_FILE")
    if path:
        with open(path) as f:
            return f.read().rstrip("\r\n")
    return os.environ.get(name)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--frontend-url", default=os.environ.get("CAIRN_FRONTEND_URL", "http://cairn-web:9002"))
    p.add_argument("--core-url", default=os.environ.get("CAIRN_CORE_URL", "http://cairn-core:8080"))
    p.add_argument("--password", default=_secret("CAIRN_ADMIN_PASSWORD"))
    p.add_argument("--out", default="/run/cairn/core_token")
    p.add_argument("--provisioner-out", default=os.environ.get("CAIRN_PROVISIONER_TOKEN_OUT"),
                   help="where the provisioning (SCIM) account's token goes (default: next to --out)")
    p.add_argument("--k8s-secret", default=os.environ.get("CAIRN_TOKEN_SECRET"),
                   help="store the token in this Kubernetes Secret instead of --out")
    p.add_argument("--k8s-provisioner-secret", default=os.environ.get("CAIRN_PROVISIONER_TOKEN_SECRET"),
                   help="Kubernetes Secret for the provisioning account's token (default: --k8s-secret)")
    p.add_argument("--duration", default=os.environ.get("CAIRN_CORE_TOKEN_DURATION", "ONE_MONTH"),
                   choices=sorted(DURATIONS_DAYS))
    p.add_argument("--keep", action="store_true", help="run as the rotation keeper")
    p.add_argument("--once", action="store_true", help="with --keep: one rotation check, then exit")
    p.add_argument("--print-admin-token", action="store_true",
                   help="print a one-hour admin token (operators, tests) and exit")
    p.add_argument("--rotate-days", type=float, default=float(os.environ.get("CAIRN_TOKEN_ROTATE_DAYS", "7")))
    p.add_argument("--grace-minutes", type=float, default=float(os.environ.get("CAIRN_TOKEN_GRACE_MINUTES", "15")))
    p.add_argument("--check-minutes", type=float, default=float(os.environ.get("CAIRN_TOKEN_CHECK_MINUTES", "30")))
    p.add_argument("--retries", type=int, default=int(os.environ.get("CAIRN_BOOTSTRAP_RETRIES", "60")))
    p.add_argument("--accounts", default=os.environ.get("CAIRN_TOKEN_ACCOUNTS", "service,provisioner"),
                   help="comma-separated: service (control plane), provisioner (SCIM)")
    args = p.parse_args()
    try:
        accounts = [ACCOUNTS[a.strip()] for a in args.accounts.split(",") if a.strip()]
    except KeyError as e:
        p.error(f"unknown account {e}; choose from {', '.join(ACCOUNTS)}")
    if args.rotate_days >= DURATIONS_DAYS[args.duration]:
        p.error("--rotate-days must be shorter than the token lifetime (--duration)")

    if args.print_admin_token:
        print(admin_token(args.frontend_url, args.password)[0])
        return 0
    if args.keep:
        while True:
            for account in accounts:
                try:
                    rotate_once(args, account=account)
                except Exception as e:  # noqa: BLE001 - keep running; next cycle retries
                    log(f"rotation check for {account.key} failed: {e}")
            if args.once:
                return 0
            time.sleep(args.check_minutes * 60)

    last_err: Exception | None = None
    pending = list(accounts)
    for attempt in range(1, args.retries + 1):
        try:
            while pending:
                bootstrap(args, pending[0])
                pending.pop(0)
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
