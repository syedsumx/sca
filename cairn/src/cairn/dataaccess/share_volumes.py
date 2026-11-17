"""Files in a share: a volume, or a folder of one, read by the share's recipient (L169).

A share hands a recipient outside the organisation tables (``cairn.dataaccess.shares``). Files that are not tables
(documents, images, model files, exports) live in governed volumes (``cairn.dataaccess.volumes``); this adds them to
a share.

* **Attaching.** ``POST /v1/shares/<share>/volumes`` with ``{"volume": "contracts", "prefix": "2026/"}``, by the
  share's maker or an operator, who must manage the volume (its owner, or an operator). ``prefix`` is a folder
  (ending in ``/``), or empty for the whole volume. ``DELETE /v1/shares/<share>/volumes/<volume>`` (``?prefix=``)
  takes it out again; ``GET`` lists them.
* **The recipient** uses the share's own credential (its OAuth token as a bearer, or the credential as HTTP Basic):
  ``GET /v1/shares/recipient/volumes`` lists what is shared; ``.../volumes/<volume>/files`` lists the files under the
  shared folders and ``.../files/<path>`` downloads one. Cairn reads the store with its own key and checks the share
  on every call, so a revoked or expired share, or a detached volume, is refused at the next request.
* **For engines** (Spark, pandas with s3fs): ``POST /v1/shares/recipient/volumes/<volume>/credentials`` gives
  temporary store credentials of the ``cairn-volumes`` role, narrowed by a session policy to reading that bucket's
  shared folders, lasting 900 seconds and never past the share's end. Like the storage keys of shared tables, ones
  already handed out last until they expire.
* **Read-only, always.** A recipient never writes, and never sees a file outside the shared folders, nor the names
  of other volumes. Every download and every credential is audited as the recipient (``share:<id>``) and counted in
  the share's usage (as ``volume:<name>``).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Iterator, Optional

from sqlalchemy import BigInteger, Column, PrimaryKeyConstraint, String, Table, and_, delete, insert, select
from starlette.requests import Request  # module level: FastAPI resolves the endpoints' annotations here

from cairn.common.db import metadata
from cairn.dataaccess.shares import PREFIX, ShareError
from cairn.dataaccess.volumes import READ_ACTIONS, VolumeError, file_path

logger = logging.getLogger(__name__)

MAX_PER_SHARE = 50
CREDENTIAL_SECONDS = 900

share_volumes = Table(
    "cairn_share_volumes", metadata,
    Column("share_id", String(32), nullable=False, index=True),
    Column("volume", String(40), nullable=False, index=True),
    Column("prefix", String(1024), nullable=False),          # "" (the whole volume) or a folder ending in "/"
    Column("attached_by", String(256), nullable=False),
    Column("attached_ms", BigInteger, nullable=False),
    PrimaryKeyConstraint("share_id", "volume", "prefix"),
)


def folder(prefix: str) -> str:
    """A shared folder: ``""`` or path segments ending in ``/``."""
    prefix = str(prefix or "")
    if not prefix:
        return ""
    if not prefix.endswith("/"):
        raise ShareError("prefix: a folder, ending in /")
    try:
        file_path(prefix[:-1])
    except VolumeError as e:
        raise ShareError(f"prefix: {e}")
    return prefix


def read_policy(bucket: str, prefixes: list[str]) -> str:
    """The session policy for a recipient: reading the shared folders of one bucket, and listing only under them."""
    if "" in prefixes:
        return json.dumps({"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": READ_ACTIONS,
             "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]}]}, separators=(",", ":"))
    return json.dumps({"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": [f"arn:aws:s3:::{bucket}/{p}*" for p in prefixes]},
        {"Effect": "Allow", "Action": ["s3:GetBucketLocation"], "Resource": [f"arn:aws:s3:::{bucket}"]},
        {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": [f"arn:aws:s3:::{bucket}"],
         "Condition": {"StringLike": {"s3:prefix": [f"{p}*" for p in prefixes]}}}]}, separators=(",", ":"))


class ShareVolumes:
    def __init__(self, engine, shares, volumes, recipient: Callable[[str], dict], audit=None):
        """``recipient(authorization)``: the active share behind a recipient's credential
        (``SharePackages.recipient``)."""
        self.engine, self.shares, self.volumes, self.recipient, self.audit = engine, shares, volumes, recipient, audit

    # ---- the provider's side -----------------------------------------------------------------------------
    def _attached(self, share_id: str, volume: Optional[str] = None) -> list[dict]:
        where = share_volumes.c.share_id == share_id
        if volume is not None:
            where = and_(where, share_volumes.c.volume == volume)
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(
                select(share_volumes).where(where).order_by(share_volumes.c.volume, share_volumes.c.prefix))]

    @staticmethod
    def _public(rows: list[dict]) -> list[dict]:
        return [{"volume": r["volume"], "prefix": r["prefix"], "attached_by": r["attached_by"],
                 "attached_ms": r["attached_ms"]} for r in rows]

    def list(self, actor: str, everyone: bool, ref: str) -> dict:
        row = self.shares._find(ref, actor, everyone)
        return {"share": row["name"], "volumes": self._public(self._attached(row["id"]))}

    def attach(self, actor: str, everyone: bool, ref: str, volume: str, prefix: str = "") -> dict:
        row = self.shares._find(ref, actor, everyone)
        if not self.shares._active(row):
            raise ShareError("the share has ended", 409)
        prefix = folder(prefix)
        try:
            self.volumes._manageable(actor, str(volume or ""))       # its owner or an operator; 404 when unseen
        except VolumeError as e:
            raise ShareError(str(e), e.status)
        have = self._attached(row["id"])
        if any(r["volume"] == volume and r["prefix"] == prefix for r in have):
            raise ShareError(f"{volume}/{prefix} is in the share already", 409)
        if len(have) >= MAX_PER_SHARE:
            raise ShareError(f"a share carries at most {MAX_PER_SHARE} folders")
        with self.engine.begin() as conn:
            conn.execute(insert(share_volumes).values(share_id=row["id"], volume=volume, prefix=prefix,
                                                      attached_by=actor, attached_ms=int(time.time() * 1000)))
            if self.audit is not None:
                self.audit.record("shares", "share.volume_attached", actor, None,
                                  {"share": row["id"], "name": row["name"], "recipient": row["recipient"],
                                   "volume": volume, "prefix": prefix}, conn=conn)
        return self.list(actor, everyone, ref)

    def detach(self, actor: str, everyone: bool, ref: str, volume: str, prefix: Optional[str] = None) -> dict:
        """Takes a volume's folder out of the share; with no ``prefix``, every folder of that volume."""
        row = self.shares._find(ref, actor, everyone)
        where = and_(share_volumes.c.share_id == row["id"], share_volumes.c.volume == volume)
        if prefix is not None:
            where = and_(where, share_volumes.c.prefix == folder(prefix))
        with self.engine.begin() as conn:
            n = conn.execute(delete(share_volumes).where(where)).rowcount
            if not n:
                raise ShareError(f"{volume} is not in the share", 404)
            if self.audit is not None:
                self.audit.record("shares", "share.volume_detached", actor, None,
                                  {"share": row["id"], "name": row["name"], "volume": volume, "prefix": prefix,
                                   "note": f"credentials already issued last up to {CREDENTIAL_SECONDS}s"}, conn=conn)
        return self.list(actor, everyone, ref)

    def volume_removed(self, volume: str) -> None:
        """A deleted volume leaves every share (a new volume of the same name is not shared by the old entry)."""
        with self.engine.begin() as conn:
            conn.execute(delete(share_volumes).where(share_volumes.c.volume == volume))

    # ---- the recipient's side ----------------------------------------------------------------------------
    def _shared(self, row: dict, volume: str) -> tuple[dict, list[str]]:
        """The volume and its folders in this share; 404 for anything else, with nothing said about why."""
        prefixes = [r["prefix"] for r in self._attached(row["id"], volume)]
        try:
            v = self.volumes._vol(volume) if prefixes else None
        except VolumeError:
            v = None
        if v is None:
            self.shares._count(row["id"], f"volume:{volume}"[:512], refusals=1)
            raise ShareError("no such volume in the share", 404)
        return v, prefixes

    def _audit(self, row: dict, action: str, volume: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("shares", action, f"urn:li:corpuser:{PREFIX}{row['id']}", f"volume:{volume}",
                              {"share": row["id"], "name": row["name"], "recipient": row["recipient"], **details})

    def recipient_volumes(self, row: dict) -> list[dict]:
        self.shares._count(row["id"], "", requests=1)
        out: dict[str, list[str]] = {}
        for r in self._attached(row["id"]):
            out.setdefault(r["volume"], []).append(r["prefix"])
        return [{"volume": v, "folders": p, "files": f"/v1/shares/recipient/volumes/{v}/files"} for v, p in out.items()]

    def recipient_files(self, row: dict, volume: str, prefix: str = "", after: str = "") -> dict:
        """The files under the shared folders. ``prefix`` narrows the listing; one outside what is shared lists
        nothing that is not shared (it is refused unless it lies in a shared folder, or a shared folder lies in it)."""
        self.shares._count(row["id"], "", requests=1)
        v, prefixes = self._shared(row, volume)
        prefix = str(prefix or "")
        if prefix:
            try:
                file_path(prefix.rstrip("/"))
            except VolumeError as e:
                raise ShareError(str(e), 400)
        store = self.volumes._store()
        inside = [p for p in prefixes if prefix.startswith(p)]
        try:
            if inside:                                     # asked within a shared folder: that listing, as it is
                return store.list(v["bucket"], prefix, after=after)
            under = sorted(p for p in prefixes if p.startswith(prefix))
            if not under:
                raise ShareError("no such folder in the share", 404)
            files: list[dict] = []
            for p in under:                                # the shared folders below what was asked, in order
                if after and not after.startswith(p) and after > p:
                    continue                               # a folder listed in full on an earlier page
                got = store.list(v["bucket"], p, limit=1000 - len(files), after=after if after.startswith(p) else "")
                files.extend(got["files"])
                if got["truncated"] or len(files) >= 1000:
                    return {"files": files, "truncated": True}
            return {"files": files, "truncated": False}
        except VolumeError as e:
            raise ShareError(str(e), e.status)

    def recipient_read(self, row: dict, volume: str, path: str) -> tuple[dict, Iterator[bytes]]:
        self.shares._count(row["id"], "", requests=1)
        v, prefixes = self._shared(row, volume)
        try:
            key = file_path(path)
        except VolumeError as e:
            raise ShareError(str(e), 400)
        if not any(key.startswith(p) for p in prefixes):
            self.shares._count(row["id"], f"volume:{volume}"[:512], refusals=1)
            raise ShareError("no such file", 404)           # as for a file that is not there
        store = self.volumes._store()
        try:
            meta = store.head(v["bucket"], key)
            body = store.get(v["bucket"], key)
        except VolumeError as e:
            raise ShareError(str(e), e.status)
        self.shares._count(row["id"], f"volume:{volume}"[:512], loads=1)
        self._audit(row, "share.volume_file_read", volume, {"path": key, "bytes": meta["size"]})
        return meta, body

    def recipient_credentials(self, row: dict, volume: str) -> dict:
        """Temporary, read-only store credentials for the shared folders of one volume."""
        self.shares._count(row["id"], "", requests=1)
        v, prefixes = self._shared(row, volume)
        left = int(row["expires_s"]) - int(time.time())
        if left < CREDENTIAL_SECONDS:                       # the store's shortest session; none outlives the share
            raise ShareError(f"the share ends in under {CREDENTIAL_SECONDS} seconds: read through "
                             f"/v1/shares/recipient/volumes/{volume}/files instead", 409)
        vs = self.volumes
        try:
            store = vs._store()
            if vs._role is None:
                vs._role = store.ensure_role()
            session = re.sub(r"[^A-Za-z0-9+=,.@_-]", "-", f"share-{row['id']}-{volume}")[:64]
            creds = store.assume(vs._role, session, read_policy(v["bucket"], prefixes), CREDENTIAL_SECONDS)
        except VolumeError as e:
            raise ShareError(str(e), e.status)
        self.shares._count(row["id"], f"volume:{volume}"[:512], loads=1)
        self._audit(row, "share.volume_credentials", volume, {"folders": prefixes, "seconds": CREDENTIAL_SECONDS})
        return {**creds, "bucket": v["bucket"], "folders": prefixes, "endpoint": vs.endpoint, "access": "read",
                "region": store.region, "paths": [f"s3a://{v['bucket']}/{p}" for p in prefixes]}


def build_share_volumes_router(sv: ShareVolumes, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from fastapi.responses import StreamingResponse

    router = APIRouter(prefix="/v1/shares", tags=["shares"])

    def operator(p) -> bool:
        return bool(authorize(p, "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False))

    def person(p):
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "shares are managed by people")
        return p

    def call(fn, *a):
        try:
            return fn(*a)
        except ShareError as e:
            raise HTTPException(e.status, str(e))

    def recipient(request: Request) -> dict:
        try:
            return sv.recipient(request.headers.get("authorization", ""))
        except ShareError as e:
            headers = {"WWW-Authenticate": 'Bearer realm="shares"'} if e.status == 401 else None
            raise HTTPException(e.status, str(e), headers=headers)

    # The recipient's endpoints: authenticated by the share's own credential, never a person's session.
    @router.get("/recipient/volumes")
    def recipient_volumes(request: Request):
        return {"volumes": call(sv.recipient_volumes, recipient(request))}

    @router.get("/recipient/volumes/{volume}/files")
    def recipient_files(volume: str, request: Request, prefix: str = "", after: str = ""):
        return call(sv.recipient_files, recipient(request), volume, prefix, after)

    @router.get("/recipient/volumes/{volume}/files/{path:path}")
    def recipient_read(volume: str, path: str, request: Request):
        meta, body = call(sv.recipient_read, recipient(request), volume, path)
        # Always a download, never a page on Cairn's origin (as for volumes read by people).
        return StreamingResponse(body, media_type=meta["type"] or "application/octet-stream",
                                 headers={"content-length": str(meta["size"]), "content-disposition": "attachment",
                                          "content-security-policy": "sandbox; default-src 'none'"})

    @router.post("/recipient/volumes/{volume}/credentials")
    def recipient_credentials(volume: str, request: Request):
        """Temporary read-only store credentials for the shared folders (for Spark and other engines)."""
        return call(sv.recipient_credentials, recipient(request), volume)

    @router.get("/{ref}/volumes")
    def list_(ref: str, p=Depends(current_principal)):
        return call(sv.list, p.urn, operator(p), ref)

    @router.post("/{ref}/volumes")
    def attach(ref: str, volume: str = Body(...), prefix: str = Body(""), p=Depends(current_principal)):
        """Adds a volume, or a folder of one, to the share. Needs the share (its maker, or an operator) and the
        volume (its owner, or an operator)."""
        p = person(p)
        return call(sv.attach, p.urn, operator(p), ref, volume, prefix)

    @router.delete("/{ref}/volumes/{volume}")
    def detach(ref: str, volume: str, prefix: Optional[str] = None, p=Depends(current_principal)):
        p = person(p)
        return call(sv.detach, p.urn, operator(p), ref, volume, prefix)

    return router
