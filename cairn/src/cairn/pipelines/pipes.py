"""Pipes: files that land in a store prefix are loaded into a lakehouse table, once each.

A pipe names a bucket and prefix in Cairn Store, a format (``csv``, ``json`` lines or ``parquet``) and the
Iceberg table it appends to (``namespace.table``, created from the first file when ``create_table``).

With ``files``, the table is a directory of the prefix instead: one row per object (``key``, ``path``,
``size``, ``modified_ms``, ``content_type``, ``sha256``), with the text of documents (``text``, ``pages``,
``extract_error``: text, Markdown, CSV, JSON, HTML, PDF, Word, PowerPoint; see ``documents``). A changed
object replaces its row; with ``s3:ObjectRemoved:*`` events, a deleted one removes it. Files over
``CAIRN_PIPES_MAX_DOCUMENT_BYTES`` (50 MB) are listed without their text. Files are loaded:

* as soon as the store says one arrived: point a store event target (a webhook) at
  ``POST /v1/pipes/events`` with the shared secret (``CAIRN_PIPES_WEBHOOK_SECRET``; the store signs each
  body, ``X-Cairn-Signature: sha256=<hex HMAC>``), and add the bucket's ``s3:ObjectCreated:*`` rule;
* and by a sweep (the scheduler's leader, or ``POST /v1/pipes/{id}/sweep``), which picks up anything an
  event missed.

Each file (its key, size and modification time) is loaded once, recorded in ``cairn_pipe_files`` with
its outcome; a file that failed is retried when it changes. Loaded tables are recorded as changed, so
data-aware schedules (Airflow, dbt) see the new data.

Settings: ``CAIRN_PIPES_STORE`` ({"endpoint", "access_key", "secret_key", "region"}: a read-only key on
the pipes' prefixes) and ``CAIRN_PIPES_CATALOG`` (pyiceberg REST catalog properties: uri, token, and the
warehouse's s3.* settings), both JSON or a path to a JSON file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, and_, delete, select
from starlette.requests import Request  # module level: FastAPI resolves the handler's annotations here

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

pipes = Table(
    "cairn_pipes", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(256), nullable=False, unique=True),
    Column("bucket", String(256), nullable=False),
    Column("prefix", String(1024), nullable=False),
    Column("format", String(16), nullable=False),
    Column("target", String(512), nullable=False),        # namespace.table
    Column("create_table", Boolean, nullable=False, default=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("created_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)

pipe_files = Table(
    "cairn_pipe_files", metadata,
    Column("pipe_id", String(32), primary_key=True),
    Column("key", String(1024), primary_key=True),
    Column("version", String(64), primary_key=True),       # size-mtime: a changed file is a new version
    Column("status", String(16), nullable=False),          # ok | error
    Column("rows", Integer, nullable=True),
    Column("error", String(2000), nullable=True),
    Column("loaded_ms", BigInteger, nullable=False),
)

FORMATS = ("csv", "json", "parquet", "xlsx", "files")
TARGET = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")



def _version(info) -> str:
    """A file's version: its size and modification time, in whole seconds (as milliseconds, the form recorded
    since pipes began). A single object's time comes from its HTTP Last-Modified, which has whole seconds; a
    listing's can have milliseconds (pyarrow 26.0.1 keeps them). Both must give the same version, or a sweep
    would load again every file an event loaded."""
    return f"{info.size}-{int(info.mtime_ns or 0) // 1_000_000_000 * 1000}"

class PipeError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _settings(name: str) -> dict:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    return json.loads(open(raw).read() if not raw.startswith("{") else raw)


def _now() -> int:
    return int(time.time() * 1000)


def _pipe(r) -> dict:
    return {"id": r.id, "name": r.name, "bucket": r.bucket, "prefix": r.prefix, "format": r.format, "target": r.target,
            "create_table": bool(r.create_table), "enabled": bool(r.enabled), "created_by": r.created_by,
            "created_ms": r.created_ms}


def column_name(header, i: int) -> str:
    """A sheet's header as a column name: lowercase letters, digits and _ ("Order ID" is order_id)."""
    name = re.sub(r"[^a-z0-9]+", "_", str(header if header is not None else "").strip().lower()).strip("_")
    if not name:
        name = f"column_{i + 1}"
    return name if name[0].isalpha() or name[0] == "_" else f"c_{name}"


def rows_to_arrow(header: list, rows: list[list]):
    """Sheet rows under a header row, as Arrow: each column typed by its values, text where they mix."""
    import datetime as _dt

    import pyarrow as pa

    names, seen = [], set()
    for i, h in enumerate(header):
        n, k = column_name(h, i), 2
        while n in seen:
            n, k = f"{column_name(h, i)}_{k}", k + 1
        seen.add(n)
        names.append(n)
    cols = {}
    for i, n in enumerate(names):
        vals = [r[i] if i < len(r) else None for r in rows]
        vals = [None if (v is None or v == "") else v for v in vals]
        kinds = {type(v) for v in vals if v is not None}
        if kinds <= {int, bool} and bool not in kinds:
            cols[n] = pa.array(vals, pa.int64())
        elif kinds <= {int, float} and kinds:
            cols[n] = pa.array([None if v is None else float(v) for v in vals], pa.float64())
        elif kinds == {bool}:
            cols[n] = pa.array(vals, pa.bool_())
        elif kinds and kinds <= {_dt.datetime}:
            cols[n] = pa.array(vals, pa.timestamp("us"))
        elif kinds and kinds <= {_dt.date}:
            cols[n] = pa.array(vals, pa.date32())
        else:
            cols[n] = pa.array([None if v is None else str(v) for v in vals], pa.string())
    return pa.table(cols)


def read_xlsx(data: bytes, sheet: Optional[str] = None):
    """An Excel workbook's sheet (the first, unless named) as Arrow; its first row is the header. Formulas give
    the values Excel last saved, not the formulas; rows that are entirely empty are skipped."""
    import io as _io

    try:
        from openpyxl import load_workbook
    except ImportError as e:
        raise PipeError("Excel files need openpyxl (the streams extra)") from e
    wb = load_workbook(_io.BytesIO(data), read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet else wb.worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()
    rows = [r for r in rows if any(v not in (None, "") for v in r)]
    if not rows:
        raise PipeError("the sheet is empty")
    return rows_to_arrow(rows[0], rows[1:])


def open_table(cat, target: str, schema, create: bool):
    """The Iceberg table ``namespace.table``; created with ``schema`` (arrow) when missing and ``create``."""
    from pyiceberg.exceptions import NoSuchTableError

    ns, name = target.split(".")
    try:
        return cat.load_table((ns, name))
    except NoSuchTableError:
        if not create:
            raise PipeError(f"{target} does not exist (create it, or let Cairn create it)")
        try:
            cat.create_namespace(ns)
        except Exception:  # noqa: BLE001 - it exists
            pass
        return cat.create_table((ns, name), schema=schema)


def fit(table, data, target: str):
    """``data`` (arrow) as the table's columns and types; refuses columns the table lacks (they would be
    dropped silently) and missing required ones."""
    import pyarrow as pa

    want = table.schema().as_arrow()
    unknown = [c for c in data.column_names if c not in want.names]
    if unknown:
        raise PipeError(f"the data has columns {target} does not: {', '.join(unknown)}")
    missing = [f.name for f in want if f.name not in data.column_names and not f.nullable]
    if missing:
        raise PipeError(f"the data has no {', '.join(missing)}")
    cols = [data.column(f.name).cast(f.type) if f.name in data.column_names else pa.nulls(len(data), f.type)
            for f in want]
    return pa.Table.from_arrays(cols, schema=want)


class Pipes:
    def __init__(self, engine, store: dict, catalog: dict, audit=None, changes=None,
                 urn: Optional[Callable[[str, str], str]] = None, webhook_secret: str = ""):
        """``store``: the source store (endpoint, access_key, secret_key, region). ``catalog``: pyiceberg
        properties of the catalog loads go to. ``urn(namespace, table)``: the dataset a table is in Cairn."""
        self.engine, self.store, self.catalog_props, self.audit, self.changes = engine, store, catalog, audit, changes
        self.urn = urn or (lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)")
        self.webhook_secret = webhook_secret
        self._locks: dict[str, threading.Lock] = {}

    # ---- definitions --------------------------------------------------------------------
    def create(self, body: dict, actor: str) -> dict:
        row = self._values(body, actor)
        with self.engine.begin() as conn:
            if conn.execute(select(pipes.c.id).where(pipes.c.name == row["name"])).first():
                raise PipeError("a pipe with that name exists", 409)
            conn.execute(pipes.insert().values(**row))
            if self.audit is not None:
                self.audit.record("lakehouse", "pipe.created", actor, self.urn(*row["target"].split(".")), row, conn=conn)
        return row

    def update(self, pid: str, body: dict, actor: str) -> dict:
        """Changes a pipe in place: files already loaded stay loaded."""
        cur = self.get(pid)
        row = {**self._values(body, actor), "id": pid, "created_by": cur["created_by"], "created_ms": cur["created_ms"]}
        with self.engine.begin() as conn:
            if conn.execute(select(pipes.c.id).where(pipes.c.name == row["name"], pipes.c.id != pid)).first():
                raise PipeError("a pipe with that name exists", 409)
            conn.execute(pipes.update().where(pipes.c.id == pid).values(**row))
            if self.audit is not None:
                self.audit.record("lakehouse", "pipe.updated", actor, self.urn(*row["target"].split(".")), row, conn=conn)
        return row

    def _values(self, body: dict, actor: str) -> dict:
        name, bucket = str(body.get("name") or "").strip(), str(body.get("bucket") or "")
        prefix, fmt, target = str(body.get("prefix") or ""), str(body.get("format") or ""), str(body.get("target") or "")
        if not name or not BUCKET.match(bucket):
            raise PipeError("a pipe needs a name and a bucket")
        if fmt not in FORMATS:
            raise PipeError(f"format is one of {', '.join(FORMATS)}")
        if not TARGET.match(target):
            raise PipeError("target is namespace.table")
        if prefix.startswith("/") or ".." in prefix:
            raise PipeError("prefix is a key prefix in the bucket")
        row = {"id": uuid.uuid4().hex, "name": name[:256], "bucket": bucket, "prefix": prefix, "format": fmt, "target": target,
               "create_table": bool(body.get("create_table")), "enabled": bool(body.get("enabled", True)),
               "created_by": actor, "created_ms": _now()}
        return row

    def list(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [_pipe(r) for r in conn.execute(select(pipes).order_by(pipes.c.name))]

    def get(self, pid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(pipes).where(pipes.c.id == pid)).first()
        if r is None:
            raise PipeError("no such pipe", 404)
        return _pipe(r)

    def delete(self, pid: str, actor: str) -> None:
        with self.engine.begin() as conn:
            if not conn.execute(delete(pipes).where(pipes.c.id == pid)).rowcount:
                raise PipeError("no such pipe", 404)
            conn.execute(delete(pipe_files).where(pipe_files.c.pipe_id == pid))
            if self.audit is not None:
                self.audit.record("lakehouse", "pipe.deleted", actor, None, {"pipe": pid}, conn=conn)

    def files(self, pid: str, limit: int = 500) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(pipe_files).where(pipe_files.c.pipe_id == pid)
                                .order_by(pipe_files.c.loaded_ms.desc()).limit(limit)).all()
        return [dict(r._mapping) for r in rows]

    # ---- reading and loading ------------------------------------------------------------
    def _fs(self):
        from pyarrow import fs

        s = self.store
        endpoint = s.get("endpoint", "")
        scheme = "https" if endpoint.startswith("https") else "http"
        return fs.S3FileSystem(access_key=s.get("access_key"), secret_key=s.get("secret_key"), region=s.get("region", "us-east-1"),
                               endpoint_override=endpoint.split("://", 1)[-1], scheme=scheme)

    def _file_row(self, path: str):
        """A files pipe's row for one object: what it is, and what a document says."""
        import mimetypes

        import pyarrow as pa
        from pyarrow import fs as pafs

        from cairn.pipelines.documents import extract

        fs = self._fs()
        info = fs.get_file_info(path)
        if info.type != pafs.FileType.File:
            raise PipeError(f"{path} is not a file", 404)
        limit = int(os.environ.get("CAIRN_PIPES_MAX_DOCUMENT_BYTES", str(50 * 1024 * 1024)))
        digest, data = hashlib.sha256(), bytearray()
        with fs.open_input_stream(path) as f:
            while chunk := f.read(1 << 20):
                digest.update(chunk)
                if len(data) <= limit:
                    data.extend(chunk)
        text, pages, error = (None, None, f"over {limit} bytes: not read for text") if info.size > limit \
            else extract(path, bytes(data))
        key = path.split("/", 1)[1]
        row = {"key": key, "path": f"s3://{path}", "size": int(info.size), "modified_ms": int(info.mtime_ns or 0) // 1_000_000,
               "content_type": mimetypes.guess_type(key)[0] or "application/octet-stream", "sha256": digest.hexdigest(),
               "text": text, "pages": pages, "extract_error": error}
        schema = pa.schema([("key", pa.string()), ("path", pa.string()), ("size", pa.int64()), ("modified_ms", pa.int64()),
                            ("content_type", pa.string()), ("sha256", pa.string()), ("text", pa.large_string()),
                            ("pages", pa.int32()), ("extract_error", pa.string())])
        return pa.Table.from_pylist([row], schema=schema)

    def _read(self, fmt: str, path: str):
        import pyarrow.csv as pacsv
        import pyarrow.json as pajson
        import pyarrow.parquet as pq

        if fmt == "files":
            return self._file_row(path)
        fs = self._fs()
        if fmt == "parquet":
            return pq.read_table(path, filesystem=fs)
        with fs.open_input_stream(path) as f:
            if fmt == "xlsx":
                return read_xlsx(f.read())
            return pacsv.read_csv(f) if fmt == "csv" else pajson.read_json(f)

    def _catalog(self):
        from pyiceberg.catalog.rest import RestCatalog

        return RestCatalog("cairn-pipes", **self.catalog_props)

    def _append(self, pipe: dict, data) -> int:
        table = open_table(self._catalog(), pipe["target"], data.schema, pipe["create_table"])
        if pipe["format"] == "files":
            from pyiceberg.expressions import EqualTo

            # One row per object: its new version replaces the old one, in one commit.
            table.overwrite(fit(table, data, pipe["target"]), overwrite_filter=EqualTo("key", data.column("key")[0].as_py()))
        else:
            table.append(fit(table, data, pipe["target"]))
        return len(data)

    def forget(self, pipe: dict, key: str) -> bool:
        """A files pipe: the row of an object that was deleted goes too."""
        from pyiceberg.exceptions import NoSuchTableError
        from pyiceberg.expressions import EqualTo

        try:
            table = self._catalog().load_table(tuple(pipe["target"].split(".")))
        except NoSuchTableError:
            return False
        table.delete(EqualTo("key", key))
        with self.engine.begin() as conn:
            conn.execute(delete(pipe_files).where(and_(pipe_files.c.pipe_id == pipe["id"], pipe_files.c.key == key)))
        if self.changes is not None:
            self.changes.record([self.urn(*pipe["target"].split("."))], "pipe", f"pipe:{pipe['name']}")
        return True

    def load(self, pipe: dict, key: str, version: str) -> Optional[dict]:
        """Loads one file into the pipe's table, once per version; returns its record (None: already done)."""
        lock = self._locks.setdefault(pipe["id"], threading.Lock())
        with lock:                     # one load at a time per pipe: Iceberg commits are per table
            with self.engine.connect() as conn:
                done = conn.execute(select(pipe_files.c.status).where(and_(
                    pipe_files.c.pipe_id == pipe["id"], pipe_files.c.key == key, pipe_files.c.version == version))).first()
            if done is not None:
                return None
            rows, error = None, None
            try:
                rows = self._append(pipe, self._read(pipe["format"], f"{pipe['bucket']}/{key}"))
            except Exception as e:  # noqa: BLE001 - recorded on the file; the next file still loads
                error = str(e).splitlines()[0][:2000] if str(e) else type(e).__name__
                logger.warning("pipe %s: %s failed: %s", pipe["name"], key, error)
            rec = {"pipe_id": pipe["id"], "key": key, "version": version, "status": "error" if error else "ok",
                   "rows": rows, "error": error, "loaded_ms": _now()}
            with self.engine.begin() as conn:
                conn.execute(pipe_files.insert().values(**rec))
        if not error and self.changes is not None:
            self.changes.record([self.urn(*pipe["target"].split("."))], "pipe", f"pipe:{pipe['name']}")
        return rec

    def sweep(self, pid: str) -> list[dict]:
        """Loads every file under the pipe's prefix not loaded yet."""
        from pyarrow import fs as pafs

        pipe = self.get(pid)
        if not pipe["enabled"]:
            return []
        base = f"{pipe['bucket']}/{pipe['prefix']}".rstrip("/")
        infos = self._fs().get_file_info(pafs.FileSelector(base, recursive=True, allow_not_found=True))
        out = []
        if pipe["format"] == "files":
            # Objects that are gone (an event missed): their rows go too.
            present = {i.path.split("/", 1)[1] for i in infos if i.type == pafs.FileType.File}
            with self.engine.connect() as conn:
                known = {r.key for r in conn.execute(select(pipe_files.c.key).where(pipe_files.c.pipe_id == pipe["id"]))}
            for key in sorted(known - present):
                self.forget(pipe, key)
                out.append({"pipe_id": pipe["id"], "key": key, "status": "removed"})
        for i in sorted((i for i in infos if i.type == pafs.FileType.File), key=lambda i: i.path):
            key = i.path.split("/", 1)[1]
            if key.endswith("/") or os.path.basename(key).startswith((".", "_")):
                continue
            rec = self.load(pipe, key, _version(i))
            if rec is not None:
                out.append(rec)
        return out

    def run_due(self) -> int:
        """Sweeps every enabled pipe (the scheduler's leader calls this)."""
        n = 0
        for p in self.list():
            if p["enabled"]:
                try:
                    n += len(self.sweep(p["id"]))
                except Exception:  # noqa: BLE001 - one pipe never stops the others
                    logger.exception("pipe %s sweep failed", p["name"])
        return n

    # ---- events from the store ----------------------------------------------------------
    def verify(self, body: bytes, signature: str) -> bool:
        if not self.webhook_secret:
            return False
        want = "sha256=" + hmac.new(self.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(want, signature or "")

    def on_events(self, doc: dict) -> list[dict]:
        """Loads each created object that a pipe watches (S3 event records)."""
        loaded = []
        defs = [p for p in self.list() if p["enabled"]]
        for rec in (doc or {}).get("Records") or []:
            event = str(rec.get("eventName", ""))
            s3 = rec.get("s3") or {}
            bucket = (s3.get("bucket") or {}).get("name", "")
            from urllib.parse import unquote_plus

            key = unquote_plus((s3.get("object") or {}).get("key", ""))
            if event.startswith("ObjectRemoved"):
                for p in defs:
                    if p["format"] == "files" and p["bucket"] == bucket and key.startswith(p["prefix"]):
                        self.forget(p, key)
                        loaded.append({"pipe_id": p["id"], "key": key, "status": "removed"})
                continue
            if not event.startswith("ObjectCreated"):
                continue
            for p in defs:
                if p["bucket"] == bucket and key.startswith(p["prefix"]) and not os.path.basename(key).startswith((".", "_")):
                    from pyarrow import fs as pafs

                    info = self._fs().get_file_info(f"{bucket}/{key}")
                    if info.type != pafs.FileType.File:
                        continue
                    rec2 = self.load(p, key, _version(info))
                    if rec2 is not None:
                        loaded.append(rec2)
        return loaded


def build_pipes_router(p: Pipes, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/pipes", tags=["lakehouse"])

    def admin(pr):
        authorize(pr, "MANAGE_SYSTEM_OPERATIONS", None)
        return pr.urn

    def wrap(fn):
        try:
            return fn()
        except PipeError as e:
            raise HTTPException(e.status, str(e))

    @router.post("/events")
    async def events(request: Request):
        """The store's event webhook (signed with the shared secret): loads what just arrived."""
        body = await request.body()
        if not p.verify(body, request.headers.get("x-cairn-signature", "")):
            raise HTTPException(401, "bad signature")
        try:
            doc = json.loads(body)
        except ValueError:
            raise HTTPException(400, "the body is not JSON")
        return {"loaded": await run_in_threadpool(p.on_events, doc)}

    @router.get("")
    def list_pipes(pr=Depends(current_principal)):
        admin(pr)
        return p.list()

    @router.post("", status_code=201)
    def create(body: dict = Body(...), pr=Depends(current_principal)):
        return wrap(lambda: p.create(body, admin(pr)))

    @router.put("/{pid}")
    def update(pid: str, body: dict = Body(...), pr=Depends(current_principal)):
        return wrap(lambda: p.update(pid, body, admin(pr)))

    @router.delete("/{pid}", status_code=204)
    def remove(pid: str, pr=Depends(current_principal)):
        wrap(lambda: p.delete(pid, admin(pr)))

    @router.get("/{pid}/files")
    def files(pid: str, pr=Depends(current_principal)):
        admin(pr)
        return p.files(pid)

    @router.post("/{pid}/sweep")
    async def sweep(pid: str, pr=Depends(current_principal)):
        admin(pr)
        return await run_in_threadpool(lambda: wrap(lambda: p.sweep(pid)))

    return router
