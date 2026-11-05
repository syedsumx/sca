"""Governed volumes: folders of files under Cairn's grants, read through Cairn's API or by Spark with short-lived,
narrowed store credentials (L58).

A volume is a bucket of its own in Cairn Store (``cairn-vol-<name>``). Who may read or write it is Cairn's
decision, not a bucket policy's: its owner, and the people and groups it is granted to (``read`` or ``write``).

* **Through the API** (``/v1/volumes/{name}/files/...``): Cairn checks the grant on every call and reads or
  writes the store with its own key. Audited.
* **From Spark and other engines** (``POST /v1/volumes/{name}/credentials``): Cairn checks the grant, then asks the
  store's STS for temporary credentials of the ``cairn-volumes`` role narrowed by a session policy to that one
  bucket, and to reading unless the person may write. They last ``CAIRN_VOLUMES_CREDENTIAL_TTL`` (900 seconds;
  at most 3600), so a removed grant stops working within that time. ``cairn_spark.volume_conf`` sets them up.
* **Who manages.** Creating a volume needs ``MANAGE_VOLUMES``; its owner (or an operator) grants, revokes and
  deletes it. A volume is deleted only when empty.

Cairn reaches the store with ``CAIRN_STORE_URL`` and ``CAIRN_VOLUMES_ACCESS_KEY`` /
``CAIRN_VOLUMES_SECRET_KEY`` (or ``_FILE``), an admin key of the store, falling back to the store admin key.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Callable, Iterator, Optional
from urllib.parse import quote, urlencode

import httpx
from sqlalchemy import BigInteger, Column, String, Table, Text, and_, delete, insert, select
from starlette.requests import Request  # a module global: FastAPI resolves the endpoints' annotations here

from cairn.common.db import metadata
from cairn.storage.store_admin import sigv4_headers

logger = logging.getLogger(__name__)

PRIVILEGE = "MANAGE_VOLUMES"
ROLE = "cairn-volumes"
ACCOUNT = "000000000000"
NAME = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")
SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
PRINCIPAL = re.compile(r"^urn:li:(corpuser|corpGroup):[^\s]+$")
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
READ_ACTIONS = ["s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"]
WRITE_ACTIONS = READ_ACTIONS + ["s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload",
                                "s3:ListMultipartUploadParts", "s3:ListBucketMultipartUploads"]

volumes = Table(
    "cairn_volumes", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(40), nullable=False, unique=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("description", Text, nullable=False),
    Column("bucket", String(63), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)
volume_grants = Table(
    "cairn_volume_grants", metadata,
    Column("volume_id", String(32), primary_key=True),
    Column("principal", String(256), primary_key=True),
    Column("access", String(5), nullable=False),            # read | write
    Column("granted_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)


class VolumeError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def bucket_of(name: str) -> str:
    return f"cairn-vol-{name}"


def file_path(path: str) -> str:
    """A file's path in a volume: segments of letters, digits, '.', '_' and '-', separated by '/'."""
    parts = [p for p in (path or "").split("/")]
    if not parts or any(not SEGMENT.match(p) or p in (".", "..") for p in parts) or len(path) > 1024:
        raise VolumeError("a file's path is segments of letters, digits, '.', '_' and '-', separated by '/'")
    return path


def session_policy(bucket: str, write: bool) -> str:
    """The session policy that narrows the role to one volume's bucket, and to reading unless ``write``."""
    return json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": WRITE_ACTIONS if write else READ_ACTIONS,
         "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]}]}, separators=(",", ":"))


class StoreClient:
    """The few S3, IAM and STS calls volumes need, signed with Cairn's store key."""

    def __init__(self, url: str, access_key: str, secret_key: str, region: str = "us-east-1", timeout: float = 60.0):
        self.url, self.ak, self.sk, self.region = url.rstrip("/"), access_key, secret_key, region
        self.http = httpx.Client(timeout=timeout)

    def _s3(self, method: str, path: str, query: Optional[dict] = None, body: bytes = b"",
            ok: tuple[int, ...] = (200, 204)) -> httpx.Response:
        url = self.url + path + (f"?{urlencode(sorted(query.items()), quote_via=quote)}" if query else "")
        h = sigv4_headers(method, url, body, self.ak, self.sk, self.region)
        try:
            r = self.http.request(method, url, content=body, headers=h)
        except httpx.HTTPError as e:
            raise VolumeError(f"Cairn Store is not reachable: {e}", 502) from e
        if r.status_code not in ok:
            code = re.search(r"<Code>([^<]+)</Code>", r.text)
            raise VolumeError(f"store: {method} {path}: {r.status_code} {code.group(1) if code else r.text[:200]}",
                              404 if r.status_code == 404 else 409 if r.status_code == 409 else 502)
        return r

    def _form(self, service: str, params: dict) -> str:
        body = urlencode(params).encode()
        h = sigv4_headers("POST", self.url + "/", body, self.ak, self.sk, self.region, service=service)
        h["content-type"] = "application/x-www-form-urlencoded"
        try:
            r = self.http.post(self.url + "/", content=body, headers=h)
        except httpx.HTTPError as e:
            raise VolumeError(f"Cairn Store is not reachable: {e}", 502) from e
        if r.status_code != 200:
            code = re.search(r"<Code>([^<]+)</Code>", r.text)
            raise VolumeError(f"store {service}: {params['Action']}: {r.status_code} {code.group(1) if code else ''}",
                              409 if code and code.group(1) == "EntityAlreadyExists" else 502)
        return r.text

    def create_bucket(self, bucket: str) -> None:
        self._s3("PUT", f"/{bucket}")

    def delete_bucket(self, bucket: str) -> None:
        self._s3("DELETE", f"/{bucket}")

    def list(self, bucket: str, prefix: str = "", limit: int = 1000, after: str = "") -> dict:
        q = {"list-type": "2", "max-keys": str(limit), "prefix": prefix}
        if after:
            q["start-after"] = after
        root = ET.fromstring(self._s3("GET", f"/{bucket}", q).text)
        files = [{"path": c.findtext(f"{NS}Key"), "size": int(c.findtext(f"{NS}Size") or 0),
                  "modified": c.findtext(f"{NS}LastModified")} for c in root.findall(f"{NS}Contents")]
        return {"files": files, "truncated": (root.findtext(f"{NS}IsTruncated") or "false") == "true"}

    def get(self, bucket: str, key: str) -> Iterator[bytes]:
        url = f"{self.url}/{bucket}/{key}"
        h = sigv4_headers("GET", url, b"", self.ak, self.sk, self.region)
        with self.http.stream("GET", url, headers=h) as r:
            if r.status_code != 200:
                raise VolumeError("no such file" if r.status_code == 404 else f"store: GET: {r.status_code}",
                                  404 if r.status_code == 404 else 502)
            yield from r.iter_bytes(1 << 20)

    def head(self, bucket: str, key: str) -> dict:
        r = self._s3("HEAD", f"/{bucket}/{key}", ok=(200, 404))
        if r.status_code == 404:
            raise VolumeError("no such file", 404)
        return {"size": int(r.headers.get("content-length", 0)), "type": r.headers.get("content-type", "")}

    def put(self, bucket: str, key: str, body: bytes) -> None:
        self._s3("PUT", f"/{bucket}/{key}", body=body)

    def delete(self, bucket: str, key: str) -> None:
        self._s3("DELETE", f"/{bucket}/{key}")

    def ensure_role(self) -> str:
        """The cairn-volumes role (trusting this account, allowed on every volume's bucket), made once."""
        arn = f"arn:aws:iam::{ACCOUNT}:role/{ROLE}"
        trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                          "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:root"}}]}
        try:
            self._form("iam", {"Action": "CreateRole", "Version": "2010-05-08", "RoleName": ROLE,
                               "AssumeRolePolicyDocument": json.dumps(trust), "MaxSessionDuration": "3600"})
        except VolumeError as e:
            if e.status != 409:
                raise
        self._form("iam", {"Action": "PutRolePolicy", "Version": "2010-05-08", "RoleName": ROLE,
                           "PolicyName": "volumes", "PolicyDocument": json.dumps({"Version": "2012-10-17", "Statement": [
                               {"Effect": "Allow", "Action": WRITE_ACTIONS,
                                "Resource": ["arn:aws:s3:::cairn-vol-*", "arn:aws:s3:::cairn-vol-*/*"]}]})})
        return arn

    def assume(self, role_arn: str, session: str, policy: str, seconds: int) -> dict:
        xml = self._form("sts", {"Action": "AssumeRole", "Version": "2011-06-15", "RoleArn": role_arn,
                                 "RoleSessionName": session, "Policy": policy, "DurationSeconds": str(seconds)})
        get = lambda tag: (re.search(f"<{tag}>([^<]+)</{tag}>", xml) or [None, None])[1]  # noqa: E731
        if not get("AccessKeyId"):
            raise VolumeError("store sts: no credentials in the answer", 502)
        return {"access_key_id": get("AccessKeyId"), "secret_access_key": get("SecretAccessKey"),
                "session_token": get("SessionToken"), "expiration": get("Expiration")}


class Volumes:
    def __init__(self, engine, store: Optional[StoreClient], groups_of: Callable[[str], list[str]],
                 may_create: Callable[[str], bool], is_operator: Callable[[str], bool], audit=None,
                 endpoint: str = ""):
        """``endpoint``: the store's address as engines reach it (given back with credentials)."""
        self.engine, self.store, self.groups_of = engine, store, groups_of
        self.may_create, self.is_operator, self.audit = may_create, is_operator, audit
        self.endpoint = endpoint or (store.url if store else "")
        ttl = int(os.environ.get("CAIRN_VOLUMES_CREDENTIAL_TTL", "900"))
        self.ttl = min(max(ttl, 900), 3600)
        self.max_upload = int(os.environ.get("CAIRN_VOLUMES_MAX_UPLOAD_BYTES", str(100 << 20)))
        self._role: Optional[str] = None

    # ---- who ---------------------------------------------------------------------------------------
    def _store(self) -> StoreClient:
        if self.store is None:
            raise VolumeError("volumes are not configured: Cairn has no store key (CAIRN_VOLUMES_ACCESS_KEY)", 503)
        return self.store

    def _vol(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(volumes).where(volumes.c.name == name)).first()
        if r is None:
            raise VolumeError(f"no volume {name}", 404)
        return dict(r._mapping)

    def access(self, actor: str, v: dict) -> str:
        """``write``, ``read`` or ``""`` for ``actor`` on volume ``v``."""
        if actor == v["owner"]:
            return "write"
        try:
            groups = [g for g in self.groups_of(actor) if g]
        except Exception:  # noqa: BLE001 - groups unknown: only what is granted to the person counts
            logger.warning("could not look up the groups of %s", actor)
            groups = []
        with self.engine.connect() as conn:
            got = {r[0] for r in conn.execute(select(volume_grants.c.access).where(and_(
                volume_grants.c.volume_id == v["id"], volume_grants.c.principal.in_([actor, *groups]))))}
        return "write" if "write" in got else "read" if "read" in got else ""

    def _need(self, actor: str, name: str, want: str) -> dict:
        v = self._vol(name)
        have = self.access(actor, v)
        if not have:
            raise VolumeError(f"no volume {name}", 404)          # not even told it exists
        if want == "write" and have != "write":
            raise VolumeError(f"{name} is shared with you to read, not to write", 403)
        if want == "manage" and actor != v["owner"] and not self.is_operator(actor):
            raise VolumeError(f"only the owner of {name} or an operator manages it", 403)
        return v

    def _manageable(self, actor: str, name: str) -> dict:
        v = self._vol(name)
        if actor != v["owner"] and not self.is_operator(actor):
            if not self.access(actor, v):
                raise VolumeError(f"no volume {name}", 404)
            raise VolumeError(f"only the owner of {name} or an operator manages it", 403)
        return v

    # ---- volumes and grants ------------------------------------------------------------------------
    def create(self, actor: str, name: str, description: str = "") -> dict:
        if not self.may_create(actor):
            raise VolumeError(f"creating volumes needs {PRIVILEGE}", 403)
        if not NAME.match(name or ""):
            raise VolumeError("name: 3 to 40 lowercase letters, digits and -")
        store = self._store()
        with self.engine.connect() as conn:
            if conn.execute(select(volumes.c.id).where(volumes.c.name == name)).first():
                raise VolumeError(f"a volume {name} exists", 409)
        store.create_bucket(bucket_of(name))
        with self.engine.begin() as conn:
            conn.execute(insert(volumes).values(id=uuid.uuid4().hex, name=name, owner=actor,
                                                description=str(description or "")[:2000], bucket=bucket_of(name),
                                                created_ms=_now()))
        self._audit(actor, "volume.created", name, {})
        return self.get(actor, name)

    def get(self, actor: str, name: str) -> dict:
        v = self._vol(name)
        have = self.access(actor, v)
        if not have and not self.is_operator(actor):
            raise VolumeError(f"no volume {name}", 404)
        out = {k: v[k] for k in ("name", "owner", "description", "bucket", "created_ms")}
        out["access"] = have or "manage"
        out["path"] = f"s3a://{v['bucket']}/"
        if actor == v["owner"] or self.is_operator(actor):
            out["grants"] = self.grants(v)
        return out

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            names = [r[0] for r in conn.execute(select(volumes.c.name).order_by(volumes.c.name))]
        out = []
        for n in names:
            try:
                out.append(self.get(actor, n))
            except VolumeError:
                continue
        return out

    def grants(self, v: dict) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(
                select(volume_grants.c.principal, volume_grants.c.access, volume_grants.c.granted_by,
                       volume_grants.c.created_ms).where(volume_grants.c.volume_id == v["id"])
                .order_by(volume_grants.c.principal))]

    def grant(self, actor: str, name: str, principal: str, access: str) -> dict:
        v = self._manageable(actor, name)
        if not PRINCIPAL.match(principal or ""):
            raise VolumeError("principal: urn:li:corpuser:… or urn:li:corpGroup:…")
        if access not in ("read", "write"):
            raise VolumeError("access: read or write")
        with self.engine.begin() as conn:
            conn.execute(delete(volume_grants).where(and_(volume_grants.c.volume_id == v["id"],
                                                          volume_grants.c.principal == principal)))
            conn.execute(insert(volume_grants).values(volume_id=v["id"], principal=principal, access=access,
                                                      granted_by=actor, created_ms=_now()))
        self._audit(actor, "volume.granted", name, {"principal": principal, "access": access})
        return self.get(actor, name)

    def revoke(self, actor: str, name: str, principal: str) -> dict:
        v = self._manageable(actor, name)
        with self.engine.begin() as conn:
            n = conn.execute(delete(volume_grants).where(and_(volume_grants.c.volume_id == v["id"],
                                                              volume_grants.c.principal == principal))).rowcount
        if not n:
            raise VolumeError(f"{principal} has no grant on {name}", 404)
        self._audit(actor, "volume.revoked", name, {"principal": principal,
                                                    "note": f"credentials already issued last up to {self.ttl}s"})
        return self.get(actor, name)

    def drop(self, actor: str, name: str) -> None:
        v = self._manageable(actor, name)
        store = self._store()
        if store.list(v["bucket"], limit=1)["files"]:
            raise VolumeError(f"{name} is not empty: delete its files first", 409)
        store.delete_bucket(v["bucket"])
        with self.engine.begin() as conn:
            conn.execute(delete(volume_grants).where(volume_grants.c.volume_id == v["id"]))
            conn.execute(delete(volumes).where(volumes.c.id == v["id"]))
        self._audit(actor, "volume.deleted", name, {})

    # ---- files ---------------------------------------------------------------------------------------
    def files(self, actor: str, name: str, prefix: str = "", after: str = "") -> dict:
        v = self._need(actor, name, "read")
        if prefix:
            file_path(prefix.rstrip("/"))
        return self._store().list(v["bucket"], prefix, after=after)

    def read(self, actor: str, name: str, path: str) -> tuple[dict, Iterator[bytes]]:
        v = self._need(actor, name, "read")
        key = file_path(path)
        store = self._store()
        meta = store.head(v["bucket"], key)
        self._audit(actor, "volume.file_read", name, {"path": key, "bytes": meta["size"]})
        return meta, store.get(v["bucket"], key)

    def write(self, actor: str, name: str, path: str, body: bytes) -> dict:
        v = self._need(actor, name, "write")
        key = file_path(path)
        if len(body) > self.max_upload:
            raise VolumeError(f"over {self.max_upload} bytes: upload larger files with the volume's credentials", 413)
        self._store().put(v["bucket"], key, body)
        self._audit(actor, "volume.file_written", name, {"path": key, "bytes": len(body)})
        return {"path": key, "size": len(body)}

    def remove(self, actor: str, name: str, path: str) -> None:
        v = self._need(actor, name, "write")
        key = file_path(path)
        self._store().delete(v["bucket"], key)
        self._audit(actor, "volume.file_deleted", name, {"path": key})

    # ---- credentials for engines -----------------------------------------------------------------------
    def credentials(self, actor: str, name: str, access: str = "read", ttl: Optional[int] = None) -> dict:
        if access not in ("read", "write"):
            raise VolumeError("access: read or write")
        v = self._need(actor, name, access)
        store = self._store()
        seconds = self.ttl if ttl is None else int(ttl)
        if not 900 <= seconds <= self.ttl:
            raise VolumeError(f"ttl_seconds: 900 to {self.ttl}")
        if self._role is None:
            self._role = store.ensure_role()
        who = re.sub(r"[^A-Za-z0-9+=,.@_-]", "-", actor.rsplit(":", 1)[-1])
        session = f"vol-{name}-{who}"[:64]
        creds = store.assume(self._role, session, session_policy(v["bucket"], access == "write"), seconds)
        self._audit(actor, "volume.credentials", name, {"access": access, "seconds": seconds})
        return {**creds, "bucket": v["bucket"], "path": f"s3a://{v['bucket']}/", "endpoint": self.endpoint,
                "access": access, "region": store.region}

    def _audit(self, actor: str, action: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("dataaccess", action, actor, f"volume:{name}", details)


def build_volumes_router(vs: Volumes, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from fastapi.responses import StreamingResponse
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/volumes", tags=["volumes"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except VolumeError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        return await call(lambda: vs.list(p.urn))

    @router.post("", status_code=201)
    async def create(name: str = Body(...), description: str = Body(""), p=Depends(current_principal)):
        return await call(lambda: vs.create(p.urn, name, description))

    @router.get("/{name}")
    async def get(name: str, p=Depends(current_principal)):
        return await call(lambda: vs.get(p.urn, name))

    @router.delete("/{name}", status_code=204)
    async def drop(name: str, p=Depends(current_principal)):
        await call(lambda: vs.drop(p.urn, name))

    @router.put("/{name}/grants")
    async def grant(name: str, principal: str = Body(...), access: str = Body(...), p=Depends(current_principal)):
        return await call(lambda: vs.grant(p.urn, name, principal, access))

    @router.delete("/{name}/grants")
    async def revoke(name: str, principal: str, p=Depends(current_principal)):
        return await call(lambda: vs.revoke(p.urn, name, principal))

    @router.get("/{name}/files")
    async def files(name: str, prefix: str = "", after: str = "", p=Depends(current_principal)):
        return await call(lambda: vs.files(p.urn, name, prefix, after))

    @router.get("/{name}/files/{path:path}")
    async def read(name: str, path: str, p=Depends(current_principal)):
        meta, body = await call(lambda: vs.read(p.urn, name, path))
        return StreamingResponse(body, media_type=meta["type"] or "application/octet-stream",
                                 headers={"content-length": str(meta["size"])})

    @router.put("/{name}/files/{path:path}")
    async def write(name: str, path: str, request: Request, p=Depends(current_principal)):
        size = int(request.headers.get("content-length") or 0)
        if size > vs.max_upload:
            raise HTTPException(413, f"over {vs.max_upload} bytes: upload larger files with the volume's credentials")
        body = await request.body()
        return await call(lambda: vs.write(p.urn, name, path, body))

    @router.delete("/{name}/files/{path:path}", status_code=204)
    async def remove(name: str, path: str, p=Depends(current_principal)):
        await call(lambda: vs.remove(p.urn, name, path))

    @router.post("/{name}/credentials")
    async def credentials(name: str, access: str = Body("read"), ttl_seconds: Optional[int] = Body(None),
                          p=Depends(current_principal)):
        return await call(lambda: vs.credentials(p.urn, name, access, ttl_seconds))

    return router
