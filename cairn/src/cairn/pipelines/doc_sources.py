"""Document sources: files in SharePoint (or OneDrive) and Google Drive kept in a Cairn Store prefix, so a files pipe
indexes them (text, pages, AI enrichment) and they join SQL, search and AI under Cairn's policies.

``POST /v1/document-sources`` (operators, ``MANAGE_SYSTEM_OPERATIONS``) with ``{"name", "kind": "sharepoint" |
"google_drive", "location": {...}, "bucket", "prefix", "secret_scope", "cron"}``:

* **SharePoint** (Microsoft Graph): ``location`` ``{"drive_id": "...", "folder": "Contracts/2026"}`` (the folder is
  optional: the drive's root). Keys in the secret scope: ``tenant_id``, ``client_id``, ``client_secret`` (an app
  registration with ``Sites.Selected`` granted on the site, or ``Files.Read.All``), and optionally ``graph_url`` and
  ``login_url``.
* **Google Drive**: ``location`` ``{"folder_id": "..."}``. Keys: ``service_account_json`` (share the folder with the
  service account, as a viewer), and optionally ``base_url``. Google Docs, Sheets and Slides are exported as Word,
  CSV and PowerPoint files, so their text is extracted like any other.

Each run (on its cron, or ``POST …/<name>/run``) lists the folder and its subfolders, writes new and changed files
(by version: ETag, checksum or modification time) to ``<bucket>/<prefix><path in the folder>``, and removes the
objects of files deleted or moved away; a moved file is written at its new path. Files over
``CAIRN_DOC_SOURCES_MAX_BYTES`` (100 MB) are skipped and listed. The store is written with
``CAIRN_DOC_SOURCES_STORE`` (``{"endpoint", "access_key", "secret_key", "region"}``, a key that may write the
sources' prefixes). The keys are read from the secret scope as the source's owner at each run, never stored on the
source or logged.

Who may read what is Cairn's, not the source's: everyone the files pipe's table is shared with sees every file
synced, so point a source at a folder whose files that audience may see. Audited: ``document_source.saved``,
``document_source.deleted``, ``document_source.synced`` (counts) and ``document_source.failed``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import Any, Callable, Iterator, Optional
from urllib.parse import quote

import httpx
from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, delete, insert, select, update

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

document_sources = Table(
    "cairn_document_sources", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(64), nullable=False, unique=True),
    Column("kind", String(16), nullable=False),
    Column("location", Text, nullable=False),
    Column("bucket", String(256), nullable=False),
    Column("prefix", String(1024), nullable=False),
    Column("secret_scope", String(128), nullable=False),
    Column("cron", String(100), nullable=True),
    Column("enabled", Boolean, nullable=False),
    Column("owner", String(512), nullable=False),
    Column("status", String(16), nullable=False),            # new | ok | failed
    Column("error", Text, nullable=True),
    Column("files", Integer, nullable=False, default=0),
    Column("last_run_ms", BigInteger, nullable=True),
    Column("next_run_ms", BigInteger, nullable=True),
    Column("created_ms", BigInteger, nullable=False),
)
document_source_items = Table(
    "cairn_document_source_items", metadata,
    Column("source_id", String(32), primary_key=True),
    Column("item_id", String(256), primary_key=True),
    Column("key", String(1024), nullable=False),
    Column("version", String(256), nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("synced_ms", BigInteger, nullable=False),
)

NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
KINDS = ("sharepoint", "google_drive")
REQUIRED = {"sharepoint": ("tenant_id", "client_id", "client_secret"), "google_drive": ("service_account_json",)}
OPTIONAL = {"sharepoint": ("graph_url", "login_url"), "google_drive": ("base_url",)}
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_EXPORTS = {   # Google's own formats, exported as files a pipe reads text from
    "application/vnd.google-apps.document":
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation":
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
}


class DocumentSourceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _url(url: str, what: str) -> str:
    if not url.startswith("https://") and not os.environ.get("CAIRN_SAAS_ALLOW_HTTP"):
        raise DocumentSourceError(f"{what} must be https://")
    return url.rstrip("/")


def _clean(path: str) -> str:
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise DocumentSourceError(f"a path leaves the folder: {path!r}")
    return "/".join(parts)


# ---------------------------------------------------------------------------- listings: (id, path, version, size, fetch)
def sharepoint_files(http: httpx.Client, keys: dict, location: dict) -> Iterator[tuple]:
    login = _url(keys.get("login_url") or "https://login.microsoftonline.com", "sharepoint: login_url")
    graph = _url(keys.get("graph_url") or "https://graph.microsoft.com/v1.0", "sharepoint: graph_url")
    r = http.post(f"{login}/{quote(keys['tenant_id'], safe='')}/oauth2/v2.0/token", data={
        "grant_type": "client_credentials", "client_id": keys["client_id"], "client_secret": keys["client_secret"],
        "scope": f"{graph.split('/v1.0')[0]}/.default"})
    if r.status_code != 200:
        raise DocumentSourceError(f"sharepoint: sign-in refused ({r.status_code})", 502)
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
    drive = quote(str(location["drive_id"]), safe="")
    folder = _clean(str(location.get("folder") or ""))
    start = f"{graph}/drives/{drive}/root" + (f":/{quote(folder)}:" if folder else "") + "/children"
    pending: list[tuple[str, str]] = [(start, "")]
    while pending:
        url, base = pending.pop()
        while url:
            r = http.get(url, headers=auth)
            if r.status_code != 200:
                raise DocumentSourceError(f"sharepoint: listing failed ({r.status_code})", 502)
            page = r.json()
            for it in page.get("value") or []:
                path = f"{base}{it['name']}"
                if "folder" in it:
                    pending.append((f"{graph}/drives/{drive}/items/{quote(it['id'], safe='')}/children", path + "/"))
                elif "file" in it:
                    item = it["id"]

                    def fetch(item=item) -> bytes:
                        got = http.get(f"{graph}/drives/{drive}/items/{quote(item, safe='')}/content", headers=auth,
                                       follow_redirects=True)
                        if got.status_code != 200:
                            raise DocumentSourceError(f"sharepoint: download failed ({got.status_code})", 502)
                        return got.content
                    yield (item, _clean(path), str(it.get("eTag") or it.get("lastModifiedDateTime") or ""),
                           int(it.get("size") or 0), fetch)
            url = page.get("@odata.nextLink")


def google_drive_files(http: httpx.Client, keys: dict, location: dict) -> Iterator[tuple]:
    from cairn.pipelines.saas import SaasError, google_session

    try:
        base, headers, params = google_session(http, keys, DRIVE_SCOPE, "https://www.googleapis.com", "google_drive")
    except SaasError as e:
        raise DocumentSourceError(str(e), e.status)
    pending: list[tuple[str, str]] = [(str(location["folder_id"]), "")]
    while pending:
        folder, prefix = pending.pop()
        token = None
        while True:
            q = {**params, "q": f"'{folder}' in parents and trashed = false", "pageSize": 1000,
                 "fields": "nextPageToken, files(id, name, mimeType, modifiedTime, size, md5Checksum)",
                 "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if token:
                q["pageToken"] = token
            r = http.get(f"{base}/drive/v3/files", headers=headers, params=q)
            if r.status_code != 200:
                raise DocumentSourceError(f"google_drive: listing failed ({r.status_code})", 502)
            page = r.json()
            for f in page.get("files") or []:
                mime, name = f.get("mimeType", ""), f["name"]
                if mime == "application/vnd.google-apps.folder":
                    pending.append((f["id"], f"{prefix}{name}/"))
                    continue
                if mime.startswith("application/vnd.google-apps.") and mime not in GOOGLE_EXPORTS:
                    continue                                   # forms, maps, shortcuts: nothing to read
                export = GOOGLE_EXPORTS.get(mime)
                fid = f["id"]

                def fetch(fid=fid, export=export) -> bytes:
                    if export:
                        got = http.get(f"{base}/drive/v3/files/{quote(fid, safe='')}/export", headers=headers,
                                       params={**params, "mimeType": export[0]})
                    else:
                        got = http.get(f"{base}/drive/v3/files/{quote(fid, safe='')}", headers=headers,
                                       params={**params, "alt": "media", "supportsAllDrives": "true"})
                    if got.status_code != 200:
                        raise DocumentSourceError(f"google_drive: download failed ({got.status_code})", 502)
                    return got.content
                path = f"{prefix}{name}" + (export[1] if export and not name.endswith(export[1]) else "")
                yield (fid, _clean(path), str(f.get("md5Checksum") or f.get("modifiedTime") or ""),
                       int(f.get("size") or 0), fetch)
            token = page.get("nextPageToken")
            if not token:
                break


LISTERS = {"sharepoint": sharepoint_files, "google_drive": google_drive_files}


# ---------------------------------------------------------------------------- the service
class DocumentSources:
    def __init__(self, engine, store: dict, read_secret: Callable[[str, str, str], str], audit=None,
                 changes=None, clock: Callable[[], float] = time.time):
        """``store``: a key that may write the sources' prefixes; ``read_secret(owner, scope, key)``: a secret, read
        as the owner."""
        self.engine, self.store, self.read_secret, self.audit, self.changes, self.clock = (
            engine, store, read_secret, audit, changes, clock)
        self.max_bytes = int(os.environ.get("CAIRN_DOC_SOURCES_MAX_BYTES", str(100 * 1024 * 1024)))
        self.timeout = float(os.environ.get("CAIRN_SAAS_HTTP_TIMEOUT", "60"))

    def _ms(self) -> int:
        return int(self.clock() * 1000)

    def _s3(self):
        """The store, written object by object (no directory markers, which a files pipe would list as files)."""
        import boto3
        from botocore.config import Config

        s = self.store
        return boto3.client("s3", endpoint_url=str(s["endpoint"]), aws_access_key_id=s.get("access_key"),
                            aws_secret_access_key=s.get("secret_key"), region_name=s.get("region", "us-east-1"),
                            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}))

    def _check(self, body: dict) -> dict:
        kind = body.get("kind")
        if kind not in KINDS:
            raise DocumentSourceError("kind: sharepoint or google_drive")
        loc = body.get("location")
        if not isinstance(loc, dict):
            raise DocumentSourceError("location: an object")
        if kind == "sharepoint" and not str(loc.get("drive_id") or ""):
            raise DocumentSourceError("location: {drive_id, folder}")
        if kind == "google_drive" and not re.match(r"^[A-Za-z0-9_-]{1,200}$", str(loc.get("folder_id") or "")):
            raise DocumentSourceError("location: {folder_id}")
        if kind == "sharepoint":
            loc = {"drive_id": str(loc["drive_id"]), "folder": _clean(str(loc.get("folder") or ""))}
        else:
            loc = {"folder_id": str(loc["folder_id"])}
        bucket, prefix = str(body.get("bucket") or ""), str(body.get("prefix") or "")
        if not BUCKET.match(bucket) or prefix.startswith("/") or ".." in prefix or (prefix and not prefix.endswith("/")):
            raise DocumentSourceError("bucket, and prefix: a key prefix ending in /")
        scope = str(body.get("secret_scope") or "")
        if not scope:
            raise DocumentSourceError("secret_scope: the scope that holds the source's keys")
        cron = body.get("cron") or None
        if cron is not None:
            from croniter import croniter

            if not isinstance(cron, str) or not croniter.is_valid(cron):
                raise DocumentSourceError("cron: a cron expression (UTC)")
        return {"kind": kind, "location": json.dumps(loc), "bucket": bucket, "prefix": prefix, "secret_scope": scope,
                "cron": cron, "enabled": bool(body.get("enabled", True))}

    def _next(self, cron: Optional[str], after_ms: int) -> Optional[int]:
        if not cron:
            return None
        from datetime import datetime, timezone

        from croniter import croniter

        base = datetime.fromtimestamp(after_ms / 1000, tz=timezone.utc)
        return int(croniter(cron, base).get_next(datetime).timestamp() * 1000)

    def save(self, actor: str, body: dict, name: Optional[str] = None) -> dict:
        if not isinstance(body, dict):
            raise DocumentSourceError("a document source is an object")
        v = self._check(body)
        now = self._ms()
        with self.engine.begin() as c:
            if name is None:
                name = str(body.get("name") or "")
                if not NAME.match(name):
                    raise DocumentSourceError("name: lowercase letters, digits and -")
                if c.execute(select(document_sources.c.id).where(document_sources.c.name == name)).first():
                    raise DocumentSourceError(f"{name} exists", 409)
                c.execute(insert(document_sources).values(id=uuid.uuid4().hex, name=name, owner=actor, status="new",
                                                          error=None, files=0, created_ms=now,
                                                          next_run_ms=self._next(v["cron"], now), **v))
            else:
                self._get(name)
                c.execute(update(document_sources).where(document_sources.c.name == name).values(
                    next_run_ms=self._next(v["cron"], now), **v))
        self._audit(actor, "document_source.saved", name, {"kind": v["kind"], "bucket": v["bucket"],
                                                          "prefix": v["prefix"]})
        return self.get(name)

    def _get(self, name: str) -> dict:
        with self.engine.connect() as c:
            r = c.execute(select(document_sources).where(document_sources.c.name == name)).first()
        if r is None:
            raise DocumentSourceError("no such document source", 404)
        return {**dict(r._mapping), "location": json.loads(r.location), "enabled": bool(r.enabled)}

    def get(self, name: str) -> dict:
        return self._get(name)

    def list(self) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(document_sources.c.name).order_by(document_sources.c.name)).all()
        return [self._get(r.name) for r in rows]

    def items(self, name: str) -> list[dict]:
        s = self._get(name)
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(select(document_source_items).where(
                document_source_items.c.source_id == s["id"]).order_by(document_source_items.c.key))]

    def delete(self, actor: str, name: str) -> None:
        """Forgets the source (the files it wrote stay in the store, where the pipe has them)."""
        s = self._get(name)
        with self.engine.begin() as c:
            c.execute(delete(document_source_items).where(document_source_items.c.source_id == s["id"]))
            c.execute(delete(document_sources).where(document_sources.c.id == s["id"]))
        self._audit(actor, "document_source.deleted", name, {})

    def _keys(self, s: dict) -> dict[str, str]:
        out = {}
        for k in REQUIRED[s["kind"]]:
            try:
                out[k] = self.read_secret(s["owner"], s["secret_scope"], k)
            except Exception as e:  # noqa: BLE001 - no scope, no key or no right: fail closed
                raise DocumentSourceError(f"cannot read {k} from secret scope {s['secret_scope']} as its owner: {e}", 403)
        for k in OPTIONAL[s["kind"]]:
            try:
                out[k] = self.read_secret(s["owner"], s["secret_scope"], k)
            except Exception as e:  # noqa: BLE001 - absent optional keys are fine
                if str(e) != "no such secret":
                    raise DocumentSourceError(f"cannot read {k} from secret scope {s['secret_scope']}: {e}", 403)
        return out

    def run(self, name: str) -> dict:
        s = self._get(name)
        started = self._ms()
        counts = {"written": 0, "deleted": 0, "unchanged": 0, "skipped": []}
        try:
            keys = self._keys(s)
            s3 = self._s3()
            with self.engine.connect() as c:
                known = {r.item_id: dict(r._mapping) for r in c.execute(select(document_source_items).where(
                    document_source_items.c.source_id == s["id"]))}
            seen: set[str] = set()
            with httpx.Client(timeout=self.timeout) as http:
                for item, path, version, size, fetch in LISTERS[s["kind"]](http, keys, s["location"]):
                    key = f"{s['bucket']}/{s['prefix']}{path}"
                    if size > self.max_bytes:
                        counts["skipped"].append({"path": path, "size": size})
                        continue
                    seen.add(item)
                    was = known.get(item)
                    if was is not None and was["version"] == version and was["key"] == key:
                        counts["unchanged"] += 1
                        continue
                    data = fetch()
                    if len(data) > self.max_bytes:
                        seen.discard(item)
                        counts["skipped"].append({"path": path, "size": len(data)})
                        continue
                    bucket, _, obj = key.partition("/")
                    s3.put_object(Bucket=bucket, Key=obj, Body=data)
                    if was is not None and was["key"] != key:           # moved: the old object goes
                        self._remove(s3, was["key"])
                    with self.engine.begin() as c:
                        c.execute(delete(document_source_items).where(document_source_items.c.source_id == s["id"],
                                                                      document_source_items.c.item_id == item))
                        c.execute(insert(document_source_items).values(source_id=s["id"], item_id=item, key=key,
                                                                       version=version, size=len(data),
                                                                       synced_ms=self._ms()))
                    counts["written"] += 1
            for item, was in known.items():
                if item not in seen:                                  # deleted, or moved out of the folder
                    self._remove(s3, was["key"])
                    with self.engine.begin() as c:
                        c.execute(delete(document_source_items).where(document_source_items.c.source_id == s["id"],
                                                                      document_source_items.c.item_id == item))
                    counts["deleted"] += 1
        except Exception as e:  # noqa: BLE001 - recorded on the source, tried again next time
            msg = str(e) if isinstance(e, DocumentSourceError) else f"{type(e).__name__}: {e}"
            with self.engine.begin() as c:
                c.execute(update(document_sources).where(document_sources.c.id == s["id"]).values(
                    status="failed", error=msg[:2000], last_run_ms=started))
            self._audit(s["owner"], "document_source.failed", name, {"error": msg[:300]})
            logger.warning("document source %s failed: %s", name, msg)
            return {**counts, "status": "failed", "error": msg}
        with self.engine.begin() as c:
            files = len(c.execute(select(document_source_items.c.item_id).where(
                document_source_items.c.source_id == s["id"])).all())
            c.execute(update(document_sources).where(document_sources.c.id == s["id"]).values(
                status="ok", error=None, last_run_ms=started, files=files))
        self._audit(s["owner"], "document_source.synced", name,
                    {k: (len(v) if isinstance(v, list) else v) for k, v in counts.items()})
        return {**counts, "status": "ok", "error": None}

    @staticmethod
    def _remove(s3, key: str) -> None:
        bucket, _, obj = key.partition("/")
        s3.delete_object(Bucket=bucket, Key=obj)                 # deleting what is gone already succeeds

    def run_due(self, now_ms: Optional[int] = None) -> int:
        now = now_ms if now_ms is not None else self._ms()
        with self.engine.connect() as c:
            due = c.execute(select(document_sources.c.name, document_sources.c.next_run_ms, document_sources.c.cron)
                            .where(document_sources.c.enabled.is_(True), document_sources.c.next_run_ms.is_not(None),
                                   document_sources.c.next_run_ms <= now)).all()
        n = 0
        for r in due:
            with self.engine.begin() as c:                       # claimed: one scheduler runs it
                got = c.execute(update(document_sources).where(document_sources.c.name == r.name,
                                                               document_sources.c.next_run_ms == r.next_run_ms)
                                .values(next_run_ms=self._next(r.cron, now))).rowcount
            if got:
                self.run(r.name)
                n += 1
        return n

    def _audit(self, actor: str, action: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("data", action, actor, f"urn:li:documentSource:{name}", details)


def build_document_sources_router(d: DocumentSources, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/document-sources", tags=["pipelines"])

    def admin(p) -> str:
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return p.urn

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except DocumentSourceError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        admin(p)
        return await call(d.list)

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        actor = admin(p)
        return await call(lambda: d.save(actor, body))

    @router.get("/{name}")
    async def get(name: str, p=Depends(current_principal)):
        admin(p)
        return await call(lambda: {**d.get(name), "items": d.items(name)})

    @router.put("/{name}")
    async def change(name: str, body: dict = Body(...), p=Depends(current_principal)):
        actor = admin(p)
        return await call(lambda: d.save(actor, body, name))

    @router.post("/{name}/run")
    async def run(name: str, p=Depends(current_principal)):
        admin(p)
        return await call(lambda: d.run(name))

    @router.delete("/{name}", status_code=204)
    async def delete_(name: str, p=Depends(current_principal)):
        actor = admin(p)
        await call(lambda: d.delete(actor, name))

    return router
