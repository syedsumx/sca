"""Storage credentials and external locations (L133): the customer's own storage, governed (L132).

* A **storage credential** says how to reach an Azure storage account (account key, an Entra service principal or
  workload identity), an S3 bucket (an IAM role assumed through STS) or a GCS bucket (a service account, downscoped).
  Its secret is only ever a reference, read where it is used (by Cairn Catalog): ``file:/path``,
  ``k8s:<secret>/<key>`` (a Kubernetes Secret mounted into the catalog), ``vault:<mount>/<path>#<field>``
  (HashiCorp Vault) or ``workload`` (the catalog's workload identity). Cairn never holds a secret value, and refuses
  configuration that looks like one.
* An **external location** is a URL prefix (``abfss://``, ``wasbs://``, ``s3://``, ``gs://``) and the credential for
  it. Locations may not overlap each other, nor Cairn's own storage (``CAIRN_STORAGE_RESERVED_URLS``, by default the
  lakehouse warehouse).
* People and groups are granted ``CREATE_TABLE``, ``READ_FILES`` and ``WRITE_FILES`` on a location
  (``urn:li:externalLocation:<name>``). Grants are policies in the policy decision point (a layer, ``storage``), so
  they are decided, cached, explained and audited like every other privilege, and administrators may also write
  their own policies on these resources.
* Cairn Catalog loads the registrations (``GET /v1/storage/catalog-config``) and asks before every table it creates
  and every credential it hands out (``POST /v1/storage/decide``); each answer is recorded in the audit log
  (``storage.create_table``, ``storage.vend``), allowed or refused. Creating anything outside a registered location
  is refused there.

Managing credentials and locations needs ``MANAGE_SYSTEM_OPERATIONS``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, String, Table, Text, and_, delete, insert, select, update

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

ADMIN = "MANAGE_SYSTEM_OPERATIONS"
PRIVILEGES = ("CREATE_TABLE", "READ_FILES", "WRITE_FILES")
PROVIDERS = ("azure", "s3", "gcs")
NAME = re.compile(r"^[a-z][a-z0-9_-]{1,62}[a-z0-9]$")
PRINCIPAL = re.compile(r"^urn:li:(corpuser|corpGroup):[^\s]+$")
REF = re.compile(r"^(file:/\S+|k8s:[a-z0-9][a-z0-9.-]*/[A-Za-z0-9._-]+|vault:[^\s#/]+/[^\s#]+#[^\s#]+|workload)$")
CONFIG_KEYS = {
    "azure": {"account", "endpoint", "hierarchical_namespace", "auth", "tenant_id", "client_id", "authority_host",
              "federated_token_file"},
    "s3": {"role_arn", "region", "endpoint", "sts_endpoint", "access_key_id", "external_id"},
    "gcs": {"project_id", "token_uri", "sts_url", "storage_url"},
}
AZURE_AUTH = ("account_key", "service_principal", "workload_identity")

storage_credentials = Table(
    "cairn_storage_credentials", metadata,
    Column("name", String(64), primary_key=True),
    Column("provider", String(8), nullable=False),
    Column("config", Text, nullable=False),               # JSON, never a secret
    Column("secret_ref", String(512), nullable=True),     # a reference to the secret, never its value
    Column("comment", Text, nullable=False),
    Column("owner", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)
external_locations = Table(
    "cairn_external_locations", metadata,
    Column("name", String(64), primary_key=True),
    Column("url", String(1024), nullable=False, unique=True),
    Column("credential", String(64), nullable=False, index=True),
    Column("read_only", Boolean, nullable=False),
    Column("comment", Text, nullable=False),
    Column("owner", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)
location_grants = Table(
    "cairn_external_location_grants", metadata,
    Column("location", String(64), primary_key=True),
    Column("principal", String(256), primary_key=True),
    Column("privilege", String(16), primary_key=True),
    Column("granted_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)


class StorageError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def urn(name: str) -> str:
    return f"urn:li:externalLocation:{name}"


# ---- URLs (the catalog parses them the same way) ---------------------------------------------------------------
_AZ = re.compile(r"^(abfss?|wasbs?)://([a-z0-9-]{3,63})@([a-z0-9]{1,24})\.(dfs|blob)\.([^/:]+)(/.*)?$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")


def _clean(path: str) -> str:
    segs = [s for s in (path or "").split("/") if s]
    if any(s in (".", "..") or any(ord(c) < 32 for c in s) for s in segs):
        raise StorageError("a location has no . or .. segments")
    return "/".join(segs)


def parse_url(url: str) -> dict:
    """``{kind, authority, path, account?, container?}``; refuses what is not a storage URL Cairn Catalog knows."""
    url = (url or "").strip()
    m = _AZ.match(url)
    if m:
        scheme, container, account, svc, endpoint, path = m.groups()
        if (scheme.startswith("abfs") and svc != "dfs") or (scheme.startswith("wasb") and svc != "blob"):
            raise StorageError(f"{url}: abfss:// names <account>.dfs.<endpoint>, wasbs:// <account>.blob.<endpoint>")
        if container.startswith("-") or container.endswith("-"):
            raise StorageError(f"{url}: not a valid container")
        return {"kind": "azure", "authority": f"{account}.{endpoint}/{container}", "path": _clean(path or ""),
                "account": account, "container": container}
    for prefix, kind in (("s3://", "s3"), ("s3a://", "s3"), ("s3n://", "s3"), ("gs://", "gcs")):
        if url.startswith(prefix):
            bucket, _, path = url[len(prefix):].partition("/")
            if not _BUCKET.match(bucket):
                raise StorageError(f"{url}: not a valid bucket")
            return {"kind": kind, "authority": bucket, "path": _clean(path)}
    raise StorageError(f"{url!r}: abfss://<container>@<account>.dfs.<endpoint>/…, wasbs://…, s3://<bucket>/… or gs://<bucket>/…")


def covers(a: dict, b: dict) -> bool:
    return (a["kind"] == b["kind"] and a["authority"] == b["authority"]
            and (not a["path"] or b["path"] == a["path"] or b["path"].startswith(a["path"] + "/")))


def overlaps(a: dict, b: dict) -> bool:
    return covers(a, b) or covers(b, a)


def _looks_secret(key: str, value: Any) -> bool:
    k = key.lower()
    if k in ("access_key_id", "client_id", "tenant_id", "federated_token_file"):
        return False
    return any(w in k for w in ("secret", "password", "key", "token", "credential", "sas", "private"))


class StorageLocations:
    def __init__(self, engine, pdp=None, authorize: Optional[Callable[[str, str, Optional[str]], bool]] = None,
                 audit=None, reserved: Optional[list[str]] = None):
        """``authorize(actor, privilege, resource_urn)``: the policy decision point, fail closed."""
        self.engine, self.pdp, self.authorize, self.audit = engine, pdp, authorize, audit
        if reserved is None:
            env = os.environ.get("CAIRN_STORAGE_RESERVED_URLS")
            reserved = [u.strip() for u in env.split(",") if u.strip()] if env is not None else [
                os.environ.get("CAIRN_LAKEHOUSE_WAREHOUSE", "s3://lakehouse/warehouse")]
        self.reserved = [parse_url(u) for u in reserved]
        self.reload()

    # ---- the policy layer ------------------------------------------------------------------------------------
    def reload(self) -> None:
        """Grants (and each location's owner) as allow policies in the decision point's ``storage`` layer."""
        if self.pdp is None:
            return
        from cairn.governance.abac import Policy, Target

        with self.engine.connect() as conn:
            owners = {r.name: r.owner for r in conn.execute(select(external_locations.c.name, external_locations.c.owner))}
            rows = list(conn.execute(select(location_grants)))
        subjects: dict[tuple[str, str], set[str]] = {}
        for name, owner in owners.items():
            for priv in PRIVILEGES:
                subjects.setdefault((name, priv), set()).add(owner)
        for r in rows:
            if r.location in owners:
                subjects.setdefault((r.location, r.privilege), set()).add(r.principal)
        policies = [Policy(id=f"storage:{name}:{priv}", description=f"External location {name}: {priv}",
                           effect="allow", actions=[priv], target=Target(resource_urns=[urn(name)], subjects=sorted(who)),
                           source="storage")
                    for (name, priv), who in sorted(subjects.items())]
        self.pdp.set_layer("storage", policies)
        self.pdp.invalidate_decisions()

    def _admin(self, actor: str) -> None:
        if self.authorize is None or not self.authorize(actor, ADMIN, None):
            raise StorageError(f"managing storage credentials and external locations needs {ADMIN}", 403)

    def _audit(self, action: str, actor: str, subject: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("storage", action, actor, subject, details)

    # ---- credentials -----------------------------------------------------------------------------------------
    def _check_credential(self, provider: str, config: dict, secret_ref: Optional[str]) -> dict:
        if provider not in PROVIDERS:
            raise StorageError(f"provider: one of {', '.join(PROVIDERS)}")
        if not isinstance(config, dict):
            raise StorageError("config: an object")
        for k, v in config.items():
            if _looks_secret(k, v):
                raise StorageError(f"config.{k}: secrets are never stored; give secret_ref (file:, k8s:, vault: or workload)")
        unknown = sorted(set(config) - CONFIG_KEYS[provider])
        if unknown:
            raise StorageError(f"config: unknown for {provider}: {', '.join(unknown)} (known: {', '.join(sorted(CONFIG_KEYS[provider]))})")
        if secret_ref is not None and not REF.match(secret_ref):
            raise StorageError("secret_ref: file:/path, k8s:<secret>/<key>, vault:<mount>/<path>#<field> or workload "
                               "(a reference, never the secret itself)")
        if provider == "azure":
            if not re.match(r"^[a-z0-9]{3,24}$", str(config.get("account", ""))):
                raise StorageError("config.account: the storage account name")
            auth = config.get("auth", "account_key")
            if auth not in AZURE_AUTH:
                raise StorageError(f"config.auth: one of {', '.join(AZURE_AUTH)}")
            if auth != "account_key" and not (config.get("tenant_id") and config.get("client_id")):
                raise StorageError(f"config: {auth} needs tenant_id and client_id")
            if auth != "workload_identity" and (not secret_ref or secret_ref == "workload"):
                raise StorageError(f"secret_ref: {auth} needs a reference to its secret")
            if "hierarchical_namespace" in config and not isinstance(config["hierarchical_namespace"], bool):
                raise StorageError("config.hierarchical_namespace: true or false")
        elif provider == "s3":
            if not str(config.get("role_arn", "")).startswith("arn:"):
                raise StorageError("config.role_arn: the IAM role the catalog assumes (arn:aws:iam::…:role/…)")
            if bool(config.get("access_key_id")) == (secret_ref == "workload") or not secret_ref:
                raise StorageError("config.access_key_id with a secret_ref to its secret key, or secret_ref workload alone")
        elif provider == "gcs" and not secret_ref:
            raise StorageError("secret_ref: a service account key's reference, or workload")
        return config

    def _cred_row(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(storage_credentials).where(storage_credentials.c.name == name)).first()
        if r is None:
            raise StorageError(f"no storage credential {name}", 404)
        return dict(r._mapping)

    @staticmethod
    def _cred_out(r: dict) -> dict:
        return {"name": r["name"], "provider": r["provider"], "config": json.loads(r["config"]),
                "secret_ref": r["secret_ref"], "comment": r["comment"], "owner": r["owner"],
                "created_ms": r["created_ms"], "updated_ms": r["updated_ms"]}

    def list_credentials(self, actor: str) -> list[dict]:
        self._admin(actor)
        with self.engine.connect() as conn:
            return [self._cred_out(dict(r._mapping))
                    for r in conn.execute(select(storage_credentials).order_by(storage_credentials.c.name))]

    def get_credential(self, actor: str, name: str) -> dict:
        self._admin(actor)
        return self._cred_out(self._cred_row(name))

    def create_credential(self, actor: str, name: str, provider: str, config: Optional[dict] = None,
                          secret_ref: Optional[str] = None, comment: str = "") -> dict:
        self._admin(actor)
        if not NAME.match(name or ""):
            raise StorageError("name: 3 to 64 lowercase letters, digits, - and _")
        config = self._check_credential(provider, dict(config or {}), secret_ref)
        now = _now()
        with self.engine.begin() as conn:
            if conn.execute(select(storage_credentials.c.name).where(storage_credentials.c.name == name)).first():
                raise StorageError(f"a storage credential {name} exists", 409)
            conn.execute(insert(storage_credentials).values(
                name=name, provider=provider, config=json.dumps(config, sort_keys=True), secret_ref=secret_ref,
                comment=str(comment or "")[:2000], owner=actor, created_ms=now, updated_ms=now))
        self._audit("storage.credential_created", actor, f"storageCredential:{name}",
                    {"provider": provider, "secret_ref": secret_ref})
        return self.get_credential(actor, name)

    def update_credential(self, actor: str, name: str, config: Optional[dict] = None, secret_ref: Optional[str] = None,
                          comment: Optional[str] = None) -> dict:
        self._admin(actor)
        cur = self._cred_row(name)
        new_cfg = dict(config) if config is not None else json.loads(cur["config"])
        new_ref = secret_ref if secret_ref is not None else cur["secret_ref"]
        self._check_credential(cur["provider"], new_cfg, new_ref)
        # Locations keep matching their credential (an Azure account cannot change under them).
        for loc in self._locations_of(name):
            self._check_pair(parse_url(loc["url"]), cur["provider"], new_cfg)
        with self.engine.begin() as conn:
            conn.execute(update(storage_credentials).where(storage_credentials.c.name == name).values(
                config=json.dumps(new_cfg, sort_keys=True), secret_ref=new_ref,
                comment=cur["comment"] if comment is None else str(comment)[:2000], updated_ms=_now()))
        self._audit("storage.credential_changed", actor, f"storageCredential:{name}", {"secret_ref": new_ref})
        return self.get_credential(actor, name)

    def delete_credential(self, actor: str, name: str) -> None:
        self._admin(actor)
        self._cred_row(name)
        used = self._locations_of(name)
        if used:
            raise StorageError(f"{name} is used by external location(s) {', '.join(r['name'] for r in used)}", 409)
        with self.engine.begin() as conn:
            conn.execute(delete(storage_credentials).where(storage_credentials.c.name == name))
        self._audit("storage.credential_deleted", actor, f"storageCredential:{name}", {})

    # ---- locations -------------------------------------------------------------------------------------------
    def _locations_of(self, credential: str) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(
                select(external_locations).where(external_locations.c.credential == credential))]

    def _loc_row(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(external_locations).where(external_locations.c.name == name)).first()
        if r is None:
            raise StorageError(f"no external location {name}", 404)
        return dict(r._mapping)

    @staticmethod
    def _check_pair(u: dict, provider: str, config: dict) -> None:
        if {"azure": "azure", "s3": "s3", "gcs": "gcs"}[provider] != u["kind"]:
            raise StorageError(f"the credential is for {provider}, the location is on {u['kind']}")
        if provider == "azure":
            if u["account"] != config.get("account"):
                raise StorageError(f"the credential is for storage account {config.get('account')}, the location is on {u['account']}")
            if config.get("hierarchical_namespace") is False and u["path"]:
                raise StorageError("without a hierarchical namespace a SAS token covers a whole container: "
                                   "the location must be a container (no path)")

    def grants(self, name: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = list(conn.execute(select(location_grants).where(location_grants.c.location == name)
                                     .order_by(location_grants.c.principal, location_grants.c.privilege)))
        out: dict[str, dict] = {}
        for r in rows:
            g = out.setdefault(r.principal, {"principal": r.principal, "privileges": [], "granted_by": r.granted_by,
                                             "created_ms": r.created_ms})
            g["privileges"].append(r.privilege)
        return list(out.values())

    def _may_see(self, actor: str, name: str) -> bool:
        if self.authorize is None:
            return False
        return any(self.authorize(actor, p, urn(name)) for p in (*PRIVILEGES, ADMIN))

    def _loc_out(self, r: dict, actor: str) -> dict:
        out = {k: r[k] for k in ("name", "url", "credential", "read_only", "comment", "owner", "created_ms", "updated_ms")}
        out["urn"] = urn(r["name"])
        if self.authorize is not None:
            out["privileges"] = [p for p in PRIVILEGES if self.authorize(actor, p, urn(r["name"]))]
            if self.authorize(actor, ADMIN, None):
                out["grants"] = self.grants(r["name"])
        return out

    def list_locations(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(select(external_locations).order_by(external_locations.c.name))]
        return [self._loc_out(r, actor) for r in rows if self._may_see(actor, r["name"])]

    def get_location(self, actor: str, name: str) -> dict:
        r = self._loc_row(name)
        if not self._may_see(actor, name):
            raise StorageError(f"no external location {name}", 404)
        return self._loc_out(r, actor)

    def create_location(self, actor: str, name: str, url: str, credential: str, read_only: bool = False,
                        comment: str = "") -> dict:
        self._admin(actor)
        if not NAME.match(name or ""):
            raise StorageError("name: 3 to 64 lowercase letters, digits, - and _")
        u = parse_url(url)
        cred = self._cred_row(credential)
        self._check_pair(u, cred["provider"], json.loads(cred["config"]))
        if any(overlaps(r, u) for r in self.reserved):
            raise StorageError(f"{url} overlaps Cairn's own storage", 409)
        with self.engine.begin() as conn:
            if conn.execute(select(external_locations.c.name).where(external_locations.c.name == name)).first():
                raise StorageError(f"an external location {name} exists", 409)
            for r in conn.execute(select(external_locations.c.name, external_locations.c.url)):
                if overlaps(parse_url(r.url), u):
                    raise StorageError(f"{url} overlaps external location {r.name} ({r.url})", 409)
            now = _now()
            conn.execute(insert(external_locations).values(
                name=name, url=url.strip().rstrip("/"), credential=credential, read_only=bool(read_only),
                comment=str(comment or "")[:2000], owner=actor, created_ms=now, updated_ms=now))
        self._audit("storage.location_created", actor, urn(name), {"url": url, "credential": credential,
                                                                    "read_only": bool(read_only)})
        self.reload()
        return self.get_location(actor, name)

    def update_location(self, actor: str, name: str, credential: Optional[str] = None, read_only: Optional[bool] = None,
                        comment: Optional[str] = None) -> dict:
        self._admin(actor)
        cur = self._loc_row(name)
        values: dict[str, Any] = {"updated_ms": _now()}
        if credential is not None and credential != cur["credential"]:
            cred = self._cred_row(credential)
            self._check_pair(parse_url(cur["url"]), cred["provider"], json.loads(cred["config"]))
            values["credential"] = credential
        if read_only is not None:
            values["read_only"] = bool(read_only)
        if comment is not None:
            values["comment"] = str(comment)[:2000]
        with self.engine.begin() as conn:
            conn.execute(update(external_locations).where(external_locations.c.name == name).values(**values))
        self._audit("storage.location_changed", actor, urn(name), {k: v for k, v in values.items() if k != "updated_ms"})
        return self.get_location(actor, name)

    def delete_location(self, actor: str, name: str) -> None:
        self._admin(actor)
        self._loc_row(name)
        with self.engine.begin() as conn:
            conn.execute(delete(location_grants).where(location_grants.c.location == name))
            conn.execute(delete(external_locations).where(external_locations.c.name == name))
        self._audit("storage.location_deleted", actor, urn(name), {})
        self.reload()

    def grant(self, actor: str, name: str, principal: str, privileges: list[str]) -> dict:
        """Sets ``principal``'s privileges on the location (replacing what they had)."""
        self._admin(actor)
        self._loc_row(name)
        if not PRINCIPAL.match(principal or ""):
            raise StorageError("principal: urn:li:corpuser:… or urn:li:corpGroup:…")
        privs = sorted(set(privileges or []))
        if not privs or any(p not in PRIVILEGES for p in privs):
            raise StorageError(f"privileges: one or more of {', '.join(PRIVILEGES)}")
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(delete(location_grants).where(and_(location_grants.c.location == name,
                                                            location_grants.c.principal == principal)))
            for p in privs:
                conn.execute(insert(location_grants).values(location=name, principal=principal, privilege=p,
                                                            granted_by=actor, created_ms=now))
        self._audit("storage.location_granted", actor, urn(name), {"principal": principal, "privileges": privs})
        self.reload()
        return self.get_location(actor, name)

    def revoke(self, actor: str, name: str, principal: str, privilege: Optional[str] = None) -> dict:
        self._admin(actor)
        self._loc_row(name)
        cond = and_(location_grants.c.location == name, location_grants.c.principal == principal)
        if privilege:
            cond = and_(cond, location_grants.c.privilege == privilege)
        with self.engine.begin() as conn:
            n = conn.execute(delete(location_grants).where(cond)).rowcount
        if not n:
            raise StorageError(f"{principal} has no such grant on {name}", 404)
        self._audit("storage.location_revoked", actor, urn(name), {"principal": principal, "privilege": privilege})
        self.reload()
        return self.get_location(actor, name)

    # ---- Cairn Catalog -----------------------------------------------------------------------------------------
    def catalog_config(self) -> dict:
        """Every registration, for Cairn Catalog: references, never secrets (Cairn holds none)."""
        with self.engine.connect() as conn:
            creds = [dict(r._mapping) for r in conn.execute(select(storage_credentials))]
            locs = [dict(r._mapping) for r in conn.execute(select(external_locations))]
        return {"credentials": [{"name": c["name"], "provider": c["provider"], "config": json.loads(c["config"]),
                                 "secret_ref": c["secret_ref"]} for c in creds],
                "locations": [{"name": loc["name"], "url": loc["url"], "credential": loc["credential"],
                               "read_only": bool(loc["read_only"])} for loc in locs]}

    def decide(self, event: dict) -> dict:
        """One use of a location by Cairn Catalog: decided (a location privilege, for a Cairn user) and recorded."""
        name = str(event.get("location") or "")
        action = str(event.get("action") or "")
        user = event.get("user")
        privilege = event.get("privilege")
        principal = str(event.get("principal") or "")
        url = str(event.get("url") or "")
        actor = (user if str(user).startswith("urn:li:") else f"urn:li:corpuser:{user}") if user else f"catalog-token:{principal}"
        allowed, reason = True, ""
        try:
            row = self._loc_row(name)
            if not covers(parse_url(row["url"]), parse_url(url)):
                allowed, reason = False, f"{url} is not in external location {name}"
            elif event.get("access") == "write" and row["read_only"]:
                allowed, reason = False, f"external location {name} is read-only"
            elif privilege is not None:
                if privilege not in PRIVILEGES:
                    allowed, reason = False, f"unknown privilege {privilege}"
                elif user and (self.authorize is None or not self.authorize(actor, privilege, urn(name))):
                    who = actor.removeprefix("urn:li:corpuser:")
                    allowed, reason = False, f"Cairn's policies do not give {who} {privilege} on external location {name}"
        except StorageError as e:
            allowed, reason = False, str(e)
        except Exception:  # noqa: BLE001 - fail closed
            logger.warning("storage decision failed", exc_info=True)
            allowed, reason = False, "the decision could not be made"
        if action not in ("vend", "create_table"):
            action = "use"
        self._audit(f"storage.{action}", actor, urn(name) if name else None,
                    {"allowed": allowed, "privilege": privilege, "access": event.get("access"), "url": url,
                     "table": event.get("table"), "catalog_principal": principal, **({"reason": reason} if reason else {})})
        return {"allowed": allowed, "reason": reason}


def build_storage_router(st: StorageLocations, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/storage", tags=["storage"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except StorageError as e:
            raise HTTPException(e.status, str(e))

    def service_only(p) -> None:
        if not p.is_service:
            raise HTTPException(403, "only Cairn Catalog (a Cairn service token) asks this")

    @router.get("/credentials")
    async def list_credentials(p=Depends(current_principal)):
        return {"credentials": await call(lambda: st.list_credentials(p.urn))}

    @router.post("/credentials", status_code=201)
    async def create_credential(name: str = Body(...), provider: str = Body(...), config: dict = Body(default_factory=dict),
                                secret_ref: Optional[str] = Body(None), comment: str = Body(""),
                                p=Depends(current_principal)):
        return await call(lambda: st.create_credential(p.urn, name, provider, config, secret_ref, comment))

    @router.get("/credentials/{name}")
    async def get_credential(name: str, p=Depends(current_principal)):
        return await call(lambda: st.get_credential(p.urn, name))

    @router.patch("/credentials/{name}")
    async def update_credential(name: str, config: Optional[dict] = Body(None), secret_ref: Optional[str] = Body(None),
                                comment: Optional[str] = Body(None), p=Depends(current_principal)):
        return await call(lambda: st.update_credential(p.urn, name, config, secret_ref, comment))

    @router.delete("/credentials/{name}", status_code=204)
    async def delete_credential(name: str, p=Depends(current_principal)):
        await call(lambda: st.delete_credential(p.urn, name))

    @router.get("/locations")
    async def list_locations(p=Depends(current_principal)):
        return {"locations": await call(lambda: st.list_locations(p.urn))}

    @router.post("/locations", status_code=201)
    async def create_location(name: str = Body(...), url: str = Body(...), credential: str = Body(...),
                              read_only: bool = Body(False), comment: str = Body(""), p=Depends(current_principal)):
        return await call(lambda: st.create_location(p.urn, name, url, credential, read_only, comment))

    @router.get("/locations/{name}")
    async def get_location(name: str, p=Depends(current_principal)):
        return await call(lambda: st.get_location(p.urn, name))

    @router.patch("/locations/{name}")
    async def update_location(name: str, credential: Optional[str] = Body(None), read_only: Optional[bool] = Body(None),
                              comment: Optional[str] = Body(None), p=Depends(current_principal)):
        return await call(lambda: st.update_location(p.urn, name, credential, read_only, comment))

    @router.delete("/locations/{name}", status_code=204)
    async def delete_location(name: str, p=Depends(current_principal)):
        await call(lambda: st.delete_location(p.urn, name))

    @router.put("/locations/{name}/grants")
    async def grant(name: str, principal: str = Body(...), privileges: list[str] = Body(...), p=Depends(current_principal)):
        return await call(lambda: st.grant(p.urn, name, principal, privileges))

    @router.delete("/locations/{name}/grants")
    async def revoke(name: str, principal: str, privilege: Optional[str] = None, p=Depends(current_principal)):
        return await call(lambda: st.revoke(p.urn, name, principal, privilege))

    @router.get("/catalog-config")
    async def catalog_config(p=Depends(current_principal)):
        service_only(p)
        return await call(st.catalog_config)

    @router.post("/decide")
    async def decide(event: dict = Body(...), p=Depends(current_principal)):
        service_only(p)
        return await call(lambda: st.decide(event))

    return router
