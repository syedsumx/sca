"""Delta to Iceberg conversion: the log reader and partition values, on a Delta log in a local directory
(the same pyarrow filesystem calls the store gets)."""

import json
import os

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("pyiceberg")

from cairn.pipelines.delta_convert import DeltaConversions, DeltaLog  # noqa: E402
from cairn.pipelines.pipes import PipeError  # noqa: E402, F401

META = {"metaData": {"id": "t", "format": {"provider": "parquet"}, "partitionColumns": [], "configuration": {},
                     "schemaString": json.dumps({"type": "struct", "fields": [
                         {"name": "id", "type": "long", "nullable": True, "metadata": {}}]})}}
PROTOCOL = {"protocol": {"minReaderVersion": 3, "minWriterVersion": 7,
                         "readerFeatures": ["deletionVectors"], "writerFeatures": ["deletionVectors"]}}


def _log(tmp_path, *commits) -> DeltaLog:
    from pyarrow import fs

    d = tmp_path / "tbl" / "_delta_log"
    d.mkdir(parents=True)
    for v, actions in enumerate(commits):
        (d / f"{v:020d}.json").write_text("\n".join(json.dumps(a) for a in actions) + "\n")
    return DeltaLog(fs.LocalFileSystem(), str(tmp_path).rstrip("/"), "tbl")


def _add(path, dv=None):
    a = {"path": path, "size": 10, "partitionValues": {}, "dataChange": True, "modificationTime": 0}
    if dv:
        a["deletionVector"] = dv
    return {"add": a}


def _remove(path, dv=None):
    r = {"path": path, "dataChange": True, "deletionTimestamp": 0}
    if dv:
        r["deletionVector"] = dv
    return {"remove": r}


DV1 = {"storageType": "u", "pathOrInlineDv": "ab^-aqEH.-t@S}K{vb[*k^", "offset": 1, "sizeInBytes": 34, "cardinality": 1}
DV2 = {"storageType": "u", "pathOrInlineDv": "ab^-aqEH.-t@S}K{vb[*k^", "offset": 45, "sizeInBytes": 34, "cardinality": 2}


def test_a_file_whose_deletion_vector_changed_stays_live_whatever_the_order_in_the_commit(tmp_path):
    """A DELETE on a file that already has a deletion vector commits add(path, new DV) and remove(path, old DV): one
    logical file goes and another comes. Keyed by path alone, a remove written after the add dropped the file, and
    every row in it vanished from the converted table."""
    log = _log(tmp_path, [PROTOCOL, META, _add("part-0.parquet")],
               [_add("part-0.parquet", DV1), _remove("part-0.parquet")],
               [_add("part-0.parquet", DV2), _remove("part-0.parquet", DV1)])
    st = log.state()
    assert list(st["files"]) == ["part-0.parquet"]
    assert st["files"]["part-0.parquet"]["deletion_vector"]["offset"] == 45
    # The logical file the remove names (its path and its deletion vector) does go.
    log2 = _log(tmp_path / "b", [PROTOCOL, META, _add("part-0.parquet", DV1)], [_remove("part-0.parquet", DV1)])
    assert log2.state()["files"] == {}


def test_timestamp_partition_values_keep_every_microsecond():
    """Iceberg readers take a partition column's value from the partition: a microsecond lost to float rounding
    changes the value read and makes an equality filter skip the file."""
    from pyiceberg import types as it

    for raw, want in [("2038-01-27 21:08:42.705950", 2148239322705950),
                      ("1952-03-13 00:09:07.824318", -561858652175682),
                      ("2041-04-04 10:00:29.026632", 2248682429026632)]:
        assert DeltaConversions._partition_value(raw, it.TimestamptzType()) == want
        assert DeltaConversions._partition_value(raw, it.TimestampType()) == want


def test_paths_inside_the_table_are_kept(tmp_path):
    log = _log(tmp_path, [PROTOCOL, META, _add("date=2024-01-01/part-0.parquet"), _add("part%20one.parquet")])
    assert sorted(log.state()["files"]) == ["date=2024-01-01/part-0.parquet", "part%20one.parquet"]
    assert os.sep == "/"
