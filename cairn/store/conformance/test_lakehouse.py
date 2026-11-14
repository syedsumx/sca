"""A real Iceberg workload on Cairn Store: pyiceberg writes and commits tables through pyarrow's S3
filesystem (the AWS C++ SDK), then reads them back, time-travels, evolves the schema and deletes rows."""

import uuid

import pyarrow as pa
import pyarrow.compute as pc
import pytest
from pyarrow import fs as pafs

from conftest import ADMIN, REGION


@pytest.fixture
def lake(store, s3):
    name = "lake-" + uuid.uuid4().hex[:10]
    s3.create_bucket(Bucket=name)
    return name


def s3fs(store) -> pafs.S3FileSystem:
    return pafs.S3FileSystem(access_key=ADMIN[0], secret_key=ADMIN[1], region=REGION, scheme="http",
                             endpoint_override=store.endpoint.removeprefix("http://"), force_virtual_addressing=False)


def test_pyarrow_filesystem(store, lake):
    fs = s3fs(store)
    big = pa.table({"id": pa.array(range(2_000_000), pa.int64()), "v": pa.array([float(i) for i in range(2_000_000)])})
    import pyarrow.parquet as pq
    pq.write_table(big, f"{lake}/raw/big.parquet", filesystem=fs)          # > one part: multipart upload
    info = fs.get_file_info(f"{lake}/raw/big.parquet")
    assert info.size > 10 << 20
    back = pq.read_table(f"{lake}/raw/big.parquet", filesystem=fs, columns=["v"])   # ranged footer + column reads
    assert back.num_rows == 2_000_000 and back.column("v")[123].as_py() == 123.0
    listing = fs.get_file_info(pafs.FileSelector(f"{lake}/raw", recursive=True))
    assert [i.path for i in listing] == [f"{lake}/raw/big.parquet"]
    fs.delete_file(f"{lake}/raw/big.parquet")
    assert fs.get_file_info(f"{lake}/raw/big.parquet").type == pafs.FileType.NotFound


def test_iceberg_table_lifecycle(store, lake, tmp_path):
    from pyiceberg.catalog.sql import SqlCatalog
    from pyiceberg.expressions import EqualTo
    from pyiceberg.types import StringType

    catalog = SqlCatalog("cairn", **{
        "uri": f"sqlite:///{tmp_path}/catalog.db", "warehouse": f"s3://{lake}/warehouse",
        "s3.endpoint": store.endpoint, "s3.access-key-id": ADMIN[0], "s3.secret-access-key": ADMIN[1],
        "s3.region": REGION, "s3.force-virtual-addressing": "false"})
    catalog.create_namespace("sales")
    orders = pa.table({"order_id": pa.array([1, 2, 3], pa.int64()), "region": ["EMEA", "AMER", "EMEA"],
                       "amount": pa.array([10.0, 20.0, 30.0])})
    table = catalog.create_table("sales.orders", schema=orders.schema)
    table.append(orders)
    first = table.current_snapshot().snapshot_id
    table.append(pa.table({"order_id": pa.array([4], pa.int64()), "region": ["APAC"], "amount": pa.array([40.0])}))
    assert table.scan().to_arrow().num_rows == 4

    # Time travel: the first snapshot still reads as it was.
    assert table.scan(snapshot_id=first).to_arrow().num_rows == 3

    # Schema evolution, then rows in the new shape.
    with table.update_schema() as upd:
        upd.add_column("channel", StringType())
    table = catalog.load_table("sales.orders")
    table.append(pa.table({"order_id": pa.array([5], pa.int64()), "region": ["EMEA"], "amount": pa.array([50.0]),
                           "channel": ["web"]}))
    rows = table.scan().to_arrow()
    assert rows.num_rows == 5 and rows.column("channel").null_count == 4

    # Row-level delete (copy-on-write rewrites the data files).
    table.delete(EqualTo("region", "AMER"))
    left = catalog.load_table("sales.orders").scan().to_arrow()
    assert sorted(left.column("order_id").to_pylist()) == [1, 3, 4, 5]
    emea = catalog.load_table("sales.orders").scan(row_filter=EqualTo("region", "EMEA")).to_arrow()
    assert pc.sum(emea.column("amount")).as_py() == 90.0
    assert len(catalog.load_table("sales.orders").history()) >= 4

    # Everything the table needs lives in the store: metadata, manifests, data.
    keys = [i.path for i in s3fs(store).get_file_info(pafs.FileSelector(f"{lake}/warehouse", recursive=True))
            if i.type == pafs.FileType.File]
    assert any(k.endswith(".metadata.json") for k in keys) and any(k.endswith(".avro") for k in keys)
    assert any(k.endswith(".parquet") for k in keys)
