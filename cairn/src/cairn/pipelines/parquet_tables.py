"""Zero-copy Parquet: Parquet files already in Cairn Store registered as an Iceberg table, without copying them.

``POST /v1/lakehouse/parquet-tables`` (``MANAGE_INGESTION``) with ``{"name", "bucket", "prefix", "target":
"namespace.table", "every_minutes"}`` lists the ``.parquet`` files under the prefix (names starting with ``_`` or
``.`` are skipped, as Spark and Hive markers), and:

* **Checks where they are and who asks.** The bucket must be one the lakehouse registers files from
  (``CAIRN_PARQUET_BUCKETS``, comma-separated; none set: nothing is registered). The person must be able to read
  the files: for a governed volume's bucket (``cairn-vol-<name>``) a read or write grant on the volume; for any
  other bucket ``READ_FILES`` on ``urn:li:storagePath:<bucket>/<prefix>`` (Cairn's policies; no policy, no read).
* **Infers the schema and checks it across files.** Every file's footer is read (only the footer: no data is
  read or copied); all must have the same columns and types, else the registration is refused naming the file
  that differs. Files written with Iceberg field ids (another table's files) are refused.
* **Partitions from Hive-style paths.** ``…/region=EU/day=2026-11-01/part-0.parquet``: each ``key=value``
  directory becomes an identity-partitioned column (a whole number when every value is one, else text;
  ``__HIVE_DEFAULT_PARTITION__`` is null). Every file must have the same keys in the same order, and a key may not
  also be a column in the files.
* **Registers without copying.** The table is created in Cairn Catalog with that schema and partitioning, a
  name mapping (the files have no field ids) and ``gc.enabled=false`` (maintenance never deletes the files: they
  were there before the table), and the files are added to it as they are, by reference, with their statistics
  read from the footers, in one commit. Readers then read the original objects.
* **Lineage.** The table's upstream is the store prefix (``urn:li:dataset:(urn:li:dataPlatform:s3,<bucket>/<prefix>,PROD)``)
  in Cairn's catalog, and the table is recorded as changed (data-aware schedules see it).
* **Incrementally.** ``POST …/<name>/refresh``, and the scheduler every ``every_minutes``, add the files that
  appeared since (checked against the table's schema and partition keys, as the person who registered it, whose
  read access is checked again). Files already in the table are left as they are; a file that disappeared or
  changed size in place is reported in ``problems`` (Iceberg files are immutable: replace a file under a new
  name). The table is then governed and queried like any other lakehouse table.

Reads the store with ``CAIRN_PARQUET_STORE`` (default ``CAIRN_PIPES_STORE``: a key that may read the registered
buckets) and writes the catalog with ``CAIRN_PARQUET_CATALOG`` (default ``CAIRN_PIPES_CATALOG``). Audited:
``parquet_table.registered``, ``parquet_table.refreshed``, ``parquet_table.updated``, ``parquet_table.deleted``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Optional
from urllib.parse import unquote

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, delete, select, update

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

parquet_tables = Table(
    "cairn_parquet_tables", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(128), nullable=False, unique=True),
    Column("target", String(512), nullable=False),          # namespace.table
    Column("bucket", String(63), nullable=False),
    Column("prefix", String(1024), nullable=False),
    Column("partition_columns", Text, nullable=False),        # JSON [[name, "long" | "string"], ...]
    Column("file_schema", Text, nullable=False),              # JSON [[column, Arrow type], ...]: what new files must be
    Column("every_minutes", Integer, nullable=True),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("created_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("registered_ms", BigInteger, nullable=True),
    Column("files", Integer, nullable=True),
    Column("rows", BigInteger, nullable=True),
    Column("problems", Text, nullable=True),                 # JSON [str]: files gone or changed in place
    Column("error", String(2000), nullable=True),
)

NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
TARGET = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
HIVE_NULL = "__HIVE_DEFAULT_PARTITION__"
READ_FILES = "READ_FILES"


class ParquetError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def path_urn(bucket: str, prefix: str) -> str:
    """The resource ``READ_FILES`` is decided on."""
    return f"urn:li:storagePath:{bucket}/{prefix}"


def source_urn(bucket: str, prefix: str) -> str:
    """The store prefix as a dataset in Cairn's catalog (the table's upstream)."""
    return f"urn:li:dataset:(urn:li:dataPlatform:s3,{bucket}/{prefix.rstrip('/')},PROD)"


def partition_keys(prefix: str, key: str) -> list[tuple[str, Optional[str]]]:
    """``[(name, value)]`` from the ``name=value`` directories between the prefix and the file."""
    out = []
    for seg in key[len(prefix):].split("/")[:-1]:
        if not seg:
            continue
        if "=" not in seg:
            raise ParquetError(f"{key}: directory {seg!r} is not name=value (Hive-style partitions)")
        k, v = seg.split("=", 1)
        k, v = unquote(k), unquote(v)
        if not COLUMN.match(k):
            raise ParquetError(f"{key}: {k!r} is not a column name")
        out.append((k, None if v == HIVE_NULL else v))
    return out


def _is_int(v: Optional[str]) -> bool:
    return v is None or bool(re.fullmatch(r"-?\d{1,18}", v))


class ParquetTables:
    def __init__(self, engine, store: dict, catalog: dict, audit=None, graph=None,
                 urn: Optional[Callable[[str, str], str]] = None, buckets: Optional[list[str]] = None,
                 may_read: Optional[Callable[[str, str, str], bool]] = None, changes=None):
        """``may_read(actor, bucket, prefix)``: whether the person may read those files (Cairn's decision).
        ``buckets``: the buckets files may be registered from."""
        self.engine, self.store, self.catalog_props, self.audit, self.graph = engine, store, catalog, audit, graph
        self.urn = urn or (lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)")
        self.buckets = [b for b in (buckets or []) if b]
        self.may_read = may_read
        self.changes = changes
        self._locks: dict[str, threading.Lock] = {}
        self.max_files = int(os.environ.get("CAIRN_PARQUET_MAX_FILES", "10000"))

    # ---- the store and the catalog ------------------------------------------------------------
    def _fs(self):
        from pyarrow import fs

        s = self.store
        endpoint = s.get("endpoint", "")
        return fs.S3FileSystem(access_key=s.get("access_key"), secret_key=s.get("secret_key"),
                               region=s.get("region", "us-east-1"), endpoint_override=endpoint.split("://", 1)[-1],
                               scheme="https" if endpoint.startswith("https") else "http")

    def _catalog(self):
        from pyiceberg.catalog.rest import RestCatalog

        from cairn.pipelines.iceberg_v3 import enable

        enable()
        return RestCatalog("cairn-parquet", **self.catalog_props)

    def _check_access(self, actor: str, bucket: str, prefix: str) -> None:
        if bucket not in self.buckets:
            raise ParquetError(f"files in {bucket} are not registered: the lakehouse registers files from "
                               f"{', '.join(self.buckets) or 'no bucket'} (CAIRN_PARQUET_BUCKETS)", 403)
        if self.may_read is None or not self.may_read(actor, bucket, prefix):
            raise ParquetError(f"you may not read {bucket}/{prefix}: registering files needs read access to them "
                               f"({READ_FILES} on {path_urn(bucket, prefix)}, or a grant on the volume)", 403)

    def _list(self, bucket: str, prefix: str) -> list[dict]:
        """The Parquet files under the prefix: [{key, path, size}], sorted by key."""
        from pyarrow import fs as pafs

        try:
            infos = self._fs().get_file_info(pafs.FileSelector(f"{bucket}/{prefix}".rstrip("/"), recursive=True,
                                                               allow_not_found=True))
        except OSError as e:
            raise ParquetError(f"the store cannot be listed at {bucket}/{prefix}: {str(e)[:300]}", 502)
        out = []
        for i in infos:
            if i.type != pafs.FileType.File or not i.path.endswith(".parquet"):
                continue
            key = i.path.split("/", 1)[1]
            if not key.startswith(prefix) or any(seg.startswith(("_", ".")) for seg in key[len(prefix):].split("/")):
                continue
            out.append({"key": key, "path": f"s3://{bucket}/{key}", "size": int(i.size)})
        if len(out) > self.max_files:
            raise ParquetError(f"{len(out)} files under {bucket}/{prefix}: at most {self.max_files} a registration "
                               "(CAIRN_PARQUET_MAX_FILES)", 413)
        return sorted(out, key=lambda f: f["key"])

    def _footer(self, bucket: str, f: dict):
        import pyarrow.parquet as pq

        try:
            with self._fs().open_input_file(f"{bucket}/{f['key']}") as h:
                md = pq.read_metadata(h)
        except Exception as e:  # noqa: BLE001 - not Parquet, or unreadable
            raise ParquetError(f"{f['key']} is not a readable Parquet file: {str(e)[:300]}")
        schema = md.schema.to_arrow_schema()
        if any((fld.metadata or {}).get(b"PARQUET:field_id") for fld in schema):
            raise ParquetError(f"{f['key']} was written with Iceberg field ids (another table's file): not registered")
        return md, schema.remove_metadata()

    @staticmethod
    def _signature(schema) -> list[list[str]]:
        return [[x.name, str(x.type)] for x in schema]

    def _scan(self, bucket: str, prefix: str, files: list[dict], expect: Optional[list] = None,
              expect_parts: Optional[list[str]] = None) -> tuple[Any, list, list[dict]]:
        """Footers and partition values of ``files``, checked against each other (and the registration's, when
        given: ``expect`` the files' columns and types, ``expect_parts`` the partition keys)."""
        first, out, parts_names, against = None, [], expect_parts, "the table's files"
        for f in files:
            md, schema = self._footer(bucket, f)
            if first is None and expect is None:
                first, against = schema, f["key"]
            sig, want = self._signature(schema), expect if expect is not None else self._signature(first)
            if sig != want:
                raise ParquetError(f"the files disagree on their schema: {f['key']} has "
                                   f"({', '.join(' '.join(c) for c in sig)}) where {against} has "
                                   f"({', '.join(' '.join(c) for c in want)})")
            keys = partition_keys(prefix, f["key"])
            names = [k for k, _ in keys]
            if parts_names is None:
                parts_names = names
            elif names != parts_names:
                raise ParquetError(f"{f['key']}: partition directories {names or 'none'} where the others have "
                                   f"{parts_names or 'none'}")
            out.append({**f, "md": md, "values": [v for _, v in keys]})
        return first, parts_names or [], out

    # ---- registrations --------------------------------------------------------------------------
    def register(self, body: dict, actor: str) -> dict:
        import pyarrow as pa
        from pyiceberg.exceptions import NoSuchTableError

        name = str(body.get("name") or "").strip()
        bucket, prefix = str(body.get("bucket") or ""), str(body.get("prefix") or "")
        target = str(body.get("target") or "")
        if not NAME.match(name):
            raise ParquetError("name: lowercase letters, digits, '-' and '_' (64 at most)")
        if not BUCKET.match(bucket):
            raise ParquetError("bucket: the store bucket the files are in")
        if prefix.startswith("/") or ".." in prefix or (prefix and not prefix.endswith("/")):
            raise ParquetError("prefix: a folder in the bucket, ending in '/' (or empty for the whole bucket)")
        if not TARGET.match(target):
            raise ParquetError("target: namespace.table")
        every = body.get("every_minutes")
        if every is not None and (isinstance(every, bool) or not isinstance(every, int) or not 5 <= every <= 10080):
            raise ParquetError("every_minutes: 5 to 10080, or null for on demand")
        with self.engine.connect() as conn:
            if conn.execute(select(parquet_tables.c.id).where(parquet_tables.c.name == name)).first():
                raise ParquetError("a registration with that name exists", 409)
        self._check_access(actor, bucket, prefix)
        files = self._list(bucket, prefix)
        if not files:
            raise ParquetError(f"no Parquet files under {bucket}/{prefix}", 404)
        schema, part_names, scanned = self._scan(bucket, prefix, files)
        clash = sorted(set(part_names) & {f.name for f in schema})
        if clash:
            raise ParquetError(f"{', '.join(clash)}: both a column in the files and a partition directory")
        part_types = [("long" if all(_is_int(s["values"][i]) for s in scanned) else "string") for i in range(len(part_names))]
        for s in scanned:
            s["values"] = [None if v is None else (int(v) if t == "long" else v) for v, t in zip(s["values"], part_types)]
        full = pa.schema(list(schema) + [pa.field(n, pa.int64() if t == "long" else pa.string(), nullable=True)
                                         for n, t in zip(part_names, part_types)])
        ns, tname = target.split(".")
        cat = self._catalog()
        try:
            cat.load_table((ns, tname))
            raise ParquetError(f"{target} exists: files are registered as a new table", 409)
        except NoSuchTableError:
            pass
        cat.create_namespace_if_not_exists(ns)
        try:
            table = cat.create_table((ns, tname), schema=full, properties={"gc.enabled": "false"})
        except Exception as e:  # noqa: BLE001 - a type Iceberg has no counterpart for, or the catalog refused
            raise ParquetError(f"{target} could not be created: {str(e)[:400]}")
        try:
            if part_names:
                with table.update_spec() as spec:
                    for n in part_names:
                        spec.add_identity(n)
                table = cat.load_table((ns, tname))
            added, rows = self._add(table, scanned, {"cairn.parquet-registration": name})
        except Exception:
            cat.drop_table((ns, tname))                 # nothing half-registered: the files stay as they were
            raise
        row = {"id": uuid.uuid4().hex, "name": name, "target": target, "bucket": bucket, "prefix": prefix,
               "partition_columns": json.dumps([[n, t] for n, t in zip(part_names, part_types)]),
               "file_schema": json.dumps(self._signature(schema)),
               "every_minutes": every, "enabled": bool(body.get("enabled", True)), "created_by": actor,
               "created_ms": _now(), "registered_ms": _now(), "files": added, "rows": rows, "problems": None,
               "error": None}
        urn = self.urn(ns, tname)
        with self.engine.begin() as conn:
            conn.execute(parquet_tables.insert().values(**row))
            if self.audit is not None:
                self.audit.record("lakehouse", "parquet_table.registered", actor, urn, {
                    "name": name, "bucket": bucket, "prefix": prefix, "files": added, "rows": rows,
                    "partitions": part_names}, conn=conn)
        self._lineage(urn, bucket, prefix, actor)
        if self.changes is not None:
            self.changes.record([urn], "parquet", f"parquet:{name}")
        return {**self._public(row), "added": added}

    def _add(self, table, scanned: list[dict], props: dict) -> tuple[int, int]:
        """Adds the files to the table by reference, statistics from their footers, in one commit."""
        from pyiceberg.io.pyarrow import compute_statistics_plan, data_file_statistics_from_parquet_metadata, \
            parquet_path_to_id_mapping
        from pyiceberg.manifest import DataFile, DataFileContent, FileFormat
        from pyiceberg.table import TableProperties
        from pyiceberg.typedef import Record

        if not scanned:
            return 0, 0
        meta = table.metadata
        schema = meta.schema()
        files, rows = [], 0
        for s in scanned:
            stats = data_file_statistics_from_parquet_metadata(
                parquet_metadata=s["md"], stats_columns=compute_statistics_plan(schema, meta.properties),
                parquet_column_mapping=parquet_path_to_id_mapping(schema))
            files.append(DataFile.from_args(
                content=DataFileContent.DATA, file_path=s["path"], file_format=FileFormat.PARQUET,
                partition=Record(*s["values"]), file_size_in_bytes=s["size"], sort_order_id=None,
                spec_id=meta.default_spec_id, equality_ids=None, key_metadata=None, **stats.to_serialized_dict()))
            rows += int(s["md"].num_rows)
        with table.transaction() as tx:
            if meta.name_mapping() is None:              # the files have no field ids: columns are found by name
                tx.set_properties(**{TableProperties.DEFAULT_NAME_MAPPING: schema.name_mapping.model_dump_json()})
            with tx.update_snapshot(snapshot_properties=props).fast_append() as append:
                for df in files:
                    append.append_data_file(df)
        return len(files), rows

    def _lineage(self, urn: str, bucket: str, prefix: str, actor: str) -> None:
        if self.graph is None:
            return
        from cairn.common.proposals import make_mcp

        try:
            src = source_urn(bucket, prefix)
            cur = self.graph.get_aspect(urn, "upstreamLineage") or {}
            ups = [u for u in (cur.get("upstreams") or []) if u.get("dataset") != src]
            ups.append({"dataset": src, "type": "COPY", "properties": {"cairn.parquet": "registered"},
                        "auditStamp": {"time": _now(), "actor": actor}})
            self.graph.emit([make_mcp(urn, "upstreamLineage", {**cur, "upstreams": ups})])
        except Exception as e:  # noqa: BLE001 - the table is registered; lineage is reported, not fatal
            logger.warning("parquet table %s: lineage not recorded: %s", urn, e)

    def refresh(self, name: str) -> dict:
        """Adds the files that appeared under the prefix since, as the person who registered them."""
        reg = self.get(name)
        lock = self._locks.setdefault(reg["id"], threading.Lock())
        if not lock.acquire(blocking=False):
            raise ParquetError(f"{name} is being registered already", 409)
        try:
            return self._refresh(reg)
        except ParquetError as e:
            self._record(reg["id"], error=str(e)[:2000])
            raise
        finally:
            lock.release()

    def _refresh(self, reg: dict) -> dict:
        self._check_access(reg["created_by"], reg["bucket"], reg["prefix"])
        ns, tname = reg["target"].split(".")
        table = self._catalog().load_table((ns, tname))
        known = {}
        for f in table.inspect.files().select(["file_path", "file_size_in_bytes"]).to_pylist():
            known[f["file_path"]] = f["file_size_in_bytes"]
        files = self._list(reg["bucket"], reg["prefix"])
        listed = {f["path"]: f for f in files}
        new = [f for f in files if f["path"] not in known]
        problems = [f"{p.split('/', 3)[-1]} is gone from the store but still in the table" for p in known if p not in listed]
        problems += [f"{f['key']} changed in place ({known[f['path']]} -> {f['size']} bytes): register a new file "
                     "under a new name" for f in files if f["path"] in known and known[f["path"]] != f["size"]]
        parts = [tuple(p) for p in reg["partition_columns"]]
        part_names = [n for n, _ in parts]
        added = rows = 0
        if new:
            _, _, scanned = self._scan(reg["bucket"], reg["prefix"], new, reg["file_schema"], part_names)
            for s in scanned:
                vals = []
                for v, (n, t) in zip(s["values"], parts):
                    if t == "long" and not _is_int(v):
                        raise ParquetError(f"{s['key']}: partition {n}={v} is not a whole number like the others")
                    vals.append(None if v is None else (int(v) if t == "long" else v))
                s["values"] = vals
            added, rows = self._add(table, scanned, {"cairn.parquet-registration": reg["name"]})
        total = self._record(reg["id"], registered_ms=_now(), error=None, problems=json.dumps(problems) if problems else None,
                             files_delta=added, rows_delta=rows)
        urn = self.urn(ns, tname)
        if self.audit is not None:
            self.audit.record("lakehouse", "parquet_table.refreshed", reg["created_by"], urn,
                              {"name": reg["name"], "added": added, "rows": rows, "problems": len(problems)})
        if added and self.changes is not None:
            self.changes.record([urn], "parquet", f"parquet:{reg['name']}")
        return {"name": reg["name"], "added": added, "rows_added": rows, "files": total["files"],
                "rows": total["rows"], "problems": problems}

    def _record(self, rid: str, files_delta: int = 0, rows_delta: int = 0, **values) -> dict:
        with self.engine.begin() as conn:
            cur = conn.execute(select(parquet_tables.c.files, parquet_tables.c.rows).where(parquet_tables.c.id == rid)).first()
            files = (cur.files or 0) + files_delta if cur else files_delta
            rows = (cur.rows or 0) + rows_delta if cur else rows_delta
            conn.execute(update(parquet_tables).where(parquet_tables.c.id == rid).values(files=files, rows=rows, **values))
        return {"files": files, "rows": rows}

    @staticmethod
    def _public(r) -> dict:
        d = dict(r) if isinstance(r, dict) else dict(r._mapping)
        d["partition_columns"] = json.loads(d["partition_columns"] or "[]")
        d["file_schema"] = json.loads(d["file_schema"] or "[]")
        d["problems"] = json.loads(d["problems"]) if d.get("problems") else []
        return d

    def list(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [self._public(r) for r in conn.execute(select(parquet_tables).order_by(parquet_tables.c.name))]

    def get(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(parquet_tables).where(
                (parquet_tables.c.name == name) | (parquet_tables.c.id == name))).first()
        if r is None:
            raise ParquetError("no such registration", 404)
        return self._public(r)

    def update(self, name: str, body: dict, actor: str) -> dict:
        reg = self.get(name)
        if set(body) - {"every_minutes", "enabled"}:
            raise ParquetError("only every_minutes and enabled change: register the files again for anything else")
        values: dict[str, Any] = {}
        if "every_minutes" in body:
            every = body["every_minutes"]
            if every is not None and (isinstance(every, bool) or not isinstance(every, int) or not 5 <= every <= 10080):
                raise ParquetError("every_minutes: 5 to 10080, or null for on demand")
            values["every_minutes"] = every
        if "enabled" in body:
            values["enabled"] = bool(body["enabled"])
        with self.engine.begin() as conn:
            conn.execute(update(parquet_tables).where(parquet_tables.c.id == reg["id"]).values(**values))
            if self.audit is not None:
                self.audit.record("lakehouse", "parquet_table.updated", actor, None, {"name": reg["name"], **values},
                                  conn=conn)
        return self.get(reg["id"])

    def delete(self, name: str, actor: str) -> None:
        """Stops registering new files; the table and the files stay."""
        reg = self.get(name)
        with self.engine.begin() as conn:
            conn.execute(delete(parquet_tables).where(parquet_tables.c.id == reg["id"]))
            if self.audit is not None:
                self.audit.record("lakehouse", "parquet_table.deleted", actor, None, {"name": reg["name"]}, conn=conn)

    def run_due(self) -> int:
        n, now = 0, _now()
        for reg in self.list():
            if not reg["enabled"] or not reg["every_minutes"]:
                continue
            if reg["registered_ms"] and now - reg["registered_ms"] < reg["every_minutes"] * 60_000:
                continue
            try:
                self.refresh(reg["name"])
                n += 1
            except Exception as e:  # noqa: BLE001 - recorded; the others go on
                logger.warning("parquet table %s: %s", reg["name"], e)
                self._record(reg["id"], registered_ms=now, error=str(e)[:2000])
        return n


def build_parquet_tables_router(pt: ParquetTables, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/lakehouse/parquet-tables", tags=["lakehouse"])

    def wrap(fn):
        try:
            return fn()
        except ParquetError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    def list_(p=Depends(current_principal)):
        authorize(p, "MANAGE_INGESTION", None)
        return pt.list()

    @router.post("", status_code=201)
    async def register(body: dict = Body(...), p=Depends(current_principal)):
        authorize(p, "MANAGE_INGESTION", None)
        return await run_in_threadpool(lambda: wrap(lambda: pt.register(body, p.urn)))

    @router.get("/{name}")
    def get(name: str, p=Depends(current_principal)):
        authorize(p, "MANAGE_INGESTION", None)
        return wrap(lambda: pt.get(name))

    @router.put("/{name}")
    def change(name: str, body: dict = Body(...), p=Depends(current_principal)):
        authorize(p, "MANAGE_INGESTION", None)
        return wrap(lambda: pt.update(name, body, p.urn))

    @router.post("/{name}/refresh")
    async def refresh(name: str, p=Depends(current_principal)):
        authorize(p, "MANAGE_INGESTION", None)
        return await run_in_threadpool(lambda: wrap(lambda: pt.refresh(name)))

    @router.delete("/{name}", status_code=204)
    def remove(name: str, p=Depends(current_principal)):
        authorize(p, "MANAGE_INGESTION", None)
        wrap(lambda: pt.delete(name, p.urn))

    return router
