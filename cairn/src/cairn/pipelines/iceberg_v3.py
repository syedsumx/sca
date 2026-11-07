"""Iceberg v3 writes from Cairn's Python writers (pipes, streams, mirrors, SaaS syncs, synthetic tables, samples).

pyiceberg (0.12) reads v3 tables and keeps v3 table metadata (``next-row-id``, a snapshot's ``first-row-id``), but has
no writer for v3 manifests or manifest lists, so any write to a v3 table fails. :func:`enable` adds them, as the
Iceberg v3 spec has them:

* a v3 manifest is a v2 manifest with v3's data-file fields (``first_row_id`` and the deletion-vector fields, left
  empty: rows a writer adds inherit their ids from the manifest);
* a v3 manifest list assigns row ids: each data manifest without a ``first_row_id`` (the ones this commit wrote, and
  any written before the table was upgraded) gets the next free id, and the ids advance by its added and existing
  rows. The snapshot's ``first-row-id`` is the table's ``next-row-id`` and its ``added-rows`` is how far the ids
  advanced, so the catalog moves ``next-row-id`` on.

Rows Cairn appends therefore carry row lineage (``_row_id``) that Spark and Trino read. A copy-on-write rewrite by
pyiceberg (a delete, an overwrite, an upsert) writes the rows it keeps into new files without their old ids, so those
rows get new ids: their lineage starts again. Spark and Trino keep ids when they rewrite.

It is applied once, and only while the installed pyiceberg has no v3 writers of its own (a release that adds them is
used as it is).
"""

from __future__ import annotations

import contextvars
import threading
from typing import Any, Optional

_lock = threading.Lock()
_enabled = False
_next_row_id: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar("cairn_iceberg_next_row_id", default=None)
_assigned: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar("cairn_iceberg_assigned", default=None)


def _needed() -> bool:
    from pyiceberg import manifest

    return not hasattr(manifest, "ManifestWriterV3") or not hasattr(manifest, "ManifestListWriterV3")


def enable() -> bool:
    """Lets pyiceberg write v3 tables. Returns True when Cairn's writers are in use (False: pyiceberg has its own)."""
    global _enabled
    with _lock:
        if _enabled:
            return True
        from pyiceberg import manifest
        from pyiceberg.table.update import snapshot as snapshot_mod

        if not _needed():
            return False
        from copy import copy

        from pyiceberg.avro.file import AvroFile, AvroOutputFile
        from pyiceberg.manifest import (MANIFEST_LIST_FILE_SCHEMAS, ManifestContent, ManifestFile, ManifestListWriterV2,
                                        ManifestWriterV2, PartitionFieldSummary)

        # A manifest's first_row_id (field 520): the last field of the v3 manifest-list schema, read and written.
        v3_list = MANIFEST_LIST_FILE_SCHEMAS[3]
        at = len(v3_list.fields) - 1
        assert v3_list.fields[at].field_id == 520, "unexpected v3 manifest-list schema"

        def _get(self) -> Optional[int]:
            return self._data[at] if len(self._data) > at else None

        def _set(self, value: Optional[int]) -> None:
            if len(self._data) <= at:
                self._data.extend([None] * (at + 1 - len(self._data)))
            self._data[at] = value
        ManifestFile.first_row_id = property(_get, _set)

        def read_manifest_list(input_file):
            """Manifest lists read with the v3 schema (a superset: v1 and v2 lists read the same, without ids)."""
            with AvroFile[ManifestFile](input_file, v3_list, read_types={-1: ManifestFile, 508: PartitionFieldSummary},
                                        read_enums={517: ManifestContent}) as reader:
                yield from reader
        manifest.read_manifest_list = read_manifest_list

        class ManifestWriterV3(ManifestWriterV2):
            @property
            def version(self):
                return 3

        class ManifestListWriterV3(ManifestListWriterV2):
            def __init__(self, output_file, snapshot_id, parent_snapshot_id, sequence_number, compression,
                         first_row_id: int):
                super().__init__(output_file, snapshot_id, parent_snapshot_id, sequence_number, compression)
                self._format_version = 3
                self._meta = {**self._meta, "format-version": "3", "first-row-id": str(first_row_id)}
                self._next = first_row_id

            def __enter__(self):
                self._writer = AvroOutputFile[ManifestFile](
                    output_file=self._output_file, record_schema=v3_list, file_schema=v3_list,
                    schema_name="manifest_file", metadata=self._meta)
                self._writer.__enter__()
                return self

            def prepare_manifest(self, manifest_file):
                m = copy(super().prepare_manifest(manifest_file))
                m._data = list(m._data)
                m.first_row_id = m.first_row_id                    # padded to the v3 schema
                if m.content == ManifestContent.DATA and m.first_row_id is None:
                    m.first_row_id = self._next
                    self._next += (m.added_rows_count or 0) + (m.existing_rows_count or 0)
                return m

            def __exit__(self, *exc):
                out = super().__exit__(*exc)
                got = _assigned.get()
                if got is not None:
                    got.append(self._next)
                return out

        orig_manifest, orig_list = manifest.write_manifest, manifest.write_manifest_list

        def write_manifest(format_version, spec, schema, output_file, snapshot_id, avro_compression):
            if format_version == 3:
                return ManifestWriterV3(spec, schema, output_file, snapshot_id, avro_compression)
            return orig_manifest(format_version, spec, schema, output_file, snapshot_id, avro_compression)

        def write_manifest_list(format_version, output_file, snapshot_id, parent_snapshot_id, sequence_number,
                                avro_compression):
            if format_version == 3:
                first = _next_row_id.get()
                if first is None or sequence_number is None:
                    raise ValueError("a v3 manifest list needs the table's next-row-id and a sequence number")
                return ManifestListWriterV3(output_file, snapshot_id, parent_snapshot_id, sequence_number,
                                            avro_compression, first)
            return orig_list(format_version, output_file, snapshot_id, parent_snapshot_id, sequence_number,
                             avro_compression)

        for mod in (manifest, snapshot_mod):
            mod.write_manifest = write_manifest
            mod.write_manifest_list = write_manifest_list
        manifest.ManifestWriterV3, manifest.ManifestListWriterV3 = ManifestWriterV3, ManifestListWriterV3

        producer = snapshot_mod._SnapshotProducer
        orig_commit = producer._commit

        def _commit(self) -> Any:
            meta = self._transaction.table_metadata
            if meta.format_version < 3:
                return orig_commit(self)
            first = meta.next_row_id or 0
            assigned: list = []
            t1, t2 = _next_row_id.set(first), _assigned.set(assigned)
            try:
                updates, requirements = orig_commit(self)
            finally:
                _next_row_id.reset(t1)
                _assigned.reset(t2)
            if not assigned:
                raise RuntimeError("the v3 manifest list was not written")
            from pyiceberg.table.update import AddSnapshotUpdate

            out = []
            for u in updates:
                if isinstance(u, AddSnapshotUpdate):
                    u = AddSnapshotUpdate(snapshot=u.snapshot.model_copy(
                        update={"first_row_id": first, "added_rows": assigned[-1] - first}))
                out.append(u)
            return tuple(out), requirements

        producer._commit = _commit
        # Manifests read before now were read without their first_row_id, and pyiceberg reuses what it read by path:
        # forget them, or a later commit would give their rows new ids.
        manifest.clear_manifest_cache()
        _enabled = True
        return True
