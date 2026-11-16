"""Delta conversion: the converted table never takes files from outside the Delta table's location."""

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pyarrow import fs as pafs

from cairn.pipelines.delta_convert import DeltaConversions, _inside
from cairn.pipelines.pipes import PipeError

SCHEMA = json.dumps({"type": "struct", "fields": [{"name": "id", "type": "long", "nullable": True, "metadata": {}}]})


def _table(root, rel: str) -> DeltaConversions:
    """A Delta table at s3://lake/t whose log adds ``rel``; another table's file at lake/other/secret.parquet."""
    (root / "lake" / "t" / "_delta_log").mkdir(parents=True)
    (root / "lake" / "other").mkdir(parents=True)
    pq.write_table(pa.table({"id": [1, 2]}), str(root / "lake" / "other" / "secret.parquet"))
    pq.write_table(pa.table({"id": [3]}), str(root / "lake" / "t" / "part-0.parquet"))
    log = [{"protocol": {"minReaderVersion": 1, "minWriterVersion": 2}},
           {"metaData": {"id": "x", "format": {"provider": "parquet"}, "schemaString": SCHEMA, "partitionColumns": [],
                         "configuration": {}}},
           {"add": {"path": rel, "partitionValues": {}, "size": 10, "dataChange": True}}]
    (root / "lake" / "t" / "_delta_log" / f"{0:020d}.json").write_text("\n".join(json.dumps(x) for x in log))
    conv = DeltaConversions({}, {})
    conv._fs = lambda: pafs.SubTreeFileSystem(str(root), pafs.LocalFileSystem())
    conv._catalog = lambda: pytest.fail("a file outside the table must be refused before the catalog is touched")
    return conv


@pytest.mark.parametrize("rel", ["../other/secret.parquet", "%2E%2E/other/secret.parquet", "a/../../other/secret.parquet",
                                 "s3://lake/other/secret.parquet", "/lake/other/secret.parquet"])
def test_a_log_entry_outside_the_table_is_refused(tmp_path, rel):
    with pytest.raises(PipeError) as e:
        _table(tmp_path, rel).convert("s3://lake/t", "conv.t", "urn:li:corpuser:admin")
    assert e.value.status == 409 and "outside the table" in str(e.value)


def test_paths_inside_the_table_are_kept():
    assert _inside("part-0.parquet") == "part-0.parquet"
    assert _inside("year%3D2024/part-0.parquet") == "year=2024/part-0.parquet"
    assert _inside("a/./b/../part.parquet") == "a/part.parquet"
