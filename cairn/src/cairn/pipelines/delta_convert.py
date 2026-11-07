"""Delta Lake tables converted to Iceberg tables in Cairn Catalog, without copying their data.

``POST /v1/delta/convert`` (``MANAGE_SYSTEM_OPERATIONS``) with ``location`` (the Delta table's
``s3://bucket/path``) and ``target`` (``namespace.table``):

* Cairn reads the Delta transaction log from the store (the latest checkpoint, then the JSON commits after it)
  and finds the table's live Parquet files at its latest version.
* It creates the Iceberg table **at the Delta table's location** and adds those files to it in one commit. The
  data stays where it is, and the catalog's per-table credentials (scoped to the table's location) cover it.
* The table records where it came from: ``cairn.converted-from`` (the location) and ``cairn.delta-version``.

* **Partitioned tables.** Delta keeps partition values in its log, not in the files. The Iceberg table is
  partitioned by the same columns (identity), and each file is added with its partition values from the log:
  Iceberg readers take those columns from the partition, as Delta readers take them from the log.
* **Deletion vectors** (rows deleted without rewriting their file). Cairn reads each one (inline, or in its
  ``deletion_vector_<uuid>.bin``) and writes that file again without its deleted rows, under
  ``<location>/cairn-converted/``; files without deletion vectors are still added as they are.

Conversion is one-way. The files now belong to the Iceberg table, and Iceberg maintenance may delete them as
the table changes, so the Delta table is retired first: unregister it (``CALL
delta.system.unregister_table(...)``) so nothing writes to it. Cairn checks that the log did not move while it
converted, and refuses what Iceberg cannot read the same way: column mapping (files whose column names are not
the table's). For that, copy the table with a derived table (``SELECT * FROM delta.<schema>.<table>``).
"""

from __future__ import annotations

import json
import logging
import posixpath
import struct
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

from cairn.pipelines.pipes import TARGET, PipeError

logger = logging.getLogger(__name__)


def _split(location: str) -> tuple[str, str]:
    u = urlsplit(location.rstrip("/"))
    if u.scheme not in ("s3", "s3a") or not u.netloc or not u.path.strip("/"):
        raise PipeError("location: the Delta table's s3://bucket/path")
    return u.netloc, u.path.strip("/")


# ---- deletion vectors (Delta protocol, "Deletion Vector Format") ----------------------------------------
Z85 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-:+=^!/*?&<>()[]{}@%$#"
DV_MAGIC = 1681511377


def z85_decode(text: str) -> bytes:
    if len(text) % 5:
        raise ValueError("Z85 text is a multiple of 5 characters")
    out = bytearray()
    for i in range(0, len(text), 5):
        n = 0
        for ch in text[i:i + 5]:
            n = n * 85 + Z85.index(ch)
        out += n.to_bytes(4, "big")
    return bytes(out)


def _roaring32(buf: bytes, at: int) -> tuple[list[int], int]:
    """One 32-bit Roaring bitmap in the portable format at ``buf[at:]``: (its values, where it ends)."""
    cookie = struct.unpack_from("<I", buf, at)[0]
    if cookie & 0xFFFF == 12347:                         # with run containers
        count = (cookie >> 16) + 1
        at += 4
        runs = buf[at:at + (count + 7) // 8]
        at += (count + 7) // 8
        has_offsets = count >= 4
    elif cookie == 12346:
        count = struct.unpack_from("<I", buf, at + 4)[0]
        at += 8
        runs, has_offsets = b"", True
    else:
        raise ValueError("not a portable Roaring bitmap")
    header = [struct.unpack_from("<HH", buf, at + 4 * i) for i in range(count)]
    at += 4 * count
    if has_offsets:
        at += 4 * count
    values: list[int] = []
    for i, (key, card_minus_1) in enumerate(header):
        high = key << 16
        if runs and runs[i // 8] >> (i % 8) & 1:
            n = struct.unpack_from("<H", buf, at)[0]
            at += 2
            for _ in range(n):
                start, length = struct.unpack_from("<HH", buf, at)
                at += 4
                values.extend(range(high + start, high + start + length + 1))
        elif card_minus_1 + 1 > 4096:                     # a bitmap container: 2^16 bits
            words = struct.unpack_from("<1024Q", buf, at)
            at += 8192
            for w, word in enumerate(words):
                while word:
                    low = word & -word
                    values.append(high + w * 64 + low.bit_length() - 1)
                    word ^= low
        else:
            values.extend(high + v for v in struct.unpack_from(f"<{card_minus_1 + 1}H", buf, at))
            at += 2 * (card_minus_1 + 1)
    return values, at


def deleted_rows(data: bytes) -> set[int]:
    """The row positions a deletion vector's data (magic + RoaringBitmapArray) marks deleted."""
    if struct.unpack_from("<I", data, 0)[0] != DV_MAGIC:
        raise ValueError("not a Delta deletion vector")
    count = struct.unpack_from("<Q", data, 4)[0]
    at, out = 12, set()
    for _ in range(count):
        key = struct.unpack_from("<I", data, at)[0]
        values, at = _roaring32(data, at + 4)
        out.update((key << 32) | v for v in values)
    return out


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

    def deletion_vector(self, dv: dict) -> set[int]:
        """The rows a file's deletion vector (its descriptor in the log) marks deleted."""
        kind, where = dv.get("storageType"), dv.get("pathOrInlineDv") or ""
        if kind == "i":
            data = z85_decode(where)
        else:
            if kind == "u":
                prefix, encoded = where[:-20], where[-20:]
                name = f"deletion_vector_{uuid.UUID(bytes=z85_decode(encoded))}.bin"
                path = f"{self.bucket}/{self.path}/" + (f"{prefix}/{name}" if prefix else name)
            elif kind == "p":
                u = urlsplit(where)
                path = f"{u.netloc}/{u.path.lstrip('/')}"
            else:
                raise PipeError(f"deletion vector storage {kind!r} is not known", 409)
            with self.fs.open_input_file(path) as f:
                f.seek(int(dv.get("offset") or 1))
                size = struct.unpack(">I", f.read(4))[0]
                data = f.read(size)
        found = deleted_rows(data)
        if len(found) != int(dv.get("cardinality", len(found))):
            raise PipeError("a deletion vector does not hold the rows its log entry says", 409)
        return found

    def state(self) -> dict:
        """{"version", "files": {path: {"size", "partition", "deletion_vector"}}, "metadata", "protocol",
        "deletion_vectors"}: the live files at the latest version, each with its partition values and its
        deletion vector, as the log last added it."""
        import pyarrow.parquet as pq

        def entry(add: dict) -> dict:
            parts = add.get("partitionValues") or {}
            if isinstance(parts, list):                  # a checkpoint's map, as pyarrow returns it
                parts = dict(parts)
            return {"size": add.get("size") or 0, "partition": parts, "deletion_vector": add.get("deletionVector")}

        version, start = self.latest(), 0
        files: dict[str, dict] = {}
        meta: dict = {}
        protocol: dict = {}
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
                    files[row["add"]["path"]] = entry(row["add"])
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
                    files[a["add"]["path"]] = entry(a["add"])
                elif "remove" in a:
                    files.pop(a["remove"]["path"], None)
                elif "metaData" in a:
                    meta = a["metaData"]
                elif "protocol" in a:
                    protocol = a["protocol"]
        return {"version": version, "files": files, "metadata": meta, "protocol": protocol,
                "deletion_vectors": sum(1 for f in files.values() if f["deletion_vector"])}


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

        from cairn.pipelines.iceberg_v3 import enable

        enable()                                         # pyiceberg writes v3 tables too
        return RestCatalog("cairn-delta", **self.catalog_props)

    def inspect(self, location: str) -> dict:
        """What converting would do, or why it cannot be done."""
        bucket, path = _split(location)
        st = DeltaLog(self._fs(), bucket, path).state()
        return {"location": f"s3://{bucket}/{path}", "version": st["version"], "files": len(st["files"]),
                "bytes": sum(f["size"] for f in st["files"].values()), "problems": self._problems(st),
                "partitioned_by": self._partition_columns(st["metadata"] or {}),
                "files_with_deletes": st["deletion_vectors"]}

    @staticmethod
    def _partition_columns(meta: dict) -> list[str]:
        parts = meta.get("partitionColumns") or []
        return json.loads(parts) if isinstance(parts, str) else list(parts)

    @staticmethod
    def _problems(st: dict) -> list[str]:
        meta = st["metadata"] or {}
        out = []
        if not meta:
            out.append("the log has no table metadata")
        conf = meta.get("configuration") or {}
        if isinstance(conf, list):                       # a checkpoint's map, as pyarrow returns it
            conf = dict(conf)
        if (conf.get("delta.columnMapping.mode") or "none") != "none":
            out.append("column mapping is on: copy it with a derived table")
        if not st["files"]:
            out.append("it has no data files")
        return out

    @staticmethod
    def _delta_fields(meta: dict) -> dict[str, Any]:
        """The table's columns as Arrow types, from its Delta schema (partition columns are not in the files)."""
        import pyarrow as pa

        simple = {"string": pa.string(), "long": pa.int64(), "integer": pa.int32(), "short": pa.int16(),
                  "byte": pa.int8(), "double": pa.float64(), "float": pa.float32(), "boolean": pa.bool_(),
                  "date": pa.date32(), "binary": pa.binary(), "timestamp": pa.timestamp("us", tz="UTC"),
                  "timestamp_ntz": pa.timestamp("us")}
        out = {}
        for f in json.loads(meta.get("schemaString") or '{"fields": []}').get("fields", []):
            t = f.get("type")
            if isinstance(t, str) and t.startswith("decimal("):
                prec, scale = (int(x) for x in t[8:-1].split(","))
                out[f["name"]] = pa.decimal128(prec, scale)
            elif isinstance(t, str) and t in simple:
                out[f["name"]] = simple[t]
        return out

    @staticmethod
    def _partition_value(raw: Optional[str], iceberg_type: Any) -> Any:
        """A Delta partition value (a string in the log) as Iceberg keeps it in a partition."""
        from pyiceberg import types as it

        if raw is None or raw == "":
            return None                                   # Delta writes null (or an empty string) for no value
        if isinstance(iceberg_type, (it.IntegerType, it.LongType)):
            return int(raw)
        if isinstance(iceberg_type, (it.DoubleType, it.FloatType)):
            return float(raw)
        if isinstance(iceberg_type, it.BooleanType):
            return raw.lower() == "true"
        if isinstance(iceberg_type, it.DateType):
            return (date.fromisoformat(raw) - date(1970, 1, 1)).days
        if isinstance(iceberg_type, (it.TimestampType, it.TimestamptzType)):
            t = datetime.fromisoformat(raw.replace(" ", "T"))
            t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
            return int(t.timestamp() * 1_000_000)
        if isinstance(iceberg_type, it.DecimalType):
            return Decimal(raw)
        if isinstance(iceberg_type, it.StringType):
            return raw
        raise PipeError(f"partition values of type {iceberg_type} are not converted yet: copy it with a derived table", 409)

    def _without_deleted(self, fs, bucket: str, path: str, rel: str, deleted: set[int]) -> str:
        """The file written again without its deleted rows, beside the table's files; its s3:// path."""
        import pyarrow as pa
        import pyarrow.compute as pc
        import pyarrow.parquet as pq

        table = pq.read_table(f"{bucket}/{path}/{rel}", filesystem=fs)
        keep = pc.invert(pc.is_in(pa.array(range(table.num_rows), pa.int64()), value_set=pa.array(sorted(deleted), pa.int64())))
        out = f"{path}/cairn-converted/{uuid.uuid4().hex}.parquet"
        pq.write_table(table.filter(keep), f"{bucket}/{out}", filesystem=fs)
        return f"s3://{bucket}/{out}"

    def convert(self, location: str, target: str, actor: str) -> dict:
        import pyarrow as pa
        import pyarrow.parquet as pq
        from pyiceberg.exceptions import TableAlreadyExistsError
        from pyiceberg.io.pyarrow import (compute_statistics_plan, data_file_statistics_from_parquet_metadata,
                                          parquet_path_to_id_mapping)
        from pyiceberg.manifest import DataFile, DataFileContent, FileFormat
        from pyiceberg.table.name_mapping import create_mapping_from_schema
        from pyiceberg.typedef import Record

        if not TARGET.match(target or ""):
            raise PipeError("target: namespace.table")
        bucket, path = _split(location)
        fs = self._fs()
        log = DeltaLog(fs, bucket, path)
        st = log.state()
        problems = self._problems(st)
        if problems:
            raise PipeError(f"s3://{bucket}/{path} cannot be converted in place: {'; '.join(problems)}", 409)
        meta = st["metadata"]
        part_cols = self._partition_columns(meta)
        # Deletion vectors first: a file with deleted rows is written again without them (the rest stay as they are).
        files: list[tuple[str, dict]] = []
        rewritten = 0
        for rel in sorted(st["files"]):
            f = st["files"][rel]
            if f["deletion_vector"]:
                deleted = log.deletion_vector(f["deletion_vector"])
                files.append((self._without_deleted(fs, bucket, path, unquote(rel), deleted), f))
                rewritten += 1
            else:
                files.append((f"s3://{bucket}/{path}/{unquote(rel)}", f))
        first = files[0][0].removeprefix("s3://")
        file_schema = pq.read_schema(first, filesystem=fs)
        delta_types = self._delta_fields(meta)
        missing = [c for c in part_cols if c not in file_schema.names]
        unknown = [c for c in missing if c not in delta_types]
        if unknown:
            raise PipeError(f"partition columns {unknown} have types not converted yet: copy it with a derived table", 409)
        schema = pa.schema(list(file_schema) + [pa.field(c, delta_types[c]) for c in missing])
        cat = self._catalog()
        ns, _, name = target.rpartition(".")
        cat.create_namespace_if_not_exists(ns)
        try:
            table = cat.create_table(target, schema=schema, location=f"s3://{bucket}/{path}", properties={
                "cairn.converted-from": f"s3://{bucket}/{path}", "cairn.delta-version": str(st["version"])})
        except TableAlreadyExistsError:
            raise PipeError(f"{target} exists", 409)
        try:
            with table.transaction() as tx:            # Delta's files carry no Iceberg field ids: map by name
                tx.set_properties({"schema.name-mapping.default": create_mapping_from_schema(table.schema()).model_dump_json()})
                if part_cols:
                    with tx.update_spec() as spec:
                        for c in part_cols:
                            spec.add_identity(c)
            table = table.refresh()
            ice_schema, pspec = table.schema(), table.spec()
            types = {fld.name: fld.field_type for fld in ice_schema.fields}
            with table.transaction() as tx:
                with tx.update_snapshot().fast_append() as append:
                    for s3_path, f in files:
                        md = pq.read_metadata(s3_path.removeprefix("s3://"), filesystem=fs)
                        stats = data_file_statistics_from_parquet_metadata(
                            parquet_metadata=md, stats_columns=compute_statistics_plan(ice_schema, table.properties),
                            parquet_column_mapping=parquet_path_to_id_mapping(ice_schema))
                        partition = Record(*[self._partition_value(f["partition"].get(c), types[c]) for c in part_cols])
                        size = fs.get_file_info(s3_path.removeprefix("s3://")).size
                        append.append_data_file(DataFile.from_args(
                            content=DataFileContent.DATA, file_path=s3_path, file_format=FileFormat.PARQUET,
                            partition=partition, file_size_in_bytes=size, sort_order_id=None, spec_id=pspec.spec_id,
                            equality_ids=None, key_metadata=None, **stats.to_serialized_dict()))
            if log.latest() != st["version"]:
                raise PipeError("the Delta table was written while it was converted: unregister it, then convert again", 409)
        except Exception:
            cat.drop_table(target)                         # nothing half-converted is left behind
            raise
        out = {"target": target, "location": f"s3://{bucket}/{path}", "delta_version": st["version"], "files": len(files),
               "partitioned_by": part_cols, "rewritten_for_deletes": rewritten,
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
