"""Delta Lake tables converted to Iceberg tables in Cairn Catalog, without copying their data.

``POST /v1/delta/convert`` (``MANAGE_SYSTEM_OPERATIONS``) with ``location`` (the Delta table's
``s3://bucket/path``) and ``target`` (``namespace.table``):

* Cairn reads the Delta transaction log from the store (the latest checkpoint, then the JSON commits after it)
  and finds the table's live Parquet files at its latest version.
* It creates the Iceberg table **at the Delta table's location** and adds those files to it in one commit. The
  data stays where it is, and the catalog's per-table credentials (scoped to the table's location) cover it.
* The table records where it came from: ``cairn.converted-from`` (the location) and ``cairn.delta-version``.

Conversion is one-way. The files now belong to the Iceberg table, and Iceberg maintenance may delete them as
the table changes, so the Delta table is retired first: unregister it (``CALL
delta.system.unregister_table(...)``) so nothing writes to it. Cairn checks that the log did not move while it
converted, and refuses what Iceberg cannot read the same way:

* partitioned tables (Delta keeps partition values in paths, not in the files),
* deletion vectors (rows deleted without rewriting their file),
* column mapping (files whose column names are not the table's).

For those, copy the table with a derived table (``SELECT * FROM delta.<schema>.<table>``).
"""

from __future__ import annotations

import json
import logging
import posixpath
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

from cairn.pipelines.pipes import TARGET, PipeError

logger = logging.getLogger(__name__)


def _split(location: str) -> tuple[str, str]:
    u = urlsplit(location.rstrip("/"))
    if u.scheme not in ("s3", "s3a") or not u.netloc or not u.path.strip("/"):
        raise PipeError("location: the Delta table's s3://bucket/path")
    return u.netloc, u.path.strip("/")


class DeltaLog:
    """A Delta table's state at its latest version, read from its ``_delta_log``."""

    def __init__(self, fs, bucket: str, path: str):
        self.fs, self.bucket, self.path = fs, bucket, path
        self.log = f"{bucket}/{path}/_delta_log"

    def _read(self, name: str) -> bytes:
        with self.fs.open_input_stream(f"{self.log}/{name}") as f:
            return f.read()

    def latest(self) -> int:
        from pyarrow import fs as pafs

        try:
            infos = self.fs.get_file_info(pafs.FileSelector(self.log, recursive=False))
        except FileNotFoundError:
            infos = []
        versions = [int(posixpath.basename(i.path)[:20]) for i in infos
                    if i.type == pafs.FileType.File and i.path.endswith(".json") and posixpath.basename(i.path)[:20].isdigit()]
        if not versions:
            raise PipeError(f"no Delta log at s3://{self.bucket}/{self.path}/_delta_log", 404)
        return max(versions)

    def state(self) -> dict:
        """{"version", "files": {path: size}, "metadata", "protocol", "deletion_vectors"}."""
        import pyarrow.parquet as pq

        version, start = self.latest(), 0
        files: dict[str, int] = {}
        meta: dict = {}
        protocol: dict = {}
        dvs = False
        try:
            last = json.loads(self._read("_last_checkpoint"))
        except (FileNotFoundError, OSError):
            last = None
        if last and int(last["version"]) <= version:
            if last.get("parts"):
                raise PipeError("multi-part checkpoints are not read yet: write a single-part checkpoint first", 409)
            cp = pq.read_table(f"{self.log}/{int(last['version']):020d}.checkpoint.parquet", filesystem=self.fs).to_pylist()
            for row in cp:
                if row.get("add"):
                    files[row["add"]["path"]] = row["add"].get("size") or 0
                    dvs = dvs or bool(row["add"].get("deletionVector"))
                if row.get("metaData"):
                    meta = row["metaData"]
                if row.get("protocol"):
                    protocol = row["protocol"]
            start = int(last["version"]) + 1
        for v in range(start, version + 1):
            for line in self._read(f"{v:020d}.json").decode().splitlines():
                if not line.strip():
                    continue
                a = json.loads(line)
                if "add" in a:
                    files[a["add"]["path"]] = a["add"].get("size") or 0
                    dvs = dvs or bool(a["add"].get("deletionVector"))
                elif "remove" in a:
                    files.pop(a["remove"]["path"], None)
                elif "metaData" in a:
                    meta = a["metaData"]
                elif "protocol" in a:
                    protocol = a["protocol"]
        return {"version": version, "files": files, "metadata": meta, "protocol": protocol, "deletion_vectors": dvs}


class DeltaConversions:
    def __init__(self, store: dict, catalog: dict, audit=None):
        """``store``: {"endpoint", "access_key", "secret_key", "region"}, a key that reads the Delta tables;
        ``catalog``: pyiceberg RestCatalog properties."""
        self.store, self.catalog_props, self.audit = store, catalog, audit

    def _fs(self):
        from pyarrow import fs

        endpoint = self.store.get("endpoint", "")
        return fs.S3FileSystem(access_key=self.store.get("access_key"), secret_key=self.store.get("secret_key"),
                               region=self.store.get("region", "us-east-1"), endpoint_override=endpoint.split("://", 1)[-1],
                               scheme="https" if endpoint.startswith("https") else "http")

    def _catalog(self):
        from pyiceberg.catalog.rest import RestCatalog

        return RestCatalog("cairn-delta", **self.catalog_props)

    def inspect(self, location: str) -> dict:
        """What converting would do, or why it cannot be done."""
        bucket, path = _split(location)
        st = DeltaLog(self._fs(), bucket, path).state()
        return {"location": f"s3://{bucket}/{path}", "version": st["version"], "files": len(st["files"]),
                "bytes": sum(st["files"].values()), "problems": self._problems(st)}

    @staticmethod
    def _problems(st: dict) -> list[str]:
        meta = st["metadata"] or {}
        out = []
        if not meta:
            out.append("the log has no table metadata")
        parts = meta.get("partitionColumns") or []
        if isinstance(parts, str):
            parts = json.loads(parts)
        if parts:
            out.append(f"partitioned by {', '.join(parts)}: copy it with a derived table")
        conf = meta.get("configuration") or {}
        if isinstance(conf, list):                       # a checkpoint's map, as pyarrow returns it
            conf = dict(conf)
        if (conf.get("delta.columnMapping.mode") or "none") != "none":
            out.append("column mapping is on: copy it with a derived table")
        if st["deletion_vectors"]:
            out.append("it has deletion vectors: copy it with a derived table (or purge them first)")
        if not st["files"]:
            out.append("it has no data files")
        return out

    def convert(self, location: str, target: str, actor: str) -> dict:
        import pyarrow.parquet as pq

        if not TARGET.match(target or ""):
            raise PipeError("target: namespace.table")
        bucket, path = _split(location)
        fs = self._fs()
        log = DeltaLog(fs, bucket, path)
        st = log.state()
        problems = self._problems(st)
        if problems:
            raise PipeError(f"s3://{bucket}/{path} cannot be converted in place: {'; '.join(problems)}", 409)
        files = [f"s3://{bucket}/{path}/{unquote(p)}" for p in sorted(st["files"])]
        schema = pq.read_schema(f"{bucket}/{path}/{unquote(sorted(st['files'])[0])}", filesystem=fs)
        cat = self._catalog()
        ns, _, name = target.rpartition(".")
        cat.create_namespace_if_not_exists(ns)
        from pyiceberg.exceptions import TableAlreadyExistsError

        try:
            table = cat.create_table(target, schema=schema, location=f"s3://{bucket}/{path}", properties={
                "cairn.converted-from": f"s3://{bucket}/{path}", "cairn.delta-version": str(st["version"])})
        except TableAlreadyExistsError:
            raise PipeError(f"{target} exists", 409)
        try:
            from pyiceberg.table.name_mapping import create_mapping_from_schema

            with table.transaction() as tx:            # Delta's files carry no Iceberg field ids: map by name
                tx.set_properties({"schema.name-mapping.default": create_mapping_from_schema(table.schema()).model_dump_json()})
                tx.add_files(files)
            if log.latest() != st["version"]:
                raise PipeError("the Delta table was written while it was converted: unregister it, then convert again", 409)
        except Exception:
            cat.drop_table(target)                         # nothing half-converted is left behind
            raise
        out = {"target": target, "location": f"s3://{bucket}/{path}", "delta_version": st["version"], "files": len(files),
               "snapshot": table.refresh().current_snapshot().snapshot_id}
        if self.audit is not None:
            self.audit.record("lakehouse", "delta.converted", actor, None, out)
        return out


def build_delta_router(conv: DeltaConversions, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/v1/delta", tags=["lakehouse"])

    def wrap(fn):
        try:
            return fn()
        except PipeError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/inspect")
    def inspect(location: str, p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return wrap(lambda: conv.inspect(location))

    @router.post("/convert", status_code=201)
    def convert(body: dict = Body(...), p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return wrap(lambda: conv.convert(str(body.get("location") or ""), str(body.get("target") or ""), p.urn))

    return router


def from_env(audit=None) -> Optional[DeltaConversions]:
    """``CAIRN_DELTA_STORE`` (default: the pipes' store) and ``CAIRN_DELTA_CATALOG`` (default: the pipes' catalog)."""
    import os

    from cairn.pipelines.pipes import _settings

    store = "CAIRN_DELTA_STORE" if os.environ.get("CAIRN_DELTA_STORE") else "CAIRN_PIPES_STORE"
    catalog = "CAIRN_DELTA_CATALOG" if os.environ.get("CAIRN_DELTA_CATALOG") else "CAIRN_PIPES_CATALOG"
    if not (os.environ.get(store) and os.environ.get(catalog)):
        return None
    return DeltaConversions(_settings(store), _settings(catalog), audit)
